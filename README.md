# pipeline-entity-drift

> **Graph-native temporal entity resolution for data pipelines.**
> Detects when the same real-world entity appears under different names across ingestion cycles — and resolves it using a hybrid scoring algorithm backed by Neo4j.

---

## The Problem

Every data pipeline that ingests named entities at a regular cadence has a silent failure mode: **entity drift**.

A service gets rebranded. A company gets acquired. A customer changes their legal name. The pipeline sees a new name and creates a new entity. The original shows a revenue cliff. The new one has no history. Downstream models — forecasting, analytics, ML — inherit this broken identity layer and produce results that are confidently wrong.

String matching patches this temporarily. It doesn't solve it. This project does.

---

## The Solution

A **temporal graph layer** that models entity identity as a chain of periodic snapshots connected by typed relationships — giving your pipeline memory, auditability, and a structured REVIEW queue instead of silent failures.

```
EntitySnapshot (Q3) ◄──PRECEDED_BY── EntitySnapshot (Q4) ◄──PRECEDED_BY── EntitySnapshot (Q1)
        │                                        │
   SNAPSHOT_OF                            CONFLICTS_WITH ── conflict_type: NAME_CHANGED_REVENUE_STABLE
        │                                        │
  CanonicalEntity                         DriftEvent
```

---

## How It Works

The hybrid scoring pipeline compares each pair of consecutive snapshots (quarterly in the EDGAR demo) using two signals:

| Signal | Weight | What it catches |
|--------|--------|-----------------|
| Fuzzy name similarity | 60% | Renames, abbreviations, legal suffix changes, typographic drift |
| Revenue continuity | 40% | Mergers, spinoffs, structural anomalies |

**Resolution outcomes:**

| Outcome | Score | Action |
|---------|-------|--------|
| `AUTO_RESOLVE` | ≥ 0.85 | Same entity — resolve and load automatically |
| `REVIEW` | 0.60–0.85 | Likely same — flag for human confirmation |
| `NEW_ENTITY` | < 0.60 | Genuinely new — create new canonical record |

**Conflict type labels** make the REVIEW queue actionable:
- `NAME_CHANGED_REVENUE_STABLE` — name changed, revenue held steady → likely a rename
- `NAME_STABLE_REVENUE_SHIFTED` — name stable, revenue shifted → merger, spinoff, or anomaly

---

## Validation Results

Run on **230 SEC EDGAR companies** with documented name changes (13,723 quarterly snapshots, 13,493 consecutive pairs).

| | Pairs | Result |
|---|---|---|
| Substantive renames (e.g. Square → Block, Anthem → Elevance) | 111 | **84 surfaced**: 50 linked by score, 34 more caught only by the `NAME_CHANGED_REVENUE_STABLE` conflict label |
| Cosmetic renames (e.g. `NIKE INC` → `NIKE, Inc.`) | 46 | 34 auto-resolved, 12 sent to review, 0 split |
| Unchanged names | 13,336 | 0 wrongly split, 2,486 sent to review |

**String matching alone** links 2 of 111 substantive renames at a 0.85 threshold, 19 at 0.70, and 55 at 0.50. Even a threshold loose enough to merge unrelated companies catches only half of the real renames.

**What this dataset does and doesn't test.** Every pair shares a CIK, so every pair is truly the same company. The validation measures how well renames get linked; it doesn't measure how well the algorithm rejects unrelated lookalikes, which needs pairs across different companies.

**Known limitations**
- 27 substantive renames are still split: their revenue also moved sharply in the rename quarter, so neither signal links them.
- Quarter-over-quarter revenue swings send about a fifth of unchanged pairs to review. Year-over-year and seasonality-adjusted comparisons were tested and didn't improve this.
- `NAME_STABLE_REVENUE_SHIFTED` flags candidate mergers and spinoffs, but it isn't validated against labels.

---

## Graph Data Model

**Node types:**
- `EntitySnapshot` — entity record for one ingestion period (a quarter in the EDGAR demo)
- `CanonicalEntity` — stable real-world identity across time
- `DriftEvent` — flagged drift event with type and confidence score

**Relationship types:**
- `SNAPSHOT_OF` — links a snapshot to its canonical entity
- `PRECEDED_BY` — temporal chain linking consecutive snapshots
- `RESOLVES_TO` — connects snapshots identified as the same entity
- `CONFLICTS_WITH` — surfaces unresolved or conflicting matches
- `TRIGGERED` — links a snapshot to the `DriftEvent` it produced

---

## Project Structure

```
pipeline-entity-drift/
│
├── data/                          # generated, not committed
│   ├── entities_raw.csv           # Companies and name history (edgar_pull.py)
│   ├── revenue_snapshots.csv      # Discrete quarterly revenue (edgar_pull.py)
│   ├── entity_snapshots.csv       # One snapshot per company per quarter (build_snapshots.py)
│   └── resolution_scores.csv      # Pairwise hybrid scores (resolution_algorithm.py)
│
├── edgar_pull.py                  # Pull companies and quarterly revenue from SEC EDGAR
├── build_snapshots.py             # Build snapshots and ground-truth rename labels
├── resolution_algorithm.py        # Hybrid scoring pipeline and validation
├── neo4j_loader.py                # Load graph into Neo4j AuraDB
├── demo_queries.cypher            # 11 Cypher queries for demo / exploration
├── entity_drift.ipynb             # End-to-end notebook
└── README.md
```

**About the revenue data:** EDGAR reports revenue for several overlapping periods (3-month quarters, year-to-date totals, full years). `edgar_pull.py` keeps only 3-month figures and derives Q4 from the annual total, so consecutive snapshots are comparable. It also merges revenue tags, because many companies switched tags when ASC 606 took effect in 2018.

---

## Quickstart

### 1. Clone the repo

```bash
git clone https://github.com/soumyagummalla/pipeline-entity-drift.git
cd pipeline-entity-drift
```

### 2. Install dependencies

```bash
pip install pandas numpy neo4j requests tqdm
```

### 3. Pull real data from SEC EDGAR

```python
# Update your email in edgar_pull.py first
HEADERS = {"User-Agent": "your-project your@email.com"}
```

```bash
python edgar_pull.py
```

### 4. Build quarterly snapshots

```bash
python build_snapshots.py
```

### 5. Run the hybrid scoring pipeline

```bash
python resolution_algorithm.py
```

This also prints the validation report.

### 6. Load into Neo4j

Create a free instance at [console.neo4j.io](https://console.neo4j.io), then:

```python
# In neo4j_loader.py, set your credentials
loader = Neo4jLoader(
    uri="neo4j+s://xxxx.databases.neo4j.io",
    user="neo4j",
    password="your-password"
)
```

```bash
python neo4j_loader.py
```

### 7. Run demo queries

Open `demo_queries.cypher` in the Neo4j browser and run the queries in order.

---

## Key Cypher Queries

**Find all rename drift events:**
```cypher
MATCH (a:EntitySnapshot)-[r:CONFLICTS_WITH]->(b:EntitySnapshot)
WHERE r.conflict_type = 'NAME_CHANGED_REVENUE_STABLE'
RETURN a.entity_name AS name_before,
       b.entity_name AS name_after,
       a.month       AS month,
       r.name_score  AS name_similarity,
       r.revenue_score AS revenue_continuity
ORDER BY r.name_score ASC
```

**Walk the full identity chain for one entity:**
```cypher
MATCH path = (start:EntitySnapshot)-[:PRECEDED_BY*]->(end:EntitySnapshot)
WHERE start.cik = $cik
  AND NOT (start)<-[:PRECEDED_BY]-()
  AND NOT (end)-[:PRECEDED_BY]->()
RETURN [n IN nodes(path) | n.month + ': ' + n.entity_name] AS identity_chain
```

**Dashboard — all drift events in a given quarter:**
```cypher
MATCH (s:EntitySnapshot {month: '2021-12'})
OPTIONAL MATCH (s)-[:TRIGGERED]->(d:DriftEvent)
RETURN s.entity_name AS entity,
       COALESCE(d.drift_type, 'stable') AS status
ORDER BY status DESC
```

---

## Tech Stack

| Layer | Tool |
|-------|------|
| Data pull | SEC EDGAR API |
| Scoring pipeline | Python (difflib, pandas, numpy) |
| Graph database | Neo4j AuraDB |
| Query language | Cypher |
| Exploration | Jupyter Notebook |

---

## Author

**Soumya Gummalla** — Data Engineer building pipelines for economic forecasting models at a cloud provider.

[LinkedIn](https://www.linkedin.com/in/soumyagummalla) · [GitHub](https://github.com/soumyagummalla)
