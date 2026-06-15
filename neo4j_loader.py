"""
Neo4j Graph Loader
===================
Loads entity snapshots and resolution scores into Neo4j AuraDB.

Graph model:
  Nodes:
    - EntitySnapshot   {snapshot_id, cik, month, entity_name, revenue_usd, drift_type}
    - CanonicalEntity  {cik, current_name, first_seen, last_seen}
    - DriftEvent       {event_id, cik, month, drift_type, conflict_type, hybrid_score}

  Relationships:
    - SNAPSHOT_OF    (EntitySnapshot)-[:SNAPSHOT_OF]->(CanonicalEntity)
    - PRECEDED_BY    (EntitySnapshot)-[:PRECEDED_BY]->(EntitySnapshot)
    - RESOLVES_TO    (EntitySnapshot)-[:RESOLVES_TO {score, outcome}]->(EntitySnapshot)
    - CONFLICTS_WITH (EntitySnapshot)-[:CONFLICTS_WITH {conflict_type}]->(EntitySnapshot)

Setup:
  1. Create a free Neo4j AuraDB instance at https://console.neo4j.io
  2. Copy your URI and password
  3. Set environment variables (recommended) or pass credentials directly:

     export NEO4J_URI="neo4j+s://xxxx.databases.neo4j.io"
     export NEO4J_USER="neo4j"
     export NEO4J_PASSWORD="your-password"

  4. pip install neo4j pandas
  5. python neo4j_loader.py

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
            session.run(cypher, params or {})

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
            snapshots.groupby("cik")
            .agg(
                current_name=("entity_name", "last"),
                first_seen=("month", "min"),
                last_seen=("month", "max"),
                snapshot_count=("snapshot_id", "count"),
            )
            .reset_index()
        )
        cypher = """
        MERGE (c:CanonicalEntity {cik: $cik})
        SET c.current_name   = $current_name,
            c.first_seen     = $first_seen,
            c.last_seen      = $last_seen,
            c.snapshot_count = $snapshot_count
        """
        for _, row in canonical.iterrows():
            self.run(cypher, row.to_dict())
        print(f"  Loaded {len(canonical)} CanonicalEntity nodes.")

    # ── EntitySnapshot nodes ──────────────────────────────────────────────────
    def load_snapshots(self, snapshots: pd.DataFrame):
        print("\n── Loading EntitySnapshot nodes...")
        cypher = """
        MERGE (s:EntitySnapshot {snapshot_id: $snapshot_id})
        SET s.cik         = $cik,
            s.month       = $month,
            s.entity_name = $entity_name,
            s.revenue_usd = $revenue_usd,
            s.drift_type  = $drift_type
        """
        for _, row in snapshots.iterrows():
            self.run(cypher, {
                "snapshot_id": row["snapshot_id"],
                "cik":         str(row["cik"]),
                "month":       row["month"],
                "entity_name": row["entity_name"],
                "revenue_usd": float(row["revenue_usd"]),
                "drift_type":  row["drift_type"],
            })
        print(f"  Loaded {len(snapshots)} EntitySnapshot nodes.")

    # ── SNAPSHOT_OF relationships ─────────────────────────────────────────────
    def link_snapshots_to_canonical(self, snapshots: pd.DataFrame):
        print("\n── Creating SNAPSHOT_OF relationships...")
        cypher = """
        MATCH (s:EntitySnapshot {snapshot_id: $snapshot_id})
        MATCH (c:CanonicalEntity {cik: $cik})
        MERGE (s)-[:SNAPSHOT_OF]->(c)
        """
        for _, row in snapshots.iterrows():
            self.run(cypher, {"snapshot_id": row["snapshot_id"], "cik": str(row["cik"])})
        print(f"  Created {len(snapshots)} SNAPSHOT_OF relationships.")

    # ── Resolution relationships ──────────────────────────────────────────────
    def load_resolution_relationships(self, scores: pd.DataFrame, snapshots: pd.DataFrame):
        print("\n── Creating resolution relationships...")
        lookup = snapshots.set_index(["cik", "month"])["snapshot_id"].to_dict()

        counts = {"preceded": 0, "resolves": 0, "conflicts": 0, "drift": 0}

        for _, row in scores.iterrows():
            cik      = str(row["cik"]).zfill(10)
            sid_from = lookup.get((cik, row["month_from"]))
            sid_to   = lookup.get((cik, row["month_to"]))

            if not sid_from or not sid_to:
                continue

            base = {
                "sid_from":     sid_from,
                "sid_to":       sid_to,
                "hybrid_score": float(row["hybrid_score"]),
                "name_score":   float(row["name_score"]),
                "revenue_score":float(row["revenue_score"]),
                "outcome":      row["outcome"],
            }

            # PRECEDED_BY — temporal chain, always created
            self.run("""
                MATCH (a:EntitySnapshot {snapshot_id: $sid_from})
                MATCH (b:EntitySnapshot {snapshot_id: $sid_to})
                MERGE (b)-[:PRECEDED_BY]->(a)
            """, base)
            counts["preceded"] += 1

            # RESOLVES_TO — when algorithm resolves as same entity
            if row["outcome"] in ("AUTO_RESOLVE", "REVIEW"):
                self.run("""
                    MATCH (a:EntitySnapshot {snapshot_id: $sid_from})
                    MATCH (b:EntitySnapshot {snapshot_id: $sid_to})
                    MERGE (a)-[r:RESOLVES_TO]->(b)
                    SET r.hybrid_score  = $hybrid_score,
                        r.name_score    = $name_score,
                        r.revenue_score = $revenue_score,
                        r.outcome       = $outcome
                """, base)
                counts["resolves"] += 1

            # CONFLICTS_WITH — when signals disagree
            if row["conflict"]:
                self.run("""
                    MATCH (a:EntitySnapshot {snapshot_id: $sid_from})
                    MATCH (b:EntitySnapshot {snapshot_id: $sid_to})
                    MERGE (a)-[r:CONFLICTS_WITH]->(b)
                    SET r.conflict_type = $conflict_type,
                        r.hybrid_score  = $hybrid_score,
                        r.name_score    = $name_score,
                        r.revenue_score = $revenue_score
                """, {**base, "conflict_type": row["conflict_type"]})
                counts["conflicts"] += 1

            # DriftEvent node — for ground truth labeled events
            if row.get("ground_truth", "none") != "none":
                event_id = f"DE_{sid_from}_{sid_to}"
                self.run("""
                    MERGE (d:DriftEvent {event_id: $event_id})
                    SET d.cik           = $cik,
                        d.month_from    = $month_from,
                        d.month_to      = $month_to,
                        d.drift_type    = $drift_type,
                        d.conflict_type = $conflict_type,
                        d.hybrid_score  = $hybrid_score
                    WITH d
                    MATCH (a:EntitySnapshot {snapshot_id: $sid_from})
                    MERGE (a)-[:TRIGGERED]->(d)
                """, {
                    **base,
                    "event_id":     event_id,
                    "cik":          cik,
                    "month_from":   row["month_from"],
                    "month_to":     row["month_to"],
                    "drift_type":   row["ground_truth"],
                    "conflict_type":row["conflict_type"],
                })
                counts["drift"] += 1

        print(f"  PRECEDED_BY    : {counts['preceded']}")
        print(f"  RESOLVES_TO    : {counts['resolves']}")
        print(f"  CONFLICTS_WITH : {counts['conflicts']}")
        print(f"  DriftEvent     : {counts['drift']}")

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
