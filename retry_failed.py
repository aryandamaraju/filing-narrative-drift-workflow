import os
import json
import time
import requests
from datetime import date
from dotenv import load_dotenv
from anthropic import Anthropic
import edgar_pull
from edgar_sections import get_clean_mdna
from edgar_diff import split_sentences, diff_sentences_filtered
from edgar_pull import calendar_label
from edgar_report import (
    get_cik_from_ticker, get_two_most_recent_filings,
    verify_item, build_prompt, write_report, HEADERS
)

load_dotenv()
client = Anthropic()

TICKERS = ["INTC", "NOW", "PANW"]

GENERIC_THESIS = "No specific thesis provided, flag any material business or financial changes generally."

def run_one(ticker):
    cik, company_name = get_cik_from_ticker(ticker)
    if not cik:
        return "FAILED", "Ticker not found"

    filings = get_two_most_recent_filings(cik)
    if len(filings) < 2:
        return "FAILED", "Could not find two comparable filings"
    newer, older = filings[0], filings[1]

    facts_url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    facts_response = requests.get(facts_url, headers=HEADERS)
    facts_response.raise_for_status()
    facts_data = facts_response.json()

    revenue_series = edgar_pull.clean_series_revenue(facts_data)
    margin_series = edgar_pull.clean_series(facts_data, "GrossProfit")
    margin_by_end = {e["end"]: e["val"] for e in margin_series}

    numeric_lines = []
    for entry in revenue_series[-6:]:
        label = calendar_label(entry)
        rev_val = entry["val"]
        gp_val = margin_by_end.get(entry["end"])
        if gp_val:
            margin_pct = gp_val / rev_val * 100
            numeric_lines.append(f"{label}: Revenue ${rev_val:,}, Gross Margin {margin_pct:.1f}%")
    numeric_context = "Revenue and gross margin trend (most recent quarters):\n" + "\n".join(numeric_lines)
    if not numeric_lines:
        numeric_context = "No standard revenue/gross margin data available for this filer."

    mdna_new = get_clean_mdna(cik, newer["accession"], newer["primary_doc"])
    mdna_old = get_clean_mdna(cik, older["accession"], older["primary_doc"])
    if not mdna_new or not mdna_old:
        return "FAILED", "Could not extract MD&A from one or both filings"

    old_sentences = split_sentences(mdna_old)
    new_sentences = split_sentences(mdna_new)
    added, removed, cosmetic_count = diff_sentences_filtered(old_sentences, new_sentences)

    prompt = build_prompt(added, removed, numeric_context, f"{company_name}: {GENERIC_THESIS}")
    response = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}]
    )
    raw_output = response.content[0].text
    clean = raw_output.strip().removeprefix("```json").removesuffix("```").strip()
    items = json.loads(clean)

    full_source_text = mdna_new + " " + mdna_old
    for item in items:
        item["verification"] = verify_item(item, numeric_context, full_source_text)

    output_dir = "reports"
    os.makedirs(output_dir, exist_ok=True)
    timestamp = time.strftime("%H%M%S")
    output_path = os.path.join(output_dir, f"{ticker}_drift_report_{date.today().isoformat()}_{timestamp}.docx")
    write_report(company_name, f"{newer['form']} filed {newer['date']}",
                 f"{older['form']} filed {older['date']}", items, output_path)

    return "OK", f"{len(items)} items -> {output_path}"

if __name__ == "__main__":
    results = []
    print(f"Retrying {len(TICKERS)} tickers...\n")

    for ticker in TICKERS:
        print(f"[{ticker}] Starting...")
        try:
            status, detail = run_one(ticker)
            print(f"[{ticker}] {status}: {detail}")
            results.append((ticker, status, detail))
        except Exception as e:
            print(f"[{ticker}] FAILED: {type(e).__name__}: {e}")
            results.append((ticker, "FAILED", f"{type(e).__name__}: {e}"))
        print()
        time.sleep(1)

    print("=" * 60)
    print("RETRY SUMMARY")
    print("=" * 60)
    ok_count = sum(1 for _, s, _ in results if s == "OK")
    print(f"{ok_count}/{len(results)} succeeded\n")
    for ticker, status, detail in results:
        marker = "✓" if status == "OK" else "✗"
        print(f"  {marker} {ticker:<6} {status:<8} {detail}")
