"""
Hybrid Entity Resolution Algorithm
====================================
Scores consecutive monthly snapshots for the same entity using two signals:
  1. Fuzzy name similarity   (weight: 0.60)
  2. Revenue continuity      (weight: 0.40)

Resolution outcomes:
  - score >= 0.85  → AUTO_RESOLVE   (same entity, high confidence)
  - score >= 0.60  → REVIEW         (likely same, flag for confirmation)
  - score <  0.60  → NEW_ENTITY     (treat as a different entity)

Conflict detection:
  When signals disagree strongly (|name_score - revenue_score| > 0.35),
  the conflict type is labeled:
  - NAME_CHANGED_REVENUE_STABLE   → rename or rebrand
  - NAME_STABLE_REVENUE_SHIFTED   → merger, spinoff, or structural anomaly

Input:  data/entity_snapshots.csv
Output: data/resolution_scores.csv

Usage:
    python resolution_algorithm.py

Requirements:
    pip install pandas numpy
"""

import pandas as pd
import numpy as np
import difflib
import re
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
DATA_DIR = Path("data")

W_NAME    = 0.60   # weight for name similarity signal
W_REVENUE = 0.40   # weight for revenue continuity signal

AUTO_RESOLVE_THRESHOLD = 0.85
REVIEW_THRESHOLD       = 0.60
CONFLICT_DELTA         = 0.35  # flag conflict if |name_score - revenue_score| > this


# ── Name normalization ────────────────────────────────────────────────────────
STOPWORDS = {
    "inc", "corp", "corporation", "ltd", "llc", "co", "company",
    "the", "and", "of", "group", "holdings", "technologies", "technology",
    "international", "solutions", "services", "systems", "enterprises"
}

def normalize_name(name: str) -> str:
    """Lowercase, strip punctuation, remove common legal suffixes."""
    name = name.lower()
    name = re.sub(r"[^\w\s]", " ", name)
    tokens = [t for t in name.split() if t not in STOPWORDS]
    return " ".join(tokens).strip()


# ── Signal 1: Fuzzy name similarity ──────────────────────────────────────────
def name_similarity(name_a: str, name_b: str) -> float:
    """
    Combines two sub-signals:
      - SequenceMatcher ratio (character-level)
      - Jaccard token overlap (word-level)
    Returns a float between 0.0 and 1.0.
    """
    if name_a == name_b:
        return 1.0

    norm_a = normalize_name(name_a)
    norm_b = normalize_name(name_b)

    if norm_a == norm_b:
        return 1.0

    seq_score = difflib.SequenceMatcher(None, norm_a, norm_b).ratio()

    tokens_a = set(norm_a.split())
    tokens_b = set(norm_b.split())
    if tokens_a or tokens_b:
        jaccard = len(tokens_a & tokens_b) / len(tokens_a | tokens_b)
    else:
        jaccard = 0.0

    return round(0.6 * seq_score + 0.4 * jaccard, 4)


# ── Signal 2: Revenue continuity ─────────────────────────────────────────────
def revenue_continuity(rev_a: float, rev_b: float) -> float:
    """
    Measures how consistent revenue is between consecutive monthly snapshots.
    Uses exponential decay: score = e^(-k * pct_change), k=3.0

    Approximate thresholds:
      < 20% change  → high continuity  (0.85 – 1.0)
      20–50% change → medium           (0.50 – 0.85)
      > 50% change  → low              (0.0  – 0.50)
    """
    if rev_a <= 0 or rev_b <= 0:
        return 0.0

    pct_change = abs(rev_b - rev_a) / rev_a
    score = np.exp(-3.0 * pct_change)
    return round(float(score), 4)


# ── Hybrid scorer ─────────────────────────────────────────────────────────────
def hybrid_score(name_a: str, name_b: str, rev_a: float, rev_b: float) -> dict:
    """
    Combines name similarity and revenue continuity into a single resolution score.
    Detects and labels signal conflicts.
    """
    n_score  = name_similarity(name_a, name_b)
    r_score  = revenue_continuity(rev_a, rev_b)
    combined = round(W_NAME * n_score + W_REVENUE * r_score, 4)

    if combined >= AUTO_RESOLVE_THRESHOLD:
        outcome = "AUTO_RESOLVE"
    elif combined >= REVIEW_THRESHOLD:
        outcome = "REVIEW"
    else:
        outcome = "NEW_ENTITY"

    conflict = abs(n_score - r_score) > CONFLICT_DELTA
    if conflict:
        conflict_type = "NAME_CHANGED_REVENUE_STABLE" if n_score < r_score else "NAME_STABLE_REVENUE_SHIFTED"
    else:
        conflict_type = "NONE"

    return {
        "name_score":    n_score,
        "revenue_score": r_score,
        "hybrid_score":  combined,
        "outcome":       outcome,
        "conflict":      conflict,
        "conflict_type": conflict_type,
    }


# ── Main pipeline ─────────────────────────────────────────────────────────────
def run_resolution(df: pd.DataFrame) -> pd.DataFrame:
    """
    For each entity (CIK), scores every pair of consecutive monthly snapshots.
    Returns a DataFrame of all pairwise scores.
    """
    df = df.sort_values(["cik", "month"]).reset_index(drop=True)
    results = []

    for cik, group in df.groupby("cik"):
        group = group.sort_values("month").reset_index(drop=True)

        for i in range(len(group) - 1):
            row_a = group.iloc[i]
            row_b = group.iloc[i + 1]

            scores = hybrid_score(
                name_a=row_a["entity_name"], name_b=row_b["entity_name"],
                rev_a=row_a["revenue_usd"],  rev_b=row_b["revenue_usd"],
            )

            results.append({
                "cik":          cik,
                "month_from":   row_a["month"],
                "month_to":     row_b["month"],
                "name_from":    row_a["entity_name"],
                "name_to":      row_b["entity_name"],
                "revenue_from": row_a["revenue_usd"],
                "revenue_to":   row_b["revenue_usd"],
                "ground_truth": row_b["drift_type"],
                **scores,
            })

    return pd.DataFrame(results)


# ── Validation ────────────────────────────────────────────────────────────────
def validate(results: pd.DataFrame):
    """
    Validates algorithm performance against ground truth drift labels.
    Drift events should be flagged as REVIEW, NEW_ENTITY, or CONFLICTS_WITH.
    """
    drift_rows  = results[results["ground_truth"] != "none"]
    stable_rows = results[results["ground_truth"] == "none"]

    print("\n── Validation ───────────────────────────────────────────────")
    print(f"Total pairs scored    : {len(results)}")
    print(f"  Drift event pairs   : {len(drift_rows)}")
    print(f"  Stable pairs        : {len(stable_rows)}")

    if len(drift_rows) > 0:
        print(f"\nDrift events — did the algorithm flag them?")
        print(drift_rows[[
            "cik", "month_from", "month_to", "name_from", "name_to",
            "name_score", "revenue_score", "hybrid_score", "outcome", "conflict_type"
        ]].to_string(index=False))

    print(f"\nOutcome distribution:")
    print(results["outcome"].value_counts().to_string())

    print(f"\nConflict type distribution:")
    print(results["conflict_type"].value_counts().to_string())

    if len(drift_rows) > 0:
        flagged = drift_rows[
            (drift_rows["conflict"] == True) |
            (drift_rows["outcome"] != "AUTO_RESOLVE")
        ]
        detection_rate = len(flagged) / len(drift_rows) * 100
        print(f"\nDrift detection rate  : {detection_rate:.0f}% ({len(flagged)}/{len(drift_rows)} drift events flagged)")


# ── Run ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    df = pd.read_csv(DATA_DIR / "entity_snapshots.csv")
    print(f"Loaded {len(df)} snapshots across {df['cik'].nunique()} entities")

    results = run_resolution(df)
    results.to_csv(DATA_DIR / "resolution_scores.csv", index=False)
    print(f"Scored {len(results)} consecutive pairs → data/resolution_scores.csv")

    validate(results)
