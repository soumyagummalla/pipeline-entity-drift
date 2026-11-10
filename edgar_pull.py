"""
SEC EDGAR Entity Drift Data Pull
=================================
Pulls companies with documented name changes from SEC EDGAR
and their quarterly revenue snapshots for the demo dataset.

Usage:
    python edgar_pull.py

Outputs:
    data/entities_raw.csv        — companies with documented name changes
    data/revenue_snapshots.csv   — quarterly revenue per company

Requirements:
    pip install requests pandas

SEC API Notes:
    - No API key required
    - Rate limit: 10 requests/sec (script stays well under this)
    - User-Agent header required — update with your email below
"""

import requests
import json
import time
import pandas as pd
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
# SEC requires a User-Agent header identifying your application
# Update this with your name/email before running
HEADERS = {"User-Agent": "pipeline-entity-drift your@email.com"}

OUTPUT_DIR = Path("data")
OUTPUT_DIR.mkdir(exist_ok=True)

# Number of companies to scan (EDGAR has 10,000+ filers — start with 300)
MAX_COMPANIES = 300


# ── Step 1: Pull full company list ────────────────────────────────────────────
def fetch_company_list():
    """
    Downloads the full EDGAR company ticker JSON.
    Contains CIK, name, ticker for all public filers.
    """
    print("Fetching company list from EDGAR...")
    url = "https://www.sec.gov/files/company_tickers.json"
    r = requests.get(url, headers=HEADERS)
    r.raise_for_status()
    data = r.json()
    companies = [
        {"cik": str(v["cik_str"]).zfill(10), "name": v["title"], "ticker": v["ticker"]}
        for v in data.values()
    ]
    print(f"  Found {len(companies)} companies")
    return companies


# ── Step 2: Fetch submission metadata per company ─────────────────────────────
def fetch_submission(cik: str) -> dict | None:
    """
    Fetches the submissions JSON for a single CIK.
    Contains formerNames — the documented name change history.

    Note: The rename date is stored under the key 'to', not 'date'.
    Example: {'name': 'Facebook Inc', 'from': '...', 'to': '2021-10-27T...'}
    """
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  Warning: could not fetch CIK {cik}: {e}")
        return None


# ── Step 3: Fetch quarterly revenue ───────────────────────────────────────────
# Revenue tags in priority order. Companies switched tags when ASC 606 took
# effect (2018), so one company can need several tags to cover its full history
# (Facebook moved from "Revenues" to "RevenueFromContractWithCustomer...").
REVENUE_KEYS = [
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "SalesRevenueNet",
    "RevenueFromContractWithCustomer",
]

# Fact durations in days. 10-Q filings report both the 3-month quarter and the
# year-to-date total (6 or 9 months) with the same end date, and 10-K filings
# report only the full year. Only 3-month values are comparable quarter to quarter.
QUARTER_DAYS = (80, 100)
NINE_MONTH_DAYS = (260, 290)
ANNUAL_DAYS = (350, 380)


def _duration(start: str, end: str) -> int:
    return (pd.Timestamp(end) - pd.Timestamp(start)).days


def _in_range(days: int, bounds: tuple) -> bool:
    return bounds[0] <= days <= bounds[1]


def quarterly_from_facts(units: list[dict]) -> dict:
    """
    Turns one tag's XBRL facts into discrete quarterly revenue, keyed by period end.

    - Keeps only 3-month facts (drops 6-month and 9-month year-to-date totals).
    - Derives Q4, which companies never report on its own: Q4 = full year - 9-month YTD,
      or full year - the three reported quarters when no 9-month figure exists.
    - When the same period was reported more than once, the latest filing wins
      (restatements).
    """
    latest = {}
    for u in units:
        if u.get("form") not in ("10-Q", "10-K", "10-Q/A", "10-K/A"):
            continue
        if not u.get("start") or u.get("val", 0) <= 0:
            continue
        key = (u["start"], u["end"])
        if key not in latest or u.get("filed", "") > latest[key].get("filed", ""):
            latest[key] = u

    quarters, nine_month, annual = {}, {}, []
    for (start, end), u in latest.items():
        days = _duration(start, end)
        if _in_range(days, QUARTER_DAYS):
            quarters[end] = {"start": start, "val": u["val"], "derived": False}
        elif _in_range(days, NINE_MONTH_DAYS):
            nine_month[(start, end)] = u["val"]
        elif _in_range(days, ANNUAL_DAYS):
            annual.append((start, end, u["val"]))

    for fy_start, fy_end, fy_val in annual:
        if fy_end in quarters:
            continue
        q4 = None
        # Option 1: full year minus the 9-month YTD that starts on the same day
        for (s, e), val in nine_month.items():
            if s == fy_start and _in_range(_duration(e, fy_end), QUARTER_DAYS):
                q4, q4_start = fy_val - val, e
                break
        # Option 2: full year minus the three reported quarters inside the year
        if q4 is None:
            inside = sorted(
                (e, q) for e, q in quarters.items()
                if q["start"] >= fy_start and e < fy_end
            )
            if len(inside) == 3:
                q4 = fy_val - sum(q["val"] for _, q in inside)
                q4_start = inside[-1][0]
        if q4 is not None and q4 > 0:
            quarters[fy_end] = {"start": q4_start, "val": q4, "derived": True}

    return quarters


def fetch_revenue(cik: str) -> list[dict]:
    """
    Fetches XBRL company facts for a CIK and returns discrete quarterly revenue.

    Merges all revenue tags, because companies switch tags over time. When two tags
    cover the same quarter, the tag listed first in REVENUE_KEYS wins.
    """
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        us_gaap = r.json().get("facts", {}).get("us-gaap", {})
    except Exception:
        return []

    merged = {}
    for key in REVENUE_KEYS:
        units = us_gaap.get(key, {}).get("units", {}).get("USD", [])
        for end, q in quarterly_from_facts(units).items():
            if end not in merged:  # earlier (higher-priority) tag already covers it
                merged[end] = {**q, "tag": key}

    return [
        {
            "cik": cik,
            "period": end,
            "period_start": q["start"],
            "revenue_usd": q["val"],
            "q4_derived": q["derived"],
            "source_tag": q["tag"],
        }
        for end, q in sorted(merged.items())
    ]


# ── Step 4: Main pipeline ──────────────────────────────────────────────────────
def main():
    companies = fetch_company_list()

    entities = []
    all_revenue = []

    print(f"\nScanning up to {MAX_COMPANIES} companies for name changes...")
    processed = 0

    for company in companies:
        if processed >= MAX_COMPANIES:
            break

        cik = company["cik"]
        sub = fetch_submission(cik)
        if not sub:
            continue

        former_names = sub.get("formerNames", [])

        # Only keep companies with at least one documented name change
        if not former_names:
            time.sleep(0.1)
            continue

        current_name = sub.get("name", company["name"])

        # Build name history — note: rename date is stored under 'to' key
        name_history = [
            {
                "cik": cik,
                "name": fn["name"],
                "date_changed": fn.get("to", "")[:7],  # slice to YYYY-MM
                "is_current": False
            }
            for fn in former_names
        ]
        name_history.append(
            {"cik": cik, "name": current_name, "date_changed": None, "is_current": True}
        )

        entities.append({
            "cik": cik,
            "current_name": current_name,
            "ticker": company["ticker"],
            "sic": sub.get("sic", ""),
            "sic_description": sub.get("sicDescription", ""),
            "former_names": json.dumps([fn["name"] for fn in former_names]),
            "name_change_count": len(former_names),
            "name_history": json.dumps(name_history),
        })

        revenue_records = fetch_revenue(cik)
        all_revenue.extend(revenue_records)

        processed += 1
        if processed % 10 == 0:
            print(f"  {processed} companies with name changes found so far...")

        time.sleep(0.15)  # Stay under SEC rate limit

    # ── Save outputs ──────────────────────────────────────────────────────────
    entities_df = pd.DataFrame(entities)
    entities_df.to_csv(OUTPUT_DIR / "entities_raw.csv", index=False)
    print(f"\nSaved {len(entities_df)} entities → data/entities_raw.csv")

    if all_revenue:
        revenue_df = pd.DataFrame(all_revenue)
        revenue_df["period"] = pd.to_datetime(revenue_df["period"])
        revenue_df = revenue_df.sort_values(["cik", "period"])
        revenue_df.to_csv(OUTPUT_DIR / "revenue_snapshots.csv", index=False)
        print(f"Saved {len(revenue_df)} revenue records → data/revenue_snapshots.csv")
    else:
        print("No revenue data pulled (check XBRL availability for selected companies)")

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n── Summary ──────────────────────────────────────────────────")
    print(f"Companies with name changes : {len(entities_df)}")
    if not entities_df.empty:
        print(f"Avg name changes per company: {entities_df['name_change_count'].mean():.1f}")
        print(f"Max name changes            : {entities_df['name_change_count'].max()}")
        print("\nSample companies pulled:")
        print(entities_df[["current_name", "name_change_count"]].head(10).to_string(index=False))


if __name__ == "__main__":
    main()
