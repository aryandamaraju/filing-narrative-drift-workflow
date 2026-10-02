import requests
from datetime import date

HEADERS = {
    "User-Agent": "Khushi Kotti khushikotti9@gmail.com"
}

def get_company_facts(cik):
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    response = requests.get(url, headers=HEADERS)
    response.raise_for_status()
    return response.json()

def get_metric(data, tag, unit="USD"):
    try:
        return data["facts"]["us-gaap"][tag]["units"][unit]
    except KeyError:
        return []

def is_quarterly(entry):
    start = date.fromisoformat(entry["start"])
    end = date.fromisoformat(entry["end"])
    days = (end - start).days
    return 80 <= days <= 100

def dedupe_by_period(entries):
    seen = {}
    for e in entries:
        key = (e["start"], e["end"])
        if key not in seen or e.get("filed", "") > seen[key].get("filed", ""):
            seen[key] = e
    return sorted(seen.values(), key=lambda x: x["end"])

def calendar_label(entry):
    end = date.fromisoformat(entry["end"])
    quarter = (end.month - 1) // 3 + 1
    return f"CY{end.year} Q{quarter}"

def clean_series(data, tag):
    """Full pipeline: pull a metric, keep quarterly only, dedupe, sort."""
    raw = get_metric(data, tag)
    quarterly = [e for e in raw if is_quarterly(e)]
    return dedupe_by_period(quarterly)

def clean_series_revenue(data):
    """Revenue specifically: different filers use different XBRL tags depending
    on when they adopted ASC 606 and which taxonomy version their filer
    software defaults to. Try each known tag in order, use the first one
    that actually returns data, so we never silently fall back to a tag
    with only stale pre-2018 entries."""
    candidate_tags = [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "Revenues",
    ]
    for tag in candidate_tags:
        result = clean_series(data, tag)
        if result:
            return result
    return []

if __name__ == "__main__":
    adobe_cik = "0000796343"
    data = get_company_facts(adobe_cik)

    print("Company name:", data.get("entityName"))
    print()

    revenue = clean_series_revenue(data)
    gross_profit = clean_series(data, "GrossProfit")

    gp_by_end = {e["end"]: e["val"] for e in gross_profit}

    print(f"{'Period':<10} {'End':<12} {'Revenue':>16} {'Gross Profit':>16} {'Gross Margin':>14}")
    print("-" * 72)
    for entry in revenue[-6:]:
        label = calendar_label(entry)
        rev_val = entry["val"]
        gp_val = gp_by_end.get(entry["end"])
        if gp_val:
            margin = gp_val / rev_val * 100
            print(f"{label:<10} {entry['end']:<12} ${rev_val:>14,} ${gp_val:>14,} {margin:>12.1f}%")
        else:
            print(f"{label:<10} {entry['end']:<12} ${rev_val:>14,} {'N/A':>15} {'N/A':>14}")
