import re
import difflib
from edgar_sections import get_clean_mdna

def split_sentences(text):
    text = re.sub(r"\s+", " ", text).strip()
    sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z])", text)
    sentences = [s.strip() for s in sentences if len(s.strip()) > 30]
    # drop table-like fragments: long stretches with mostly digits and no
    # real punctuation are financial statement tables that leaked into text
    # extraction, not narrative prose, and they break word-level diffing
    filtered = []
    for s in sentences:
        digit_ratio = sum(c.isdigit() for c in s) / max(len(s), 1)
        too_long = len(s) > 600
        if digit_ratio > 0.25 or too_long:
            continue
        filtered.append(s)
    return filtered

def normalize_sentence(s):
    s = s.lower()
    s = re.sub(r"\$\s?[\d,.]+\s?(billion|million|thousand)?", "[NUM]", s)
    s = re.sub(r"\d+(\.\d+)?%?", "[NUM]", s)
    s = re.sub(r"\b(first|second|third|fourth)\s+quarter\b", "[QTR]", s)
    s = re.sub(r"\bfiscal\s+20\d{2}\b", "[FY]", s)
    s = re.sub(r"\bq[1-4]\b", "[QTR]", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def is_cosmetic_pair(old_norm, new_norm, threshold=0.85):
    ratio = difflib.SequenceMatcher(None, old_norm, new_norm).ratio()
    return ratio >= threshold

def diff_sentences_filtered(old_sentences, new_sentences):
    old_normalized = [normalize_sentence(s) for s in old_sentences]
    new_normalized = [normalize_sentence(s) for s in new_sentences]

    matcher = difflib.SequenceMatcher(None, old_normalized, new_normalized)

    material_added = []
    material_removed = []
    cosmetic_only_count = 0

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue

        if tag == "replace" and (i2 - i1) == (j2 - j1):
            all_pairs_cosmetic = True
            for offset in range(i2 - i1):
                old_s = old_normalized[i1 + offset]
                new_s = new_normalized[j1 + offset]
                if not is_cosmetic_pair(old_s, new_s):
                    all_pairs_cosmetic = False
                    break
            if all_pairs_cosmetic:
                cosmetic_only_count += (i2 - i1)
                continue

        if tag in ("insert", "replace"):
            material_added.extend(new_sentences[j1:j2])
        if tag in ("delete", "replace"):
            material_removed.extend(old_sentences[i1:i2])

    return material_added, material_removed, cosmetic_only_count

if __name__ == "__main__":
    adobe_cik = "0000796343"
    print("Fetching CY2026 Q2...")
    q2_text = get_clean_mdna(adobe_cik, "0000796343-26-000112", "adbe-20260529.htm")
    print("Fetching CY2026 Q1...")
    q1_text = get_clean_mdna(adobe_cik, "0000796343-26-000056", "adbe-20260227.htm")

    q1_sentences = split_sentences(q1_text)
    q2_sentences = split_sentences(q2_text)

    added, removed, cosmetic_count = diff_sentences_filtered(q1_sentences, q2_sentences)

    print(f"Q1 sentences: {len(q1_sentences)}  |  Q2 sentences: {len(q2_sentences)}")
    print(f"Cosmetic-only changes filtered: ~{cosmetic_count}")
    print(f"Structurally NEW in Q2: {len(added)}")
    print(f"Structurally REMOVED from Q1: {len(removed)}")
