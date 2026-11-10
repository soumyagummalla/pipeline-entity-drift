"""
Build Entity Snapshots
======================
Turns the raw EDGAR pull into one EntitySnapshot row per company per quarter,
with the name each company was using at that time and a ground-truth label for
every snapshot where the name changed.

Ground truth:
  A snapshot is labeled drift_type='rename' when its name differs from the
  previous snapshot's name for the same CIK. This catches every documented
  rename, even when the rename date falls between quarter-ends.

  rename_kind separates the two kinds of rename:
  - cosmetic     → same name after normalization ("NIKE INC" → "NIKE, Inc.")
  - substantive  → a real name change ("Square, Inc." → "Block, Inc.")

Input:  data/entities_raw.csv, data/revenue_snapshots.csv   (from edgar_pull.py)
Output: data/entity_snapshots.csv

Usage:
    python build_snapshots.py
"""

import json
import pandas as pd
from pathlib import Path

from resolution_algorithm import normalize_name

DATA_DIR = Path("data")


def name_for_month(month: str, former: list[dict], current_name: str) -> str:
    """
    Returns the name in effect at the end of `month` (YYYY-MM).
    `former` is sorted by date_changed; each former name applies until the month
    it was changed.
    """
    for record in former:
        if month < record["date_changed"]:
            return record["name"]
    return current_name


def build_snapshots(entities: pd.DataFrame, revenue: pd.DataFrame) -> pd.DataFrame:
    entities = entities.copy()
    revenue = revenue.copy()
    entities["cik"] = entities["cik"].astype(str).str.zfill(10)
    revenue["cik"] = revenue["cik"].astype(str).str.zfill(10)

    # Snapshots are keyed by the month the quarter ends in (YYYY-MM).
    # Revenue is already discrete quarterly revenue (see edgar_pull.py).
    revenue["month"] = pd.to_datetime(revenue["period"]).dt.to_period("M").astype(str)
    revenue = (
        revenue.sort_values(["cik", "month"])
        .groupby(["cik", "month"], as_index=False)["revenue_usd"]
        .last()
    )

    rows = []
    snapshot_id = 1
    for _, entity in entities.iterrows():
        cik = entity["cik"]
        history = json.loads(entity["name_history"])
        former = sorted(
            (n for n in history if not n["is_current"] and n["date_changed"]),
            key=lambda n: n["date_changed"],
        )

        rev = revenue[revenue["cik"] == cik]
        previous_name = None
        for _, rev_row in rev.iterrows():
            name = name_for_month(rev_row["month"], former, entity["current_name"])

            drift_type, rename_kind = "none", ""
            if previous_name is not None and name != previous_name:
                drift_type = "rename"
                same_after_normalizing = normalize_name(name) == normalize_name(previous_name)
                rename_kind = "cosmetic" if same_after_normalizing else "substantive"

            rows.append({
                "snapshot_id":   f"S{snapshot_id:05d}",
                "cik":           cik,
                "month":         rev_row["month"],
                "entity_name":   name,
                "revenue_usd":   rev_row["revenue_usd"],
                "drift_type":    drift_type,
                "rename_kind":   rename_kind,
                "canonical_cik": cik,
            })
            snapshot_id += 1
            previous_name = name

    return pd.DataFrame(rows)


if __name__ == "__main__":
    entities = pd.read_csv(DATA_DIR / "entities_raw.csv", dtype={"cik": str})
    revenue = pd.read_csv(DATA_DIR / "revenue_snapshots.csv", dtype={"cik": str})

    df = build_snapshots(entities, revenue)
    df.to_csv(DATA_DIR / "entity_snapshots.csv", index=False)

    renames = df[df["drift_type"] == "rename"]
    print(f"Snapshots         : {len(df)}")
    print(f"Entities          : {df['cik'].nunique()}")
    print(f"Quarter range     : {df['month'].min()} → {df['month'].max()}")
    print(f"Renames           : {len(renames)}")
    print(f"  substantive     : {(renames['rename_kind'] == 'substantive').sum()}")
    print(f"  cosmetic        : {(renames['rename_kind'] == 'cosmetic').sum()}")
    print("\nSaved → data/entity_snapshots.csv")
