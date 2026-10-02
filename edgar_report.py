import os
import re
import json
import requests
from datetime import date
from dotenv import load_dotenv
from anthropic import Anthropic
from docx import Document
from docx.shared import RGBColor
import edgar_pull
from edgar_sections import get_clean_mdna
from edgar_diff import split_sentences, diff_sentences_filtered
from edgar_pull import calendar_label

load_dotenv()
client = Anthropic()

HEADERS = {
    "User-Agent": "Khushi Kotti khushikotti9@gmail.com"
}

def get_cik_from_ticker(ticker):
    url = "https://www.sec.gov/files/company_tickers.json"
    response = requests.get(url, headers=HEADERS)
    response.raise_for_status()
    data = response.json()
    ticker = ticker.upper().strip()
    for entry in data.values():
        if entry["ticker"] == ticker:
            return str(entry["cik_str"]).zfill(10), entry["title"]
    return None, None

def get_two_most_recent_filings(cik):
    """Return the two most recent filings of the SAME type (10-Q vs 10-Q,
    or 10-K vs 10-K), never a mismatched 10-K/10-Q pair."""
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    response = requests.get(url, headers=HEADERS)
    response.raise_for_status()
    data = response.json()
    recent = data["filings"]["recent"]

    all_filings = []
    for i in range(len(recent["form"])):
        if recent["form"][i] in ("10-K", "10-Q"):
            all_filings.append({
                "form": recent["form"][i],
                "date": recent["filingDate"][i],
                "accession": recent["accessionNumber"][i],
                "primary_doc": recent["primaryDocument"][i],
            })

    if not all_filings:
        return []

    newest = all_filings[0]
    target_type = newest["form"]

    for candidate in all_filings[1:]:
        if candidate["form"] == target_type:
            return [newest, candidate]

    return all_filings[:2]

def normalize_number(num_str):
    cleaned = num_str.lower()
    cleaned = cleaned.replace("$", "").replace(",", "")
    cleaned = cleaned.replace("million", "").replace("billion", "")
    cleaned = cleaned.replace("m", "").replace("b", "").replace("%", "")
    return cleaned.strip()

def extract_numbers(text):
    dollars = re.findall(r"\$[\d,]+(?:\.\d+)?\s?(?:billion|million|B|M)?", text)
    percents = re.findall(r"\d+(?:\.\d+)?%", text)
    bare_numbers = re.findall(r"(?<![\$\d.])\b\d{1,3}(?:,\d{3})+(?:\.\d+)?\b", text)
    return set(d.strip() for d in dollars) | set(percents) | set(bare_numbers)

def verify_item(item, numeric_context, full_source_text):
    cited = extract_numbers(item.get("what_changed", "") + " " + item.get("numeric_check", ""))
    all_source_nums = extract_numbers(numeric_context) | extract_numbers(full_source_text)
    normalized_source = {normalize_number(n) for n in all_source_nums}

    not_in_source = [n for n in cited if normalize_number(n) not in normalized_source]
    is_flagged_inferred = item.get("stated_vs_inferred", "none").strip().lower() != "none"

    if not cited:
        return "No specific figures cited"
    elif not not_in_source:
        return "Verified — all cited figures appear directly in the filing text or numeric data provided"
    elif not_in_source and is_flagged_inferred:
        return f"Contains model-calculated figures ({', '.join(not_in_source)}) — derived, not directly stated; model flagged this as inferred"
    else:
        return f"CHECK MANUALLY — figures not traced to source and not flagged as inferred: {', '.join(not_in_source)}"

def build_prompt(new_sentences, removed_sentences, numeric_context, thesis):
    new_block = "\n".join(f"- {s}" for s in new_sentences)
    removed_block = "\n".join(f"- {s}" for s in removed_sentences)
    return f"""You are helping a long/short equity analyst review changes between two consecutive 
SEC filings.

NEW SENTENCES (in the newer filing, not in the prior one):
{new_block}

REMOVED SENTENCES (in the prior filing, not in the newer one):
{removed_block}

NUMERIC CONTEXT:
{numeric_context}

ANALYST'S THESIS:
{thesis}

Identify which changes are MATERIAL to the thesis or to understanding the business. Ignore 
tabular data dumps, exhibit/signature boilerplate, and routine repeated disclosures.

IMPORTANT MATH RULE: if NUMERIC_CONTEXT gives a "percentage of growth" or "percentage points 
of change" figure, that percentage applies to the CHANGE, not the current total. Only calculate 
a derived dollar figure if given both a prior and current base. If you don't have enough to 
calculate correctly, say so rather than guessing. Do not mix a dollar-ratio calculation with a 
percentage-point comparison in the same statement, keep the two separate and clearly labeled.

LENGTH RULE: limit to the 5 most material items. Keep each field to 1-2 sentences.

For each item, output:
- theme: short label
- materiality_score: integer 1-5, how much this should change what the analyst does next 
  (5 = re-underwrite the position now, 1 = worth noting, no action needed)
- what_changed: one sentence, naming specific figures where relevant
- ties_to_thesis: which assumption it touches, or "none"
- numeric_check: the specific number(s) involved and whether they confirm, contradict, or 
  are neutral. Show at most one simple calculation, no compound or mixed-unit math.
- stated_vs_inferred: flag any inference (including any number you calculated yourself 
  rather than quoting directly), or "none" if fully supported by source text
- action_item: one concrete next step
- forecast_impact: an object with four fields, or null if this item has no clear model 
  implication:
  - line_item: which forecast line this touches (e.g. "Revenue", "Gross Margin", 
    "Operating Expenses", "R&D Expense", "Segment Revenue"). Be specific to what 
    NUMERIC_CONTEXT or the filing text actually supports, don't invent a line item with 
    no basis.
  - direction: "increase", "decrease", or "reassess" (use "reassess" when the item raises 
    real uncertainty about a line without indicating a clear directional revision)
  - magnitude_estimate: a short phrase using only figures that are stated or directly 
    derivable from NUMERIC_CONTEXT or the filing text (e.g. "in line with the ~180bps 
    margin move already seen" or "not quantifiable from available data, needs analyst 
    follow-up"). Never invent a forecast number that isn't grounded in something stated.
  - confidence: "high" if the filing gives a specific stated figure driving this, "medium" 
    if it's a reasonable inference from stated data, "low" if it's mostly qualitative/risk 
    language with no numeric anchor

Also produce, as a separate top-level field alongside "items":
- pm_headline: ONE sentence, the single most important takeaway a portfolio manager 
  needs before market open, written the way an analyst would text their PM.

CRITICAL OUTPUT FORMAT: respond ONLY with valid JSON, structured EXACTLY as a single 
JSON object with two top-level keys, nothing else, no list at the top level:
{{
  "pm_headline": "one sentence here",
  "items": [ {{theme, materiality_score, what_changed, ties_to_thesis, numeric_check, stated_vs_inferred, action_item, forecast_impact}}, ... 5 items, sorted by materiality_score descending ]
}}"""

def write_report(company_name, period_new, period_old, pm_headline, items, output_path):
    doc = Document()
    doc.add_heading(f"{company_name} — Filing Narrative Drift Report", level=1)
    subtitle = doc.add_paragraph()
    subtitle.add_run(f"Comparing {period_new} vs {period_old}").italic = True

    if pm_headline:
        p = doc.add_paragraph()
        p.add_run("PM Headline: ").bold = True
        p.add_run(pm_headline)

    doc.add_paragraph()

    for item in items:
        score = item.get("materiality_score", "")
        doc.add_heading(f"{item['theme']}  (Materiality: {score}/5)", level=2)
        for label, key in [("What changed", "what_changed"), ("Ties to thesis", "ties_to_thesis"),
                            ("Numeric check", "numeric_check"), ("Stated vs. inferred", "stated_vs_inferred")]:
            p = doc.add_paragraph()
            p.add_run(f"{label}: ").bold = True
            p.add_run(str(item[key]))

        p = doc.add_paragraph()
        p.add_run("Verification: ").bold = True
        run = p.add_run(item["verification"])
        if item["verification"].startswith("CHECK MANUALLY"):
            run.font.color.rgb = RGBColor(0xC0, 0x00, 0x00)

        p = doc.add_paragraph()
        p.add_run("Action item: ").bold = True
        p.add_run(item["action_item"])
        doc.add_paragraph()

    # ---- Forecast Refresh table ----
    forecast_rows = []
    for item in items:
        fi = item.get("forecast_impact")
        if fi and isinstance(fi, dict) and fi.get("line_item"):
            forecast_rows.append((
                item.get("theme", ""),
                fi.get("line_item", ""),
                fi.get("direction", ""),
                fi.get("magnitude_estimate", ""),
                fi.get("confidence", ""),
            ))

    if forecast_rows:
        doc.add_heading("Forecast Refresh", level=2)
        p = doc.add_paragraph()
        p.add_run("What each finding above implies for the model.").italic = True

        table = doc.add_table(rows=1, cols=5)
        table.style = "Light Grid Accent 1"
        hdr = table.rows[0].cells
        for i, col_name in enumerate(["Theme", "Line Item", "Direction", "Magnitude", "Confidence"]):
            hdr[i].text = col_name

        for theme, line_item, direction, magnitude, confidence in forecast_rows:
            row = table.add_row().cells
            row[0].text = theme
            row[1].text = line_item
            row[2].text = direction
            row[3].text = magnitude
            row[4].text = confidence

    doc.save(output_path)

if __name__ == "__main__":
    ticker = input("Enter a company ticker (e.g. ADBE): ").strip()
    print(f"\nLooking up {ticker}...")
    cik, company_name = get_cik_from_ticker(ticker)
    if not cik:
        print("Ticker not found. Exiting.")
        exit()
    print(f"Found: {company_name} (CIK {cik})")

    print("\nFinding two most recent filings...")
    filings = get_two_most_recent_filings(cik)
    if len(filings) < 2:
        print("Could not find two filings to compare. Exiting.")
        exit()
    newer, older = filings[0], filings[1]
    print(f"  Newer: {newer['form']} filed {newer['date']}")
    print(f"  Older: {older['form']} filed {older['date']}")

    print("\nPulling revenue and margin data...")
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

    print("\nFetching and cleaning filing text...")
    mdna_new = get_clean_mdna(cik, newer["accession"], newer["primary_doc"])
    mdna_old = get_clean_mdna(cik, older["accession"], older["primary_doc"])
    if not mdna_new or not mdna_old:
        print("Could not extract MD&A from one or both filings. Exiting.")
        exit()

    print("Diffing filings...")
    old_sentences = split_sentences(mdna_old)
    new_sentences = split_sentences(mdna_new)
    added, removed, cosmetic_count = diff_sentences_filtered(old_sentences, new_sentences)
    print(f"  {len(added)} new, {len(removed)} removed, ~{cosmetic_count} cosmetic-only (filtered)")

    thesis = input("\nPaste a short thesis for this company (or press Enter to skip): ").strip()
    if not thesis:
        thesis = f"{company_name}: no specific thesis provided, flag any material business changes generally."

    print("\nRunning materiality analysis...")
    prompt = build_prompt(added, removed, numeric_context, thesis)
    response = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}]
    )

    raw_output = response.content[0].text
    clean = raw_output.strip().removeprefix("```json").removesuffix("```").strip()
    try:
        parsed = json.loads(clean)
        pm_headline = parsed.get("pm_headline", "") if isinstance(parsed, dict) else ""
        items = parsed.get("items", []) if isinstance(parsed, dict) else parsed
    except Exception as e:
        print(f"Could not parse model output as JSON: {e}")
        print(raw_output)
        exit()

    print(f"\nVerifying {len(items)} items against source data...")
    full_source_text = mdna_new + " " + mdna_old
    for item in items:
        item["verification"] = verify_item(item, numeric_context, full_source_text)

    print("\n=== SUMMARY ===")
    if pm_headline:
        print(f"\nPM Headline: {pm_headline}\n")
    for item in items:
        print(f"\n{item['theme']} (Materiality: {item.get('materiality_score', '?')}/5)")
        print(f"  {item['verification']}")

    import time
    output_dir = "reports"
    os.makedirs(output_dir, exist_ok=True)
    safe_ticker = ticker.upper()
    timestamp = time.strftime("%H%M%S")
    output_path = os.path.join(output_dir, f"{safe_ticker}_drift_report_{date.today().isoformat()}_{timestamp}.docx")

    write_report(company_name, f"{newer['form']} filed {newer['date']}",
                 f"{older['form']} filed {older['date']}", pm_headline, items, output_path)

    print(f"\nReport saved to: {output_path}")
