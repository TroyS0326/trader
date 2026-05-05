import re
from html import unescape

RISK_NOTE_HTML = "<p><em>This article is for educational purposes only and is not financial advice. Traders should test rules carefully, use risk controls, and understand that all trading involves risk.</em></p>"


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
) -> dict:
    changes = []

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

    if _looks_trading_related(f"{safe_title} {body_text}") and not _contains_risk_language(body_text):
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
        "canonical_url": canonical_url or "",
        "og_image": og_image or "",
    }
    return {"fields": fields, "changes": changes}
