import re
from collections import Counter
from html import unescape
from urllib.parse import urlparse

BLOCKING_CLAIMS = [
    "guaranteed profit",
    "guaranteed wins",
    "no risk",
    "risk-free trading",
    "make $ per day",
    "get rich",
    "guaranteed returns",
    "this will make you profitable",
    "always works",
    "can't lose",
]

HYPE_CLAIMS = [
    "guaranteed",
    "guaranteed profit",
    "always wins",
    "risk-free",
    "make money fast",
    "beat the market guaranteed",
]

SOFT_RISKY_PHRASES = [
    "best trading bot",
    "beat the market",
    "easy money",
    "passive income from trading",
    "sure thing",
]

GENERIC_AI_PHRASES = ["in today's fast-paced world", "delve into"]
TRADING_TERMS = ["trading", "day trade", "day trading", "market", "bot", "strategy"]
DISCLAIMER_TERMS = ["not financial advice", "risk management", "paper trading", "manage risk"]
RISK_TERMS = ["risk", "discipline", "rules"]


def _strip_html(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html or "")
    return re.sub(r"\s+", " ", unescape(text)).strip()


def _links(html: str):
    return re.findall(r'href=["\']([^"\']+)["\']', html or "", flags=re.IGNORECASE)


def _count_headers(html: str, level: int) -> int:
    return len(re.findall(rf"<h{level}(\s|>)", html or "", flags=re.IGNORECASE))


def _is_internal(url: str) -> bool:
    u = (url or "").strip().lower()
    return u.startswith("/") or u.startswith("https://xeanvi.com") or u.startswith("http://xeanvi.com")


def analyze_blog_post_seo(title: str, slug: str, meta_title: str, meta_description: str, excerpt: str, body_html: str, target_keyword: str = "", canonical_url: str = "", status: str = "draft") -> dict:
    title = (title or "").strip()
    slug = (slug or "").strip()
    meta_title = (meta_title or "").strip()
    meta_description = (meta_description or "").strip()
    excerpt = (excerpt or "").strip()
    body_html = body_html or ""
    canonical_url = (canonical_url or "").strip()
    target_keyword = (target_keyword or "").strip()
    status = (status or "draft").strip().lower()
    is_publish = status == "published"

    blocking_issues, warnings, suggestions = [], [], []
    score = 100

    body_text = _strip_html(body_html)
    body_lower = body_text.lower()
    combined = " ".join([title, meta_title, meta_description, excerpt, body_text]).lower()

    words = re.findall(r"\b[\w'-]+\b", body_text)
    word_count = len(words)

    links = _links(body_html)
    internal_link_count = sum(1 for l in links if _is_internal(l))
    external_link_count = sum(1 for l in links if l.lower().startswith("http") and not _is_internal(l))
    h2_count = _count_headers(body_html, 2)
    h3_count = _count_headers(body_html, 3)

    if not title:
        score -= 20
        (blocking_issues if is_publish else warnings).append("Title is required.")
    elif len(title) > 70:
        score -= 6
        warnings.append("Title is longer than 70 characters.")
    elif len(title) < 25:
        score -= 2
        suggestions.append("Title is short; consider 25+ characters for clarity.")

    if any(p in title.lower() for p in HYPE_CLAIMS):
        score -= 12
        warnings.append("Title contains hype-style claims. Use neutral, educational wording.")

    if not slug:
        score -= 15
        (blocking_issues if is_publish else warnings).append("Slug is required.")
    else:
        if len(slug) > 80:
            score -= 5
            warnings.append("Slug is longer than 80 characters.")
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
            score -= 3
            suggestions.append("Slug should be lowercase and hyphenated.")

    if not meta_title:
        score -= 5
        warnings.append("Meta title is missing.")
    else:
        if len(meta_title) > 70:
            score -= 5
            warnings.append("Meta title is longer than 70 characters.")
        elif len(meta_title) < 35 or len(meta_title) > 65:
            score -= 2
            suggestions.append("Meta title is best kept between 35 and 65 characters.")

    if not meta_description:
        score -= 6
        warnings.append("Meta description is missing.")
    else:
        if len(meta_description) > 165:
            score -= 5
            warnings.append("Meta description is longer than 165 characters.")
        elif len(meta_description) < 90:
            score -= 2
            suggestions.append("Meta description is short; consider at least 90 characters.")
        elif len(meta_description) < 120 or len(meta_description) > 160:
            score -= 1
            suggestions.append("Meta description is ideally 120-160 characters.")

    if not body_text:
        score -= 25
        (blocking_issues if is_publish else warnings).append("Body content is required.")
    else:
        if word_count < 250:
            score -= 15
            (blocking_issues if is_publish else warnings).append("Body content is under 300 words (severe thin content).")
        elif word_count < 600:
            score -= 7
            warnings.append("Body content is under 600 words.")

    if h2_count < 2:
        score -= 4
        warnings.append("Use at least 2 H2 headings for structure.")

    if internal_link_count == 0:
        score -= 5
        warnings.append("Add at least one internal XeanVI link.")

    if any(term in combined for term in TRADING_TERMS) and not any(term in body_lower for term in RISK_TERMS):
        score -= 2
        suggestions.append("For trading topics, mention risk, discipline, or rules.")

    if target_keyword:
        kw = target_keyword.lower()
        if kw not in title.lower():
            score -= 2
            suggestions.append("Target keyword is missing from title.")
        if kw not in meta_description.lower():
            score -= 2
            suggestions.append("Target keyword is missing from meta description.")
        if kw not in body_lower[:300]:
            score -= 2
            suggestions.append("Target keyword is missing from the first 300 body characters.")

    if external_link_count == 0 and any(term in combined for term in ["how", "guide", "explained", "what is"]):
        suggestions.append("Consider citing an external educational source.")

    for phrase in BLOCKING_CLAIMS:
        if phrase in combined:
            score -= 30
            if is_publish:
                blocking_issues.append(f"Dangerous claim detected: '{phrase}'.")
            else:
                warnings.append(f"Dangerous claim detected: '{phrase}'.")

    for phrase in SOFT_RISKY_PHRASES:
        if phrase in combined:
            score -= 8
            warnings.append(f"Risky marketing phrase detected: '{phrase}'.")

    for phrase in GENERIC_AI_PHRASES:
        if phrase in body_lower:
            score -= 3
            warnings.append(f"Generic AI phrase detected: '{phrase}'.")

    if "xeanvi" not in body_lower:
        warnings.append("Body does not reference XeanVI directly.")

    if not any(term in body_lower for term in DISCLAIMER_TERMS):
        warnings.append("Add a cautionary disclaimer (not financial advice / risk management / paper trading).")

    if words:
        ngrams = [" ".join(words[i:i+3]).lower() for i in range(len(words)-2)]
        if ngrams:
            phrase, count = Counter(ngrams).most_common(1)[0]
            if count >= 6:
                score -= 5
                warnings.append(f"Repeated phrase pattern detected ('{phrase}') {count} times.")

    if canonical_url:
        parsed = urlparse(canonical_url)
        if parsed.scheme not in {"http", "https"}:
            suggestions.append("Canonical URL should be a full http/https URL.")

    score = max(0, min(100, score))
    final_status = "blocked" if blocking_issues else ("needs_work" if warnings else "good")

    metrics = {
        "word_count": word_count,
        "internal_link_count": internal_link_count,
        "external_link_count": external_link_count,
        "h2_count": h2_count,
        "h3_count": h3_count,
        "has_target_keyword_in_title": bool(target_keyword and target_keyword.lower() in title.lower()),
        "has_target_keyword_in_meta_description": bool(target_keyword and target_keyword.lower() in meta_description.lower()),
        "has_target_keyword_in_first_300_chars": bool(target_keyword and target_keyword.lower() in body_lower[:300]),
        "meta_title_length": len(meta_title),
        "meta_description_length": len(meta_description),
        "slug_length": len(slug),
    }

    return {
        "score": score,
        "status": final_status,
        "blocking_issues": list(dict.fromkeys(blocking_issues)),
        "warnings": list(dict.fromkeys(warnings)),
        "suggestions": list(dict.fromkeys(suggestions)),
        "metrics": metrics,
    }
