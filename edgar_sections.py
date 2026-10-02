import requests
import warnings
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

HEADERS = {
    "User-Agent": "Khushi Kotti khushikotti9@gmail.com"
}

def get_filing_text(cik, accession_number, primary_doc):
    accession_nodash = accession_number.replace("-", "")
    url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession_nodash}/{primary_doc}"
    response = requests.get(url, headers=HEADERS)
    response.raise_for_status()
    return response.text, url

def html_to_text(html):
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style"]):
        tag.decompose()
    for tag in soup.find_all(["ix:header", "ix:hidden", "ix:references", "ix:resources"]):
        tag.decompose()
    for tag in soup.find_all(style=True):
        try:
            style_val = tag.get("style") if hasattr(tag, "get") else None
        except Exception:
            style_val = None
        if not style_val:
            continue
        if isinstance(style_val, list):
            style_val = " ".join(str(v) for v in style_val)
        try:
            if "display:none" in str(style_val).replace(" ", "").lower():
                tag.decompose()
        except Exception:
            continue
    text = soup.get_text(separator="\n")
    lines = [line.strip() for line in text.split("\n")]
    lines = [line for line in lines if line]
    return "\n".join(lines)

def find_all_occurrences(text, marker):
    """Return every index where marker appears (case-insensitive)."""
    lower_text = text.lower()
    lower_marker = marker.lower()
    positions = []
    idx = -1
    while True:
        idx = lower_text.find(lower_marker, idx + 1)
        if idx == -1:
            break
        positions.append(idx)
    return positions

def extract_section(text, start_markers, end_markers, min_content_length=2000):
    """Find a section's real content, not its table-of-contents mention.

    A heading like 'Management's Discussion and Analysis' can appear multiple
    times in a filing: once (or more) in the table of contents, and once as
    the actual section start. Different filers structure their table of
    contents differently, so we can't assume a fixed number of occurrences
    to skip. Instead, we try every occurrence of every marker, compute how
    much text would fall between it and the nearest end marker, and accept
    the first candidate that's long enough to plausibly be real content
    rather than a one-line index entry. If nothing clears the bar, we fall
    back to whichever candidate produced the most text, best effort rather
    than failing silently.
    """
    lower_text = text.lower()
    candidates = []

    for start_marker in start_markers:
        for start_idx in find_all_occurrences(text, start_marker):
            end_idx = len(text)
            for end_marker in end_markers:
                idx = lower_text.find(end_marker.lower(), start_idx + 200)
                if idx != -1 and idx < end_idx:
                    end_idx = idx
            section_length = end_idx - start_idx
            candidates.append((start_idx, end_idx, section_length))

    if not candidates:
        return None

    # prefer the first (earliest) candidate that's clearly real content
    for start_idx, end_idx, length in candidates:
        if length >= min_content_length:
            return text[start_idx:end_idx].strip()

    # nothing hit the length bar; fall back to the longest candidate found
    # rather than returning a near-empty table-of-contents fragment
    best = max(candidates, key=lambda c: c[2])
    return text[best[0]:best[1]].strip()

def strip_boilerplate(mdna_text, cut_marker="BUSINESS OVERVIEW"):
    idx = mdna_text.find(cut_marker)
    if idx != -1:
        return mdna_text[idx:].strip()
    return mdna_text

def get_clean_mdna(cik, accession_number, primary_doc):
    """Full pipeline: fetch a filing and return its cleaned, trimmed MD&A text."""
    html, url = get_filing_text(cik, accession_number, primary_doc)
    text = html_to_text(html)
    mdna = extract_section(
        text,
        start_markers=[
            "Management’s Discussion and Analysis of Financial Condition",
            "Management's Discussion and Analysis of Financial Condition",
            "Management’s Discussion and Analysis (MD&A)",
            "Management's Discussion and Analysis (MD&A)",
            "Management’s Discussion and Analysis",
            "Management's Discussion and Analysis",
        ],
        end_markers=[
            "Item 3. Quantitative and Qualitative Disclosures",
            "Item 3.\nQuantitative",
            "Item 7A.",
            "Item 3 —",
        ],
        min_content_length=2000
    )
    if mdna is None:
        return None
    return strip_boilerplate(mdna)

if __name__ == "__main__":
    adobe_cik = "0000796343"
    filings = {
        "CY2026 Q2": ("0000796343-26-000112", "adbe-20260529.htm"),
        "CY2026 Q1": ("0000796343-26-000056", "adbe-20260227.htm"),
    }
    results = {}
    for label, (accession, doc) in filings.items():
        print(f"Fetching {label}...")
        mdna = get_clean_mdna(adobe_cik, accession, doc)
        if mdna:
            results[label] = mdna
            print(f"  -> {len(mdna)} characters")
        else:
            print(f"  -> FAILED to extract MD&A")
        print()
    print("Summary:")
    for label, text in results.items():
        print(f"  {label}: {len(text)} chars, starts with: {text[:80]!r}")
