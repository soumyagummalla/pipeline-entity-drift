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
def fetch_revenue(cik: str) -> list[dict]:
    """
    Fetches XBRL company facts for a CIK.
    Extracts quarterly/annual revenue from 10-Q and 10-K filings.

    Tries multiple GAAP revenue field names since companies report differently.
    """
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        facts = r.json()

        us_gaap = facts.get("facts", {}).get("us-gaap", {})
        revenue_keys = [
            "Revenues",
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "SalesRevenueNet",
            "RevenueFromContractWithCustomer",
        ]

        for key in revenue_keys:
            if key in us_gaap:
                units = us_gaap[key].get("units", {}).get("USD", [])
                quarterly = [
                    {
                        "cik": cik,
                        "period": u["end"],
                        "revenue_usd": u["val"],
                        "form": u.get("form", ""),
                        "filed": u.get("filed", ""),
                    }
                    for u in units
                    if u.get("form") in ("10-Q", "10-K") and u.get("val", 0) > 0
                ]
                if quarterly:
                    return quarterly
        return []
    except Exception:
        return []


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
