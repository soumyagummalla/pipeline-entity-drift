# pipeline-entity-drift

> **Graph-native temporal entity resolution for data pipelines.**
> Detects when the same real-world entity appears under different names across monthly ingestion cycles — and resolves it using a hybrid scoring algorithm backed by Neo4j.

---

## The Problem

Every data pipeline that ingests named entities at a regular cadence has a silent failure mode: **entity drift**.

A service gets rebranded. A company gets acquired. A customer changes their legal name. The pipeline sees a new name and creates a new entity. The original shows a revenue cliff. The new one has no history. Downstream models — forecasting, analytics, ML — inherit this broken identity layer and produce results that are confidently wrong.

String matching patches this temporarily. It doesn't solve it. This project does.

---

## The Solution

A **temporal graph layer** that models entity identity as a chain of monthly snapshots connected by typed relationships — giving your pipeline memory, auditability, and a structured REVIEW queue instead of silent failures.

```
EntitySnapshot (Jan) ──PRECEDED_BY──► EntitySnapshot (Feb) ──PRECEDED_BY──► EntitySnapshot (Mar)
        │                                        │
   SNAPSHOT_OF                            CONFLICTS_WITH ── conflict_type: NAME_CHANGED_REVENUE_STABLE
        │                                        │
  CanonicalEntity                         DriftEvent
```

---

## How It Works

The hybrid scoring pipeline runs pairwise comparison across consecutive monthly snapshots using two signals:

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

Validated against **300 real companies from SEC EDGAR** with documented name change history:

| Metric | Result |
|--------|--------|
| Monthly snapshot pairs scored | 9,137 |
| Known drift events | 34 |
| Drift detection rate | **100%** |
| Real companies detected | Square→Block, Facebook→Meta, Tesla Motors→Tesla, JPMorgan (4 changes) |

---

## Graph Data Model

**Node types:**
- `EntitySnapshot` — entity record for a specific ingestion month
- `CanonicalEntity` — stable real-world identity across time
- `DriftEvent` — flagged drift event with type and confidence score

**Relationship types:**
- `SNAPSHOT_OF` — links a monthly record to its canonical entity
- `PRECEDED_BY` — temporal chain linking consecutive snapshots
- `RESOLVES_TO` — connects snapshots identified as the same entity
- `CONFLICTS_WITH` — surfaces unresolved or conflicting matches

---

## Project Structure

```
pipeline-entity-drift/
│
├── data/
│   ├── entity_snapshots.csv       # Monthly entity records (generated)
│   └── resolution_scores.csv      # Pairwise hybrid scores (generated)
│
├── edgar_pull.py                  # Pull real company data from SEC EDGAR API
├── generate_sample_data.py        # Generate synthetic dataset for testing
├── resolution_algorithm.py        # Hybrid scoring pipeline
├── neo4j_loader.py               # Load graph into Neo4j AuraDB
├── demo_queries.cypher           # 10 Cypher queries for demo / exploration
└── README.md
```

---

## Quickstart

### 1. Clone the repo

```bash
git clone https://github.com/soumyagummalla/pipeline-entity-drift.git
cd pipeline-entity-drift
```

### 2. Install dependencies

```bash
pip install pandas numpy neo4j requests
```

### 3. Pull real data from SEC EDGAR

```python
# Update your email in edgar_pull.py first
HEADERS = {"User-Agent": "your-project your@email.com"}
```

```bash
python edgar_pull.py
```

### 4. Build monthly snapshots

```bash
python generate_sample_data.py
```

### 5. Run the hybrid scoring pipeline

```bash
python resolution_algorithm.py
```

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
RETURN [n IN nodes(path) | n.month + ': ' + n.entity_name] AS identity_chain
```

**Dashboard — all drift events in a given month:**
```cypher
MATCH (s:EntitySnapshot {month: '2021-11'})
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
