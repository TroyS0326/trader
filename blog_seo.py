import re
from collections import Counter
from html import unescape
from urllib.parse import urlparse

HARD_BLOCK_PHRASES = [
    "guaranteed profit", "guarantee profits", "guaranteed returns", "guaranteed wins", "guaranteed winner",
    "guaranteed trading income", "guaranteed profitable", "guaranteed success", "always profitable", "always wins",
    "can't lose", "cannot lose", "never lose", "no losing trades", "risk-free trading", "no risk trading", "zero risk",
    "risk free profits", "make money fast", "get rich", "get rich quick", "easy money", "passive income from trading",
    "make $100", "make $500", "make $1000", "make 1000 a day", "daily profits guaranteed", "earn daily profits",
    "steady daily income", "trading paycheck", "replace your job with trading", "quit your job trading",
    "profitable trading bot", "guaranteed trading bot", "bot that wins", "wins every trade",
    "never loses", "beats the market guaranteed", "outperform the market guaranteed", "automatic profits",
    "autopilot profits", "money printing bot", "set and forget trading bot", "you should buy", "you should sell",
    "buy this stock", "sell this stock", "this stock will go up", "this stock will explode", "this stock is guaranteed",
    "guaranteed breakout", "guaranteed squeeze", "safe trading", "no downside", "foolproof strategy", "can't fail",
    "impossible to lose", "perfect strategy", "secret strategy that always works", "investment advice", "financial advisor",
    "fiduciary", "registered investment advisor", "sec approved", "finra approved", "certified trading returns",
    "audited returns", "verified profits",
]

WARNING_PHRASES = [
    "best trading bot", "beat the market", "market-beating", "high win rate", "profitable setup", "elite trader",
    "secret indicator", "hidden strategy", "institutional secret", "smart money secret", "easy trading",
    "passive trading", "automated income", "side hustle trading", "hands-free trading", "algorithm that prints money",
    "ai stock picker", "ai predicts the market", "predicts price movement", "next big stock", "hot stock",
    "moonshot", "rocket stock",
]

REQUIRED_CAUTION_CONCEPTS = [
    "not financial advice", "educational purposes", "trading involves risk", "risk management", "paper trading",
    "test rules", "no guarantee", "losses", "stop-loss", "stop loss", "risk controls",
]

GENERIC_AI_PHRASES = ["in today's fast-paced world", "delve into"]
TRADING_TERMS = ["trading", "trader", "day trading", "stock", "stocks", "market", "vwap", "orb", "breakout", "broker", "alpaca", "paper trading", "live trading", "bracket order", "stop loss", "risk", "automation", "scanner"]
DISCLAIMER_TERMS = ["not financial advice", "risk management", "paper trading", "manage risk"]
RISK_TERMS = ["risk", "discipline", "rules"]
NEGATION_PREFIXES = [" not ", " never ", " avoid ", " does not ", " do not ", " without ", " no claim of ", " should not promise "]


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip()).lower()


def _strip_html_tags(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html or "")
    return re.sub(r"\s+", " ", unescape(text)).strip()


def _find_phrase_matches(text: str, phrases: list[str]) -> list[tuple[str, int]]:
    normalized = _normalize_text(text)
    matches = []
    for phrase in phrases:
        needle = _normalize_text(phrase)
        start = 0
        while True:
            idx = normalized.find(needle, start)
            if idx < 0:
                break
            matches.append((needle, idx))
            start = idx + 1
    return matches


def _is_negated_context(text: str, phrase: str, index: int) -> bool:
    normalized = _normalize_text(text)
    start = index if index >= 0 else normalized.find(_normalize_text(phrase))
    if start < 0:
        return False
    lookback = f" {normalized[max(0, start - 40):start]} "
    return any(token in lookback for token in NEGATION_PREFIXES)


def _looks_trading_related(text: str) -> bool:
    normalized = _normalize_text(text)
    return any(term in normalized for term in TRADING_TERMS)


def _has_caution_language(text: str) -> bool:
    normalized = _normalize_text(text)
    return any(term in normalized for term in REQUIRED_CAUTION_CONCEPTS)


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

    body_text = _strip_html_tags(body_html)
    body_lower = _normalize_text(body_text)
    combined = " ".join([title, slug, meta_title, meta_description, excerpt, body_text])
    combined_lower = _normalize_text(combined)

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

    if any(p in _normalize_text(title) for p in WARNING_PHRASES):
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
            warnings.append("Body content is under 300 words (severe thin content).")
        elif word_count < 600:
            score -= 7
            warnings.append("Body content is under 600 words.")

    if h2_count < 2:
        score -= 4
        warnings.append("Use at least 2 H2 headings for structure.")

    if internal_link_count == 0:
        score -= 3
        warnings.append("Add 1–3 relevant internal links.")
        suggestions.append("Use the Suggested Internal Links panel to insert links where they fit naturally.")

    if _looks_trading_related(combined_lower) and not any(term in body_lower for term in RISK_TERMS):
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

    if external_link_count == 0 and any(term in combined_lower for term in ["how", "guide", "explained", "what is"]):
        suggestions.append("Consider adding one curated external educational source, such as FINRA, Investor.gov, or SEC investor education, when relevant.")

    hard_matches = _find_phrase_matches(combined_lower, HARD_BLOCK_PHRASES)
    warning_matches = _find_phrase_matches(combined_lower, WARNING_PHRASES)
    has_caution_language = _has_caution_language(combined_lower)

    for phrase, idx in hard_matches:
        if _is_negated_context(combined_lower, phrase, idx):
            warnings.append(f"Review risky phrase used in cautionary context: '{phrase}'.")
            continue
        score -= 30
        message = f"Remove or rewrite risky claim: '{phrase}'. XeanVI content must not promise trading outcomes."
        if is_publish:
            blocking_issues.append(message)
        else:
            warnings.append(message)

    for phrase, idx in warning_matches:
        if _is_negated_context(combined_lower, phrase, idx):
            continue
        score -= 8
        warnings.append(f"Review risky marketing phrase: '{phrase}'. Avoid hype and explain capabilities cautiously.")

    for phrase in GENERIC_AI_PHRASES:
        if phrase in body_lower:
            score -= 3
            warnings.append(f"Generic AI phrase detected: '{phrase}'.")

    if "xeanvi" not in body_lower:
        warnings.append("Body does not reference XeanVI directly.")

    if not any(term in body_lower for term in DISCLAIMER_TERMS):
        warnings.append("Add a cautionary disclaimer (not financial advice / risk management / paper trading).")

    if is_publish and _looks_trading_related(combined_lower) and not has_caution_language:
        warnings.append("Add a short educational/risk note explaining that the article is not financial advice and that trading involves risk.")
    if is_publish and blocking_issues and not has_caution_language:
        blocking_issues.append("Severe claims plus missing caution language. Add risk and educational context before publishing.")

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
    else:
        warnings.append("Add a canonical URL for this blog post.")

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
