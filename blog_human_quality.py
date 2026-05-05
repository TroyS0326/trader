import re
from collections import Counter

GENERIC_PHRASES = [
    "in today's fast-paced world",
    "delve into",
    "unlock the power",
    "game-changer",
    "revolutionary",
    "cutting-edge",
    "transformative",
    "seamless",
    "robust solution",
    "comprehensive guide",
    "whether you're a beginner or an expert",
    "navigate the complexities",
    "in the realm of",
    "it is important to note",
    "furthermore",
    "moreover",
    "additionally",
    "in conclusion",
]

SPECIFIC_TERMS = [
    "vwap", "orb", "bracket order", "stop-loss", "paper trading", "trading playbook",
    "broker", "alpaca", "risk controls", "entry rules", "target", "scanner", "execution",
    "dashboard", "automation",
]

TRUST_TERMS = [
    "not financial advice", "educational purposes", "trading involves risk", "risk management",
    "paper trading", "test rules", "no guarantee", "losses", "risk controls",
]

PRACTICAL_TERMS = [
    "for example", "example", "step", "checklist", "takeaway", "final thoughts", "conclusion",
    "how to", "next", "plan", "rules",
]


def _strip_html_tags(value: str) -> str:
    text = value or ""
    text = re.sub(r"(?is)<\s*(script|style).*?>.*?<\s*/\s*\1\s*>", " ", text)
    text = re.sub(r"(?i)<\s*br\s*/?\s*>", "\n", text)
    text = re.sub(r"(?i)</\s*p\s*>", "\n", text)
    text = re.sub(r"(?i)</\s*h[1-6]\s*>", "\n", text)
    text = re.sub(r"(?i)</\s*li\s*>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _split_sentences(text: str) -> list[str]:
    cleaned = (text or "").strip()
    if not cleaned:
        return []
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", cleaned) if s.strip()]


def _count_words(text: str) -> int:
    return len(re.findall(r"\b[\w'-]+\b", text or ""))


def _count_phrase_occurrences(text: str, phrases: list[str]) -> int:
    lowered = (text or "").lower()
    total = 0
    for phrase in phrases:
        total += len(re.findall(re.escape(phrase.lower()), lowered))
    return total


def _contains_any(text: str, phrases: list[str]) -> bool:
    return _count_phrase_occurrences(text, phrases) > 0


def analyze_human_quality(title: str, excerpt: str = "", body_html: str = "", target_keyword: str = "") -> dict:
    full_text = " ".join([title or "", excerpt or "", _strip_html_tags(body_html)])
    full_text_lower = full_text.lower()
    sentences = _split_sentences(full_text)
    sentence_lengths = [_count_words(s) for s in sentences if _count_words(s) > 0]
    word_count = _count_words(full_text)
    sentence_count = len(sentence_lengths)
    avg_sentence_length = round((sum(sentence_lengths) / sentence_count), 2) if sentence_count else 0.0
    short_sentence_ratio = round((sum(1 for n in sentence_lengths if n < 8) / sentence_count), 3) if sentence_count else 0.0
    long_sentence_ratio = round((sum(1 for n in sentence_lengths if n > 30) / sentence_count), 3) if sentence_count else 0.0

    paragraph_count = len(re.findall(r"(?i)<\s*p\b", body_html or ""))
    h2_count = len(re.findall(r"(?i)<\s*h2\b", body_html or ""))
    list_count = len(re.findall(r"(?i)<\s*(ul|ol)\b", body_html or ""))
    internal_link_count = len(re.findall(r"(?i)<a[^>]+href=[\"\']/[^\"\']*[\"\']", body_html or ""))

    generic_phrase_count = _count_phrase_occurrences(full_text_lower, GENERIC_PHRASES)
    specific_matches = _count_phrase_occurrences(full_text_lower, SPECIFIC_TERMS)
    trust_matches = _count_phrase_occurrences(full_text_lower, TRUST_TERMS)
    practical_matches = _count_phrase_occurrences(full_text_lower, PRACTICAL_TERMS)

    strengths, warnings, suggestions = [], [], []

    readability_score = 0
    if word_count >= 250:
        readability_score += 8
        strengths.append("Good overall length for a helpful blog post.")
    elif word_count >= 120:
        readability_score += 5
    else:
        warnings.append("Post is short; readers may need more explanation and examples.")
        suggestions.append("Add a plain-English takeaway section.")

    if 12 <= avg_sentence_length <= 24:
        readability_score += 10
        strengths.append("Sentence length is in a clear, reader-friendly range.")
    elif avg_sentence_length > 30:
        warnings.append("Average sentence length is high; readability may suffer.")
        suggestions.append("Shorten a few long sentences for readability.")
    elif avg_sentence_length < 8 and sentence_count > 0:
        warnings.append("Most sentences are very short; flow may feel choppy.")

    if long_sentence_ratio <= 0.3:
        readability_score += 7
    else:
        warnings.append("Too many long sentences can make the post harder to scan.")

    specificity_score = min(25, specific_matches * 2 + (8 if specific_matches >= 4 else 0))
    if specific_matches >= 4:
        strengths.append("Uses specific trading/platform language instead of generic copy.")
    else:
        warnings.append("Content may be too generic for trading readers.")
        suggestions.append("Replace generic phrases with specific trading workflow language.")

    trust_score = min(25, trust_matches * 3 + (7 if trust_matches >= 3 else 0))
    if trust_matches >= 3:
        strengths.append("Includes strong risk and educational framing.")
    else:
        warnings.append("Risk/caution language is limited for a trading topic.")
        suggestions.append("Add one paragraph explaining what can go wrong.")

    usefulness_score = 0
    if h2_count >= 2:
        usefulness_score += 6
    else:
        warnings.append("Add clearer section structure with H2 headings.")
    if list_count >= 1:
        usefulness_score += 5
    else:
        warnings.append("No list structure found; checklists can improve usability.")
    if practical_matches >= 2:
        usefulness_score += 5
    else:
        suggestions.append("Add a short real-world example of how a trader might use this concept.")
    if internal_link_count >= 1:
        usefulness_score += 5
    else:
        suggestions.append("Add an internal link to the XeanVI playbook page.")
    if re.search(r"(?i)final thoughts|conclusion|takeaway", full_text):
        usefulness_score += 4

    if generic_phrase_count >= 3:
        warnings.append("Multiple generic phrases detected; tone may feel templated.")
        suggestions.append("Replace generic phrases with concrete examples and process details.")

    if target_keyword.strip():
        keyword = target_keyword.strip().lower()
        keyword_hits = len(re.findall(re.escape(keyword), full_text_lower))
        keyword_density = (keyword_hits / max(word_count, 1)) * 100
        if keyword_density > 3:
            warnings.append("Target keyword appears too often and may feel unnatural.")

    openings = []
    for sentence in sentences:
        words = re.findall(r"\b[\w'-]+\b", sentence.lower())
        if len(words) >= 3:
            openings.append(" ".join(words[:3]))
    repeated_openings = [k for k, v in Counter(openings).items() if v >= 3]
    if repeated_openings:
        warnings.append("Several sentences start the same way, which can feel repetitive.")

    score = max(0, min(100, readability_score + specificity_score + trust_score + usefulness_score - min(10, generic_phrase_count * 2)))
    status = "strong" if score >= 80 else "acceptable" if score >= 60 else "needs_work"

    return {
        "score": int(score),
        "status": status,
        "strengths": list(dict.fromkeys(strengths)),
        "warnings": list(dict.fromkeys(warnings)),
        "suggestions": list(dict.fromkeys(suggestions)),
        "metrics": {
            "word_count": word_count,
            "sentence_count": sentence_count,
            "avg_sentence_length": float(avg_sentence_length),
            "short_sentence_ratio": float(short_sentence_ratio),
            "long_sentence_ratio": float(long_sentence_ratio),
            "paragraph_count": paragraph_count,
            "h2_count": h2_count,
            "list_count": list_count,
            "generic_phrase_count": generic_phrase_count,
            "specificity_score": int(specificity_score),
            "readability_score": int(readability_score),
            "trust_score": int(trust_score),
        }
    }
