// ═══════════════════════════════════════════════════════════════════════════
// DEMO CYPHER QUERIES
// "Your Pipeline Has an Identity Problem. Graphs Can Fix It."
// NODES 2026 — Soumya Gummalla
// ═══════════════════════════════════════════════════════════════════════════


// ── QUERY 1: The Problem (show this first — audience immediately gets it) ───
// "How many unique names has this entity appeared under?"
// Run this on Facebook/Meta to open the talk.

MATCH (s:EntitySnapshot)
WHERE s.cik = '0001326801'
RETURN s.month, s.entity_name, s.revenue_usd
ORDER BY s.month;


// ── QUERY 2: Show the drift event ──────────────────────────────────────────
// "Find all snapshots flagged as drift"

MATCH (s:EntitySnapshot)-[:TRIGGERED]->(d:DriftEvent)
RETURN s.entity_name     AS name_before,
       d.drift_type      AS drift_type,
       d.conflict_type   AS signal,
       d.hybrid_score    AS confidence,
       d.month_from      AS month
ORDER BY d.month_from;


// ── QUERY 3: The algorithm in action ───────────────────────────────────────
// "Show me all pairs where the name changed but revenue was stable"
// This is the NAME_CHANGED_REVENUE_STABLE conflict — renames and rebrands

MATCH (a:EntitySnapshot)-[r:CONFLICTS_WITH]->(b:EntitySnapshot)
WHERE r.conflict_type = 'NAME_CHANGED_REVENUE_STABLE'
RETURN a.entity_name    AS name_before,
       b.entity_name    AS name_after,
       a.month          AS month_from,
       b.month          AS month_to,
       r.name_score     AS name_similarity,
       r.revenue_score  AS revenue_continuity,
       r.hybrid_score   AS combined_score
ORDER BY r.name_score ASC;


// ── QUERY 4: Revenue shift with stable name (spinoffs/merges) ─────────────
// "Find where the name stayed the same but revenue shifted dramatically"

MATCH (a:EntitySnapshot)-[r:CONFLICTS_WITH]->(b:EntitySnapshot)
WHERE r.conflict_type = 'NAME_STABLE_REVENUE_SHIFTED'
RETURN a.entity_name    AS entity,
       a.month          AS month_from,
       b.month          AS month_to,
       a.revenue_usd    AS revenue_before,
       b.revenue_usd    AS revenue_after,
       r.revenue_score  AS continuity_score,
       r.hybrid_score   AS combined_score
ORDER BY r.revenue_score ASC;


// ── QUERY 5: Walk the identity chain ───────────────────────────────────────
// "Give me the full identity history of one entity across 12 months"
// This is the graph advantage slide — one Cypher, full history

MATCH path = (start:EntitySnapshot)-[:PRECEDED_BY*]->(end:EntitySnapshot)
WHERE start.cik = '0001326801'
  AND NOT (start)<-[:PRECEDED_BY]-()     // most recent snapshot
RETURN [n IN nodes(path) | n.month + ': ' + n.entity_name] AS identity_chain,
       length(path) AS months_tracked;


// ── QUERY 6: Audit trail — every resolution decision, on demand ────────────
// "Show me every resolution decision for Meta with confidence scores"
// This is the auditability argument

MATCH (a:EntitySnapshot)-[r:RESOLVES_TO]->(b:EntitySnapshot)
WHERE a.cik = '0001326801'
RETURN a.month          AS month_from,
       a.entity_name    AS name_from,
       b.entity_name    AS name_to,
       r.name_score     AS name_score,
       r.revenue_score  AS revenue_score,
       r.hybrid_score   AS combined,
       r.outcome        AS decision
ORDER BY a.month;


// ── QUERY 7: The canonical entity view ─────────────────────────────────────
// "What does the graph think each entity's real identity is today?"

MATCH (c:CanonicalEntity)<-[:SNAPSHOT_OF]-(s:EntitySnapshot)
WITH c, collect(DISTINCT s.entity_name) AS all_names,
     count(s) AS snapshot_count,
     max(s.month) AS last_seen
WHERE size(all_names) > 1    // only show entities with name drift
RETURN c.current_name    AS canonical_name,
       all_names         AS name_history,
       snapshot_count,
       last_seen
ORDER BY size(all_names) DESC;


// ── QUERY 8: Dashboard — drift summary for a given month ──────────────────
// "What happened in November 2021?" — the big drift month in our dataset

MATCH (s:EntitySnapshot {month: '2021-11'})
OPTIONAL MATCH (s)-[:TRIGGERED]->(d:DriftEvent)
RETURN s.entity_name    AS entity,
       s.revenue_usd    AS revenue,
       COALESCE(d.drift_type, 'stable') AS status,
       COALESCE(d.conflict_type, 'NONE') AS signal
ORDER BY status DESC;


// ── QUERY 9: Compound drift (the chain argument) ───────────────────────────
// "Show entities with more than one drift event across their history"
// This is the "drift compounds" argument vs. lookup tables

MATCH (c:CanonicalEntity)<-[:SNAPSHOT_OF]-(s:EntitySnapshot)-[:TRIGGERED]->(d:DriftEvent)
WITH c, count(d) AS drift_count, collect(d.drift_type) AS drift_types
WHERE drift_count > 1
RETURN c.current_name   AS entity,
       drift_count,
       drift_types
ORDER BY drift_count DESC;


// ── QUERY 10: The closing query — why graph beats a lookup table ──────────
// Run this last. One query. Full entity intelligence.

MATCH (c:CanonicalEntity {current_name: 'Meta Platforms Inc'})
MATCH (c)<-[:SNAPSHOT_OF]-(s:EntitySnapshot)
OPTIONAL MATCH (s)-[conflict:CONFLICTS_WITH]->(next:EntitySnapshot)
OPTIONAL MATCH (s)-[:TRIGGERED]->(d:DriftEvent)
RETURN s.month          AS month,
       s.entity_name    AS name,
       s.revenue_usd    AS revenue,
       COALESCE(conflict.conflict_type, 'STABLE')   AS signal,
       COALESCE(conflict.hybrid_score,  1.0)        AS confidence,
       COALESCE(d.drift_type, 'none')               AS drift_event
ORDER BY s.month;
