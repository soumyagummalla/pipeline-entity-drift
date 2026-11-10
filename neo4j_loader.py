"""
Neo4j Graph Loader
===================
Loads entity snapshots and resolution scores into Neo4j AuraDB.

Graph model:
  Nodes:
    - EntitySnapshot   {snapshot_id, cik, month, entity_name, revenue_usd, drift_type}
    - CanonicalEntity  {cik, current_name, first_seen, last_seen}
    - DriftEvent       {event_id, cik, month_from, month_to, drift_type, rename_kind,
                        conflict_type, hybrid_score}

  Relationships:
    - SNAPSHOT_OF    (EntitySnapshot)-[:SNAPSHOT_OF]->(CanonicalEntity)
    - PRECEDED_BY    (EntitySnapshot)-[:PRECEDED_BY]->(EntitySnapshot)
    - RESOLVES_TO    (EntitySnapshot)-[:RESOLVES_TO {score, outcome}]->(EntitySnapshot)
    - CONFLICTS_WITH (EntitySnapshot)-[:CONFLICTS_WITH {conflict_type}]->(EntitySnapshot)
    - TRIGGERED      (EntitySnapshot)-[:TRIGGERED]->(DriftEvent)

Setup:
  1. Create a free Neo4j AuraDB instance at https://console.neo4j.io
  2. Copy your URI and password
  3. Set environment variables (recommended) or pass credentials directly:

     export NEO4J_URI="neo4j+s://xxxx.databases.neo4j.io"
     export NEO4J_USER="neo4j"
     export NEO4J_PASSWORD="your-password"

  4. pip install neo4j pandas
  5. python neo4j_loader.py   (batched: a few minutes; safe to rerun after a failure)

Input:
  data/entity_snapshots.csv
  data/resolution_scores.csv
"""

import os
import pandas as pd
from pathlib import Path

try:
    from neo4j import GraphDatabase
    NEO4J_AVAILABLE = True
except ImportError:
    NEO4J_AVAILABLE = False
    print("neo4j driver not installed. Run: pip install neo4j")

DATA_DIR = Path("data")
BATCH_SIZE = 1000  # rows per UNWIND query


def _text(value) -> str:
    """Empty string for missing CSV values (NaN), otherwise the value as text."""
    return "" if pd.isna(value) else str(value)


class Neo4jLoader:
    def __init__(self, uri=None, user=None, password=None, dry_run=False):
        self.uri      = uri      or os.getenv("NEO4J_URI",      "neo4j+s://xxxx.databases.neo4j.io")
        self.user     = user     or os.getenv("NEO4J_USER",     "neo4j")
        self.password = password or os.getenv("NEO4J_PASSWORD", "your-password")
        self.dry_run  = dry_run or not NEO4J_AVAILABLE
        self.driver   = None

        if not self.dry_run:
            self.driver = GraphDatabase.driver(self.uri, auth=(self.user, self.password))
            print(f"Connected to Neo4j: {self.uri}")

    def close(self):
        if self.driver:
            self.driver.close()

    def run(self, cypher: str, params: dict = None):
        if self.dry_run:
            return
        with self.driver.session() as session:
            session.run(cypher, params or {}).consume()

    def run_batches(self, cypher: str, rows: list[dict], label: str):
        """
        Sends rows in batches of BATCH_SIZE through one UNWIND query per batch.
        Each batch is a managed write transaction, so the driver retries it
        automatically if the connection drops. Every query uses MERGE, so a
        rerun after a failure skips what is already loaded.
        """
        total = len(rows)
        if self.dry_run:
            print(f"  [dry run] {label}: {total}")
            return
        with self.driver.session() as session:
            for i in range(0, total, BATCH_SIZE):
                batch = rows[i:i + BATCH_SIZE]
                session.execute_write(lambda tx: tx.run(cypher, rows=batch).consume())
                print(f"  {label}: {min(i + BATCH_SIZE, total)}/{total}", end="\r")
        print(f"  {label}: {total}/{total}      ")

    # ── Schema ────────────────────────────────────────────────────────────────
    def create_schema(self):
        print("\n── Creating schema constraints and indexes...")
        for stmt in [
            "CREATE CONSTRAINT snapshot_id IF NOT EXISTS FOR (s:EntitySnapshot) REQUIRE s.snapshot_id IS UNIQUE",
            "CREATE CONSTRAINT canonical_cik IF NOT EXISTS FOR (c:CanonicalEntity) REQUIRE c.cik IS UNIQUE",
            "CREATE CONSTRAINT drift_event_id IF NOT EXISTS FOR (d:DriftEvent) REQUIRE d.event_id IS UNIQUE",
            "CREATE INDEX snapshot_month IF NOT EXISTS FOR (s:EntitySnapshot) ON (s.month)",
            "CREATE INDEX snapshot_cik IF NOT EXISTS FOR (s:EntitySnapshot) ON (s.cik)",
            "CREATE INDEX snapshot_name IF NOT EXISTS FOR (s:EntitySnapshot) ON (s.entity_name)",
        ]:
            self.run(stmt)
        print("  Schema ready.")

    # ── CanonicalEntity nodes ─────────────────────────────────────────────────
    def load_canonical_entities(self, snapshots: pd.DataFrame):
        print("\n── Loading CanonicalEntity nodes...")
        canonical = (
            snapshots.sort_values("month")
            .groupby("cik")
            .agg(
                current_name=("entity_name", "last"),
                first_seen=("month", "min"),
                last_seen=("month", "max"),
                snapshot_count=("snapshot_id", "count"),
            )
            .reset_index()
        )
        rows = [
            {
                "cik": str(r["cik"]),
                "current_name": r["current_name"],
                "first_seen": r["first_seen"],
                "last_seen": r["last_seen"],
                "snapshot_count": int(r["snapshot_count"]),
            }
            for _, r in canonical.iterrows()
        ]
        self.run_batches("""
            UNWIND $rows AS row
            MERGE (c:CanonicalEntity {cik: row.cik})
            SET c.current_name   = row.current_name,
                c.first_seen     = row.first_seen,
                c.last_seen      = row.last_seen,
                c.snapshot_count = row.snapshot_count
        """, rows, "CanonicalEntity")

    # ── EntitySnapshot nodes ──────────────────────────────────────────────────
    def load_snapshots(self, snapshots: pd.DataFrame):
        print("\n── Loading EntitySnapshot nodes...")
        rows = [
            {
                "snapshot_id": r["snapshot_id"],
                "cik":         str(r["cik"]),
                "month":       r["month"],
                "entity_name": r["entity_name"],
                "revenue_usd": float(r["revenue_usd"]),
                "drift_type":  r["drift_type"],
                "rename_kind": _text(r.get("rename_kind", "")),
            }
            for _, r in snapshots.iterrows()
        ]
        self.run_batches("""
            UNWIND $rows AS row
            MERGE (s:EntitySnapshot {snapshot_id: row.snapshot_id})
            SET s.cik         = row.cik,
                s.month       = row.month,
                s.entity_name = row.entity_name,
                s.revenue_usd = row.revenue_usd,
                s.drift_type  = row.drift_type,
                s.rename_kind = row.rename_kind
        """, rows, "EntitySnapshot")

    # ── SNAPSHOT_OF relationships ─────────────────────────────────────────────
    def link_snapshots_to_canonical(self, snapshots: pd.DataFrame):
        print("\n── Creating SNAPSHOT_OF relationships...")
        rows = [{"snapshot_id": r["snapshot_id"], "cik": str(r["cik"])} for _, r in snapshots.iterrows()]
        self.run_batches("""
            UNWIND $rows AS row
            MATCH (s:EntitySnapshot {snapshot_id: row.snapshot_id})
            MATCH (c:CanonicalEntity {cik: row.cik})
            MERGE (s)-[:SNAPSHOT_OF]->(c)
        """, rows, "SNAPSHOT_OF")

    # ── Resolution relationships ──────────────────────────────────────────────
    def load_resolution_relationships(self, scores: pd.DataFrame, snapshots: pd.DataFrame):
        print("\n── Creating resolution relationships...")
        lookup = snapshots.set_index(["cik", "month"])["snapshot_id"].to_dict()

        preceded, resolves, conflicts, drift = [], [], [], []
        for _, row in scores.iterrows():
            cik      = str(row["cik"]).zfill(10)
            sid_from = lookup.get((cik, row["month_from"]))
            sid_to   = lookup.get((cik, row["month_to"]))
            if not sid_from or not sid_to:
                continue

            base = {
                "sid_from":      sid_from,
                "sid_to":        sid_to,
                "hybrid_score":  float(row["hybrid_score"]),
                "name_score":    float(row["name_score"]),
                "revenue_score": float(row["revenue_score"]),
                "outcome":       row["outcome"],
            }
            preceded.append(base)
            if row["outcome"] in ("AUTO_RESOLVE", "REVIEW"):
                resolves.append(base)
            if str(row["conflict"]).lower() == "true":
                conflicts.append({**base, "conflict_type": row["conflict_type"]})
            if _text(row.get("ground_truth", "none")) not in ("", "none"):
                drift.append({
                    **base,
                    "event_id":      f"DE_{sid_from}_{sid_to}",
                    "cik":           cik,
                    "month_from":    row["month_from"],
                    "month_to":      row["month_to"],
                    "drift_type":    row["ground_truth"],
                    "rename_kind":   _text(row.get("rename_kind", "")),
                    "conflict_type": row["conflict_type"],
                })

        # PRECEDED_BY: the temporal chain, always created
        self.run_batches("""
            UNWIND $rows AS row
            MATCH (a:EntitySnapshot {snapshot_id: row.sid_from})
            MATCH (b:EntitySnapshot {snapshot_id: row.sid_to})
            MERGE (b)-[:PRECEDED_BY]->(a)
        """, preceded, "PRECEDED_BY")

        # RESOLVES_TO: the score linked the pair (AUTO_RESOLVE or REVIEW)
        self.run_batches("""
            UNWIND $rows AS row
            MATCH (a:EntitySnapshot {snapshot_id: row.sid_from})
            MATCH (b:EntitySnapshot {snapshot_id: row.sid_to})
            MERGE (a)-[r:RESOLVES_TO]->(b)
            SET r.hybrid_score  = row.hybrid_score,
                r.name_score    = row.name_score,
                r.revenue_score = row.revenue_score,
                r.outcome       = row.outcome
        """, resolves, "RESOLVES_TO")

        # CONFLICTS_WITH: the two signals disagree
        self.run_batches("""
            UNWIND $rows AS row
            MATCH (a:EntitySnapshot {snapshot_id: row.sid_from})
            MATCH (b:EntitySnapshot {snapshot_id: row.sid_to})
            MERGE (a)-[r:CONFLICTS_WITH]->(b)
            SET r.conflict_type = row.conflict_type,
                r.hybrid_score  = row.hybrid_score,
                r.name_score    = row.name_score,
                r.revenue_score = row.revenue_score
        """, conflicts, "CONFLICTS_WITH")

        # DriftEvent nodes: ground-truth renames
        self.run_batches("""
            UNWIND $rows AS row
            MERGE (d:DriftEvent {event_id: row.event_id})
            SET d.cik           = row.cik,
                d.month_from    = row.month_from,
                d.month_to      = row.month_to,
                d.drift_type    = row.drift_type,
                d.rename_kind   = row.rename_kind,
                d.conflict_type = row.conflict_type,
                d.hybrid_score  = row.hybrid_score
            WITH d, row
            MATCH (a:EntitySnapshot {snapshot_id: row.sid_from})
            MERGE (a)-[:TRIGGERED]->(d)
        """, drift, "DriftEvent")

    # ── Master load ───────────────────────────────────────────────────────────
    def load_all(self, snapshots: pd.DataFrame, scores: pd.DataFrame):
        self.create_schema()
        self.load_canonical_entities(snapshots)
        self.load_snapshots(snapshots)
        self.link_snapshots_to_canonical(snapshots)
        self.load_resolution_relationships(scores, snapshots)
        print("\nGraph load complete.")


# ── Run ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    snapshots = pd.read_csv(DATA_DIR / "entity_snapshots.csv", dtype={"cik": str})
    scores    = pd.read_csv(DATA_DIR / "resolution_scores.csv", dtype={"cik": str})

    snapshots["cik"] = snapshots["cik"].str.zfill(10)
    scores["cik"]    = scores["cik"].str.zfill(10)

    print(f"Snapshots : {len(snapshots)}")
    print(f"Scores    : {len(scores)}")

    # ── Credentials ───────────────────────────────────────────────────────────
    # Option 1 (recommended): set environment variables
    #   export NEO4J_URI="neo4j+s://xxxx.databases.neo4j.io"
    #   export NEO4J_USER="neo4j"
    #   export NEO4J_PASSWORD="your-password"
    #
    # Option 2: pass directly (do not commit credentials to Git)
    #   loader = Neo4jLoader(uri="...", user="...", password="...")

    loader = Neo4jLoader()  # reads from environment variables
    loader.load_all(snapshots, scores)
    loader.close()
