import re
import json
import requests
import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from anthropic import Anthropic
import edgar_pull
from edgar_sections import get_clean_mdna
from edgar_diff import split_sentences, diff_sentences_filtered
from edgar_pull import calendar_label
from edgar_report import (
    get_cik_from_ticker, get_two_most_recent_filings,
    verify_item, build_prompt, HEADERS
)

load_dotenv()
client = Anthropic()

st.set_page_config(page_title="Filing Narrative Drift", layout="centered")

st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=EB+Garamond:wght@400;500;600&display=swap');
    html, body, [class*="css"] { font-family: 'EB Garamond', Georgia, serif; color: #111111; }
    p, li, .stMarkdown, div[data-testid="stMarkdownContainer"] { font-family: 'EB Garamond', Georgia, serif !important; color: #111111 !important; }
    [class*="material-icons"], [class*="material-symbols"], span[data-testid="stIconMaterial"] { font-family: 'Material Symbols Rounded', 'Material Icons' !important; }
    .block-container { background-color: #FFFFFF; max-width: 800px; padding-top: 2.5rem; }
    h1, h2, h3, h4 { font-family: 'EB Garamond', Georgia, serif !important; color: #1E3A8A !important; font-weight: 600 !important; }
    h1 { font-size: 2.2rem !important; }
    label { font-family: 'EB Garamond', Georgia, serif !important; color: #1E3A8A !important; font-weight: 600 !important; font-size: 0.95rem !important; }
    .stTextInput input, .stTextArea textarea {
        font-family: 'EB Garamond', Georgia, serif !important; background-color: #FFFFFF !important;
        color: #111111 !important; border: 1px solid #C9CBD1 !important; border-radius: 4px !important;
    }
    .stButton>button { font-family: 'EB Garamond', Georgia, serif !important; background-color: #1E3A8A !important;
        color: #FFFFFF !important; border-radius: 4px; font-weight: 600; padding: 0.55rem 1.6rem; border: none; }
    .stButton>button:hover { background-color: #16296A !important; }
    div[data-testid="stVerticalBlockBorderWrapper"] { background-color: #FFFFFF !important; border: 1px solid #D8D9DE !important; border-radius: 6px !important; }
    div[data-testid="stExpander"] { background-color: #FFFFFF !important; border: 1px solid #D8D9DE !important; border-radius: 6px !important; }
    hr { border-color: #D8D9DE !important; }
    .score-pill { display:inline-block; font-size:0.72rem; font-weight:600; padding:2px 9px; border-radius:10px; margin-right:6px; font-family:'EB Garamond', Georgia, serif; }
    .stTabs [data-baseweb="tab"] { font-family: 'EB Garamond', Georgia, serif !important; font-size: 1.05rem !important; color: #1E3A8A !important; }
</style>
""", unsafe_allow_html=True)

st.markdown('<div style="font-size:0.78rem;font-weight:600;letter-spacing:0.12em;text-transform:uppercase;color:#1E3A8A;">The Drift Desk</div>', unsafe_allow_html=True)
st.title("Filing Narrative Drift & Materiality Flagging")
st.markdown('<div style="font-size:1.05rem;color:#333;margin:-8px 0 22px 0;line-height:1.5;border-left:3px solid #1E3A8A;padding-left:14px;">Turns quarter-over-quarter filing language into thesis-tested, numerically verified signals — in minutes, not hours.</div>', unsafe_allow_html=True)

SCORE_COLORS = {5: ("#FDE8E8", "#B91C1C"), 4: ("#FEF0E0", "#C2540A"), 3: ("#FEF9E0", "#946800"),
                 2: ("#E8F0FE", "#1E4FA3"), 1: ("#EDEDED", "#555555")}

def score_pill(score):
    try:
        s = int(score)
    except (ValueError, TypeError):
        return ""
    bg, fg = SCORE_COLORS.get(s, ("#EDEDED", "#555555"))
    return f'<span class="score-pill" style="background:{bg};color:{fg};">Materiality {s}/5</span>'

def esc(text):
    return str(text).replace("$", "\\$")

# shares outstanding and capex removed entirely: too inconsistently tagged
# across filers to be reliable. Charts that come back empty for a given
# company are skipped silently rather than shown with a "not available" note.
METRICS = {
    "Revenue ($B)": {"kind": "revenue", "chart": "bar"},
    "Revenue YoY Growth (%)": {"kind": "revenue_growth", "chart": "line"},
    "Gross Margin (%)": {"kind": "margin", "chart": "line"},
    "R&D Expense ($B)": {"kind": "tag", "tag": "ResearchAndDevelopmentExpense", "chart": "bar"},
    "Net Income ($B)": {"kind": "tag", "tag": "NetIncomeLoss", "chart": "bar"},
    "Operating Income ($B)": {"kind": "tag", "tag": "OperatingIncomeLoss", "chart": "bar"},
}

@st.cache_data(ttl=3600, show_spinner=False)
def fetch_facts(cik):
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    r = requests.get(url, headers=HEADERS)
    r.raise_for_status()
    return r.json()

def get_metric_dict(facts_data, metric_label, window=8):
    spec = METRICS[metric_label]
    result = {}
    if spec["kind"] == "revenue":
        series = edgar_pull.clean_series_revenue(facts_data)
        for e in series[-window:]:
            result[calendar_label(e)] = round(e["val"] / 1e9, 2)
    elif spec["kind"] == "revenue_growth":
        series = edgar_pull.clean_series_revenue(facts_data)
        rows = []
        for i in range(4, len(series)):
            cur, prior = series[i], series[i - 4]
            growth = (cur["val"] - prior["val"]) / prior["val"] * 100
            rows.append((calendar_label(cur), round(growth, 1)))
        for label, val in rows[-window:]:
            result[label] = val
    elif spec["kind"] == "margin":
        revenue = edgar_pull.clean_series_revenue(facts_data)
        margin = edgar_pull.clean_series(facts_data, "GrossProfit")
        margin_by_end = {e["end"]: e["val"] for e in margin}
        for e in revenue[-window:]:
            gp = margin_by_end.get(e["end"])
            if gp:
                result[calendar_label(e)] = round(gp / e["val"] * 100, 1)
    elif spec["kind"] == "tag":
        series = edgar_pull.clean_series(facts_data, spec["tag"])
        for e in series[-window:]:
            result[calendar_label(e)] = round(e["val"] / 1e9, 2)
    return result

if "last_ticker" not in st.session_state:
    st.session_state["last_ticker"] = ""
if "last_company_name" not in st.session_state:
    st.session_state["last_company_name"] = ""

tab1, tab2 = st.tabs(["Thesis Analysis", "Charts"])

# =========================== TAB 1: thesis analysis ===========================
with tab1:
    st.caption("Diffs a company's two most recent 10-K/10-Q filings, checks language changes against the numbers, and flags what's actually material to your thesis.")

    ticker = st.text_input("Company ticker", placeholder="e.g. ADBE, AAPL")
    thesis = st.text_area("Your thesis (optional)", placeholder="e.g. Adobe, Long. Core assumptions: (1) pricing power holds, ...", height=100)

    run = st.button("Run analysis", type="primary")

    if run:
        if not ticker.strip():
            st.error("Enter a ticker first.")
            st.stop()

        with st.spinner("Looking up company..."):
            cik, company_name = get_cik_from_ticker(ticker)
        if not cik:
            st.error("Ticker not found.")
            st.stop()

        st.session_state["last_ticker"] = ticker.strip().upper()
        st.session_state["last_company_name"] = company_name

        st.success(f"Found: {company_name} (CIK {cik})")

        with st.spinner("Locating the two most recent comparable filings..."):
            filings = get_two_most_recent_filings(cik)
        if len(filings) < 2:
            st.error("Could not find two filings to compare.")
            st.stop()
        newer, older = filings[0], filings[1]
        st.write(f"**Newer:** {newer['form']} filed {newer['date']}  |  **Older:** {older['form']} filed {older['date']}")

        with st.spinner("Pulling revenue and margin data..."):
            facts_data = fetch_facts(cik)
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

            # widen the numeric context beyond revenue/margin so forecast_impact
            # has real anchors for opex, R&D, and income lines too, not just the
            # two metrics tracked above. Same real XBRL tags used in the Charts tab.
            extra_tags = {
                "Operating Expenses": "OperatingExpenses",
                "R&D Expense": "ResearchAndDevelopmentExpense",
                "Net Income": "NetIncomeLoss",
                "Operating Income": "OperatingIncomeLoss",
            }
            extra_lines = []
            for label_name, tag in extra_tags.items():
                series = edgar_pull.clean_series(facts_data, tag)
                if len(series) >= 2:
                    recent = series[-1]
                    prior = series[-2]
                    extra_lines.append(
                        f"{label_name}: {calendar_label(prior)} ${prior['val']:,} -> "
                        f"{calendar_label(recent)} ${recent['val']:,}"
                    )
            if extra_lines:
                numeric_context += "\n\nOther recent line items (quarter-over-quarter):\n" + "\n".join(extra_lines)

        with st.spinner("Fetching and cleaning filing text..."):
            mdna_new = get_clean_mdna(cik, newer["accession"], newer["primary_doc"])
            mdna_old = get_clean_mdna(cik, older["accession"], older["primary_doc"])
        if not mdna_new or not mdna_old:
            st.error("Could not extract MD&A from one or both filings.")
            st.stop()

        with st.spinner("Diffing filings, sentence by sentence..."):
            old_sentences = split_sentences(mdna_old)
            new_sentences = split_sentences(mdna_new)
            added, removed, cosmetic_count = diff_sentences_filtered(old_sentences, new_sentences)
        st.write(f"**Diff:** {len(added)} new sentences, {len(removed)} removed, ~{cosmetic_count} cosmetic-only (filtered out)")

        final_thesis = thesis.strip() if thesis.strip() else f"{company_name}: no specific thesis provided, flag any material business changes generally."

        with st.spinner("Running materiality analysis against your thesis..."):
            prompt = build_prompt(added, removed, numeric_context, final_thesis)
            response = client.messages.create(model="claude-sonnet-4-5", max_tokens=4096,
                messages=[{"role": "user", "content": prompt}])
            raw_output = response.content[0].text
            clean = raw_output.strip().removeprefix("```json").removesuffix("```").strip()
            try:
                parsed = json.loads(clean)
                if isinstance(parsed, dict):
                    pm_headline, items = parsed.get("pm_headline", ""), parsed.get("items", [])
                elif isinstance(parsed, list):
                    pm_headline, items = "", parsed
                else:
                    pm_headline, items = "", []
            except Exception as e:
                st.error(f"Could not parse model output: {e}")
                st.text(raw_output)
                st.stop()

        full_source_text = mdna_new + " " + mdna_old
        for item in items:
            item["verification"] = verify_item(item, numeric_context, full_source_text)

        def sort_key(it):
            try:
                return -int(it.get("materiality_score", 0))
            except (ValueError, TypeError):
                return 0
        items.sort(key=sort_key)

        st.divider()
        st.subheader(f"{company_name} — Filing Narrative Drift Report")
        st.caption(f"Comparing {newer['form']} filed {newer['date']} vs {older['form']} filed {older['date']}")

        if pm_headline:
            st.markdown(f"""
            <div style="background:#F0F3FA;border-left:3px solid #1E3A8A;border-radius:4px;padding:14px 20px;margin:18px 0;">
                <div style="font-weight:600;color:#1E3A8A;font-size:0.8rem;letter-spacing:0.05em;text-transform:uppercase;margin-bottom:4px;">PM Headline</div>
                <div style="font-size:1.1rem;color:#111111;">{pm_headline}</div>
            </div>
            """, unsafe_allow_html=True)

        DIRECTION_STYLE = {
            "increase": ("&#9650;", "#1E7A3E", "#E6F4EA"),
            "decrease": ("&#9660;", "#B91C1C", "#FDE8E8"),
            "reassess": ("&#8226;", "#946800", "#FEF9E0"),
        }
        CONFIDENCE_STYLE = {
            "high": "#1E7A3E",
            "medium": "#C2540A",
            "low": "#8B8F9C",
        }

        def render_forecast_panel(fi):
            # the model occasionally returns forecast_impact as a JSON string
            # instead of a nested object; parse it if so before checking its shape
            if isinstance(fi, str):
                try:
                    fi = json.loads(fi)
                except Exception:
                    fi = None
            if not fi or not isinstance(fi, dict) or not fi.get("line_item"):
                st.markdown("""
                <div style="border:1px dashed #D8D9DE;border-radius:6px;padding:16px;height:100%;
                            display:flex;align-items:center;justify-content:center;text-align:center;">
                    <span style="font-size:0.82rem;color:#8B8F9C;">No clear model implication for this item</span>
                </div>
                """, unsafe_allow_html=True)
                return
            direction = fi.get("direction", "")
            arrow, dir_color, dir_bg = DIRECTION_STYLE.get(direction.lower(), ("&#8226;", "#555555", "#EDEDED"))
            conf = fi.get("confidence", "")
            conf_color = CONFIDENCE_STYLE.get(conf.lower(), "#555555")
            st.markdown(f"""
            <div style="border:1px solid #D8D9DE;border-radius:6px;padding:14px 16px;height:100%;background:#FAFAF8;">
                <div style="font-size:0.68rem;font-weight:600;letter-spacing:0.08em;text-transform:uppercase;color:#8B8F9C;margin-bottom:8px;">
                    Forecast Refresh
                </div>
                <div style="display:inline-block;background:{dir_bg};color:{dir_color};border-radius:4px;padding:4px 10px;font-weight:600;font-size:0.9rem;margin-bottom:8px;">
                    {arrow}&nbsp;{esc(direction).title()}
                </div>
                <div style="font-weight:600;color:#1E3A8A;font-size:0.98rem;margin-bottom:4px;">{fi.get('line_item', '')}</div>
                <div style="font-size:0.85rem;color:#333;margin-bottom:8px;line-height:1.4;">{fi.get('magnitude_estimate', '')}</div>
                <div style="font-size:0.75rem;color:{conf_color};font-weight:600;">{esc(conf).title()} confidence</div>
            </div>
            """, unsafe_allow_html=True)

        for item in items:
            with st.container(border=True):
                st.markdown(score_pill(item.get("materiality_score")), unsafe_allow_html=True)
                st.markdown(f"#### {esc(item['theme'])}")

                col_thesis, col_forecast = st.columns([2, 1])
                with col_thesis:
                    st.markdown(f"""
- **What changed:** {esc(item['what_changed'])}
- **Ties to thesis:** {esc(item['ties_to_thesis'])}
- **Numeric check:** {esc(item['numeric_check'])}
- **Stated vs. inferred:** {esc(item['stated_vs_inferred'])}
- **Verification:** {esc(item['verification'])}
- **Action item:** {esc(item['action_item'])}
""")
                with col_forecast:
                    render_forecast_panel(item.get("forecast_impact"))

        st.markdown("""
        <div style="margin-top:24px;padding:16px 20px;border-top:1px solid #D8D9DE;font-size:0.82rem;color:#555;">
            <div style="font-weight:600;color:#1E3A8A;margin-bottom:8px;">How to read this report</div>
            <div style="margin-bottom:4px;"><b>Materiality (1–5):</b> how much a finding should change what the analyst does next. 5 = re-underwrite the position now; 1 = worth noting, no action needed.</div>
            <div style="margin-bottom:4px;"><b>Forecast Refresh direction:</b> ▲ Increase / ▼ Decrease = the filing supports a directional model revision. • Reassess = real uncertainty raised, but no clear direction yet.</div>
            <div><b>Confidence:</b> High = driven by a figure stated directly in the filing. Medium = a reasonable inference from stated data. Low = mostly qualitative or risk language with no numeric anchor.</div>
        </div>
        """, unsafe_allow_html=True)

# =========================== TAB 2: charts, fully automatic, no input ===========================
with tab2:
    active_ticker = st.session_state.get("last_ticker", "")
    active_name = st.session_state.get("last_company_name", "")

    if not active_ticker:
        st.caption("Run a thesis analysis in the first tab. Charts for that company will populate here automatically, no need to enter a ticker again.")
    else:
        st.markdown(f"#### {active_name} ({active_ticker})")
        st.caption("Every chart below is pulled fresh from EDGAR for the same company analyzed in the Thesis Analysis tab.")

        with st.spinner(f"Pulling all metrics for {active_ticker}..."):
            cik_c, _ = get_cik_from_ticker(active_ticker)
            facts_c = fetch_facts(cik_c)
            results = {}
            for metric_name in METRICS:
                try:
                    d = get_metric_dict(facts_c, metric_name)
                except Exception:
                    d = {}
                results[metric_name] = d

        available_charts = {name: d for name, d in results.items() if d}

        if not available_charts:
            st.caption(f"No standard XBRL metrics were available for {active_name}.")
        else:
            for metric_name, d in available_charts.items():
                spec = METRICS[metric_name]
                st.markdown(f"##### {metric_name}")
                df = pd.DataFrame({metric_name: d})
                df.index.name = "Quarter"
                if spec["chart"] == "bar":
                    st.bar_chart(df, color=["#1E3A8A"], x_label="Fiscal Quarter", y_label=metric_name)
                else:
                    st.line_chart(df, color=["#1E3A8A"], x_label="Fiscal Quarter", y_label=metric_name)
                st.markdown("<div style='margin-bottom:10px;'></div>", unsafe_allow_html=True)
