import re
from html import unescape

RISK_NOTE_HTML = "<p><em>This article is for educational purposes only and is not financial advice. Traders should test rules carefully, use risk controls, and understand that all trading involves risk.</em></p>"


REPEATED_PHRASE_SYNONYMS = {
    "the market open": [
        "the opening session",
        "early trading",
        "the first part of the trading day",
        "the open",
    ],
    "trading discipline": [
        "rules-based discipline",
        "execution discipline",
        "a structured trading process",
    ],
    "risk management": [
        "risk controls",
        "defined risk rules",
        "risk planning",
    ],
    "paper trading": [
        "simulated trading",
        "a paper environment",
        "practice trading",
    ],
    "trading playbook": [
        "rules-based playbook",
        "trade plan",
        "structured trading plan",
    ],
}

CURATED_EXTERNAL_SOURCES = {
    "finra": "<p><strong>Educational source:</strong> For broader context on day trading rules and risks, review <a href=\"https://www.finra.org/investors/investing/investment-products/stocks/day-trading\" target=\"_blank\" rel=\"noopener noreferrer\">FINRA's day trading investor education resource</a>.</p>",
    "investor_gov": "<p><strong>Educational source:</strong> For a general risk overview, see <a href=\"https://www.investor.gov/additional-resources/general-resources/glossary/day-trading\" target=\"_blank\" rel=\"noopener noreferrer\">Investor.gov's day trading overview</a>.</p>",
    "sec": '<p><strong>Educational source:</strong> For broader investor education, review the <a href="https://www.sec.gov/education/investor-education" target="_blank" rel="noopener noreferrer">SEC investor education resources</a>.</p>',
}

def _extract_repeated_phrases_from_warnings(warnings: list[str] | None) -> list[str]:
    phrases = []
    for warning in warnings or []:
        match = re.search(r"Repeated phrase pattern detected \('([^']+)'\)", warning or "", flags=re.IGNORECASE)
        if match:
            phrases.append(match.group(1).strip().lower())
    return list(dict.fromkeys(phrases))


def _split_html_segments(body_html: str) -> list[tuple[str, bool]]:
    parts = re.split(r"(<[^>]+>)", body_html or "")
    return [(part, bool(part.startswith("<") and part.endswith(">"))) for part in parts if part is not None]


def _reduce_repeated_phrases(body_html: str, repeated_phrases: list[str] | None = None) -> tuple[str, list[str]]:
    phrases = [p.strip().lower() for p in (repeated_phrases or []) if p and p.strip().lower() in REPEATED_PHRASE_SYNONYMS]
    if not phrases:
        return body_html, []

    updated = body_html or ""
    changes = []
    segments = _split_html_segments(updated)

    for phrase in phrases:
        pattern = re.compile(rf"\b{re.escape(phrase)}\b", flags=re.IGNORECASE)
        text_only = " ".join(seg for seg, is_tag in segments if not is_tag)
        total_count = len(pattern.findall(text_only))
        if total_count <= 4:
            continue

        replacements = REPEATED_PHRASE_SYNONYMS[phrase]
        seen = 0
        replace_idx = 0
        new_segments = []
        for segment, is_tag in segments:
            if is_tag:
                new_segments.append((segment, is_tag))
                continue

            def _repl(match):
                nonlocal seen, replace_idx
                seen += 1
                if seen <= 2:
                    return match.group(0)
                replacement = replacements[replace_idx % len(replacements)]
                replace_idx += 1
                return replacement

            new_segments.append((pattern.sub(_repl, segment), is_tag))

        if seen > 2:
            segments = new_segments
            changes.append(f"Reduced repeated phrase '{phrase}' by replacing later occurrences with natural alternatives.")

    return "".join(seg for seg, _ in segments), changes


def _has_external_http_link(body_html: str) -> bool:
    return bool(re.search(r'href=["\']https?://', body_html or "", flags=re.IGNORECASE))


def _add_curated_external_source_if_missing(body_html: str, title: str, target_keyword: str = "") -> tuple[str, list[str]]:
    safe_body = body_html or ""
    combined = f"{title or ''} {target_keyword or ''} {_strip_html_tags(safe_body)}".lower()
    if len(_strip_html_tags(safe_body)) < 80 or _has_external_http_link(safe_body):
        return safe_body, []

    if any(token in combined for token in ["day trading", "market open", "pattern day trader", "pdt"]):
        snippet = CURATED_EXTERNAL_SOURCES["finra"]
    elif any(token in combined for token in ["risk", "trading", "stock", "market"]):
        snippet = CURATED_EXTERNAL_SOURCES["investor_gov"]
    else:
        snippet = CURATED_EXTERNAL_SOURCES["sec"]

    return safe_body.rstrip() + "\n\n" + snippet, ["Added one curated external educational source."]


def _strip_html_tags(value):
    text = unescape(value or "")
    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _clean_slug(value):
    slug = (value or "").strip().lower()
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"[^a-z0-9-]", "", slug)
    slug = re.sub(r"-{2,}", "-", slug)
    return slug.strip("-")


def _truncate_words(value, max_chars):
    text = re.sub(r"\s+", " ", (value or "").strip())
    if len(text) <= max_chars:
        return text
    truncated = text[:max_chars + 1]
    last_space = truncated.rfind(" ")
    if last_space > 0:
        truncated = truncated[:last_space]
    else:
        truncated = text[:max_chars]
    return truncated.rstrip(" ,;:-")


def _contains_risk_language(text):
    haystack = (text or "").lower()
    markers = [
        "not financial advice",
        "educational purposes only",
        "trading involves risk",
        "all trading involves risk",
        "use risk controls",
        "risk management",
    ]
    return any(marker in haystack for marker in markers)


def _looks_trading_related(text):
    haystack = (text or "").lower()
    markers = [
        "trading",
        "day trading",
        "vwap",
        "market",
        "stock",
        "scanner",
        "entry",
        "exit",
        "playbook",
        "bracket order",
        "paper trading",
    ]
    return any(marker in haystack for marker in markers)


def _infer_keyword(title, body_text):
    combined = f"{title or ''} {body_text or ''}".lower()
    if "playbook" in combined:
        return "trading playbook"
    if "vwap" in combined:
        return "VWAP reclaim"
    if "paper trading" in combined:
        return "paper trading"
    if "bracket order" in combined or "bracket orders" in combined:
        return "bracket orders"
    if " ai " in f" {combined} " or "automation" in combined or "automated" in combined:
        return "AI trading automation"
    return ""


def _normalize_base_url(site_base_url: str) -> str:
    base = (site_base_url or "").strip() or "https://xeanvi.com"
    return re.sub(r"/+$", "", base)


def _build_canonical(base_url: str, slug: str) -> str:
    return f"{_normalize_base_url(base_url)}/blog/{(slug or '').strip('/')}"


def apply_safe_seo_fixes(
    title: str,
    slug: str,
    meta_title: str,
    meta_description: str,
    excerpt: str,
    body_html: str,
    target_keyword: str = "",
    canonical_url: str = "",
    og_image: str = "",
    seo_report: dict | None = None,
    site_base_url: str = "https://xeanvi.com",
) -> dict:
    changes = []
    unapplied_suggestions = []

    safe_title = (title or "").strip()
    safe_body_html = body_html or ""
    body_text = _strip_html_tags(safe_body_html)

    safe_slug = _clean_slug(slug)
    if not safe_slug and safe_title:
        safe_slug = _clean_slug(safe_title)
        if safe_slug:
            changes.append("Generated missing slug from title.")
    elif safe_slug != (slug or "").strip():
        changes.append("Cleaned slug formatting.")

    safe_meta_title = (meta_title or "").strip()
    if not safe_meta_title and safe_title:
        safe_meta_title = safe_title
        changes.append("Generated missing meta title from title.")
    if safe_meta_title:
        if "| xeanvi" not in safe_meta_title.lower():
            candidate = f"{safe_meta_title} | XeanVI"
            if len(candidate) <= 65:
                safe_meta_title = candidate
                changes.append("Appended brand suffix to meta title.")
        trimmed = _truncate_words(safe_meta_title, 65)
        if trimmed != safe_meta_title:
            safe_meta_title = trimmed
            changes.append("Trimmed meta title length.")

    safe_excerpt = (excerpt or "").strip()
    if not safe_excerpt and body_text:
        safe_excerpt = _truncate_words(body_text, 240)
        changes.append("Generated missing excerpt from body.")

    safe_meta_description = (meta_description or "").strip()
    if not safe_meta_description:
        source_text = safe_excerpt or body_text
        safe_meta_description = _truncate_words(source_text, 160)
        if safe_meta_description:
            changes.append("Generated missing meta description from excerpt/body.")
    else:
        trimmed_meta_description = _truncate_words(safe_meta_description, 160)
        if trimmed_meta_description != safe_meta_description:
            safe_meta_description = trimmed_meta_description
            changes.append("Trimmed meta description length.")

    safe_target_keyword = (target_keyword or "").strip()
    if not safe_target_keyword:
        inferred = _infer_keyword(safe_title, body_text)
        if inferred:
            safe_target_keyword = inferred
            changes.append("Inferred target keyword from title/body.")

    safe_canonical_url = (canonical_url or "").strip()
    if not safe_canonical_url and safe_slug:
        safe_canonical_url = _build_canonical(site_base_url, safe_slug)
        changes.append("Generated missing canonical URL from slug.")

    if seo_report:
        for suggestion in (seo_report.get("suggestions") or []) + (seo_report.get("warnings") or []):
            lowered = (suggestion or "").lower()
            if "canonical url" in lowered and safe_slug and not safe_canonical_url:
                safe_canonical_url = _build_canonical(site_base_url, safe_slug)
                changes.append("Applied canonical URL suggestion.")
            elif "internal link" in lowered or "external" in lowered:
                unapplied_suggestions.append(suggestion)
            elif "h2" in lowered or "heading" in lowered or "rewrite" in lowered:
                unapplied_suggestions.append(suggestion)

    repeated_phrases = _extract_repeated_phrases_from_warnings((seo_report or {}).get("warnings"))
    safe_body_html, phrase_changes = _reduce_repeated_phrases(safe_body_html, repeated_phrases)
    changes.extend(phrase_changes)

    should_add_source = False
    if seo_report:
        suggestion_text = " ".join((seo_report.get("suggestions") or []) + (seo_report.get("warnings") or [])).lower()
        should_add_source = "external" in suggestion_text and "source" in suggestion_text
    elif _looks_trading_related(f"{safe_title} {body_text}"):
        should_add_source = True

    if should_add_source:
        safe_body_html, source_changes = _add_curated_external_source_if_missing(safe_body_html, safe_title, safe_target_keyword)
        changes.extend(source_changes)

    if _looks_trading_related(f"{safe_title} {body_text}") and not _contains_risk_language(_strip_html_tags(safe_body_html)):
        safe_body_html = safe_body_html.rstrip() + "\n\n" + RISK_NOTE_HTML
        changes.append("Appended educational risk note to body HTML.")

    fields = {
        "title": safe_title,
        "slug": safe_slug,
        "meta_title": safe_meta_title,
        "meta_description": safe_meta_description,
        "excerpt": safe_excerpt,
        "body_html": safe_body_html,
        "target_keyword": safe_target_keyword,
        "canonical_url": safe_canonical_url,
        "og_image": og_image or "",
    }
    return {"fields": fields, "changes": list(dict.fromkeys(changes)), "unapplied_suggestions": list(dict.fromkeys(unapplied_suggestions))}
