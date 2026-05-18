import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

PROD_ENV_PATH = "/etc/xeanvi/xeanvi.env"
BASE_DIR = Path(__file__).resolve().parent
if os.path.exists(PROD_ENV_PATH):
    load_dotenv(PROD_ENV_PATH)
else:
    load_dotenv(BASE_DIR / ".env")

DEFAULT_MODEL = "gemini-2.5-flash"
GEMINI_ENDPOINT_TEMPLATE = "https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"
logger = logging.getLogger(__name__)


def _clean_text(value: str, fallback: str = "") -> str:
    return (value or fallback).strip()


def _strip_json_fences(text: str) -> str:
    cleaned = (text or "").strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[len("```json"):].strip()
    elif cleaned.startswith("```"):
        cleaned = cleaned[len("```"):].strip()
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3].strip()
    return cleaned


def _extract_json_object(text: str) -> dict | None:
    """
    Extract first JSON object from model text.
    Handles:
    - raw JSON
    - ```json fenced JSON
    - ``` fenced JSON
    - commentary before/after JSON
    """
    cleaned = _strip_json_fences((text or "").strip())
    if not cleaned:
        return None
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass

    first = cleaned.find("{")
    last = cleaned.rfind("}")
    if first == -1 or last == -1 or last <= first:
        return None
    try:
        parsed = json.loads(cleaned[first:last + 1])
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        return None
    return None


def _strip_html_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", text or "")


def _first_chars_text(text: str, limit: int = 220) -> str:
    plain = re.sub(r"\s+", " ", _strip_html_tags(text)).strip()
    return plain[:limit].strip()


def _looks_like_html(text: str) -> bool:
    return bool(re.search(r"<(p|h1|h2|h3|ul|ol|li|a|strong|em|blockquote|div|section)\b", text or "", re.I))


def _gemini_error_summary(response: Any) -> str:
    status_summary = f"HTTP {response.status_code}"
    try:
        payload = response.json()
    except ValueError:
        return status_summary
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return status_summary
    message = _clean_text(error.get("message", ""))
    status = _clean_text(error.get("status", ""))
    if status and message:
        return f"{status_summary} ({status}: {message[:200]})"
    if message:
        return f"{status_summary} ({message[:200]})"
    if status:
        return f"{status_summary} ({status})"
    return status_summary


def apply_ai_seo_cleanup(
    title: str,
    slug: str,
    meta_title: str,
    meta_description: str,
    excerpt: str,
    body_html: str,
    target_keyword: str = "",
    seo_report: dict | None = None,
    internal_link_suggestions: list[dict] | None = None,
) -> dict[str, Any]:
    safe_fields = {
        "title": _clean_text(title),
        "slug": _clean_text(slug),
        "meta_title": _clean_text(meta_title),
        "meta_description": _clean_text(meta_description),
        "excerpt": _clean_text(excerpt),
        "body_html": body_html or "",
        "target_keyword": _clean_text(target_keyword),
    }
    result: dict[str, Any] = {"ok": False, "fields": safe_fields, "changes": [], "error": None}

    api_key = _clean_text(os.getenv("GEMINI_API_KEY"))
    if not api_key:
        result["error"] = "AI SEO cleanup is not configured. Missing GEMINI_API_KEY."
        return result

    model_name = _clean_text(os.getenv("GEMINI_MODEL"), DEFAULT_MODEL) or DEFAULT_MODEL

    warnings = [str(x).strip() for x in (seo_report or {}).get("warnings", []) if str(x).strip()]
    suggestions = [str(x).strip() for x in (seo_report or {}).get("suggestions", []) if str(x).strip()]

    link_rows = []
    for item in (internal_link_suggestions or [])[:8]:
        if not isinstance(item, dict):
            continue
        url = _clean_text(str(item.get("url") or ""))
        anchor = _clean_text(str(item.get("anchor_text") or ""))
        reason = _clean_text(str(item.get("reason") or ""))
        if url and anchor:
            link_rows.append({"url": url, "anchor_text": anchor, "reason": reason})

    prompt = f"""
You are editing an existing XeanVI educational blog draft.

Preserve:
- topic
- meaning
- educational intent
- existing structure where possible
- safe risk disclaimers
- useful internal links already present

Fix:
- risky trading/profit claims
- missing natural XeanVI mention
- missing target keyword placement
- missing meta title
- missing meta description
- missing excerpt
- repeated phrases
- generic AI phrases
- weak wording
- missing internal links only when relevant

Do not:
- promise profits
- imply guaranteed trading outcomes
- give personalized financial advice
- say users should buy or sell a stock
- invent statistics
- invent citations
- remove risk disclaimers
- stuff keywords
- stuff links
- add more than 3 internal links
- add irrelevant internal links
- rewrite into hype marketing copy

Hard claim rewrite rule:
If text says "guarantee profits" or similar risky phrase, rewrite it safely such as:
"No trading system can guarantee profits, and XeanVI content is intended for educational workflow and risk-discipline support."

Internal links:
Use only relevant suggestions passed in internal_link_suggestions.
Do not force /playbook or /features into every post.
Insert links naturally inside existing sentences where they fit.
Maximum 3 internal links total.
Do not duplicate a URL already in body_html.

Target keyword:
If target_keyword is provided:
- make title include it naturally only if not awkward
- make meta_description include it naturally
- mention it within the first 300 body characters naturally
- do not keyword stuff

XeanVI mention:
If body does not reference XeanVI directly add one natural sentence near end.

External source:
Do not invent sources.
If a curated external source paragraph already exists, preserve it.
If missing and article is trading-related, add one curated source from existing safe source map if available.

Return strict JSON only:
{{
  "title": "...",
  "meta_title": "...",
  "meta_description": "...",
  "excerpt": "...",
  "body_html": "...",
  "target_keyword": "...",
  "changes": ["..."]
}}
No markdown fences. No commentary outside JSON.

Current fields JSON:
{json.dumps(safe_fields, ensure_ascii=False)}

SEO warnings: {json.dumps(warnings, ensure_ascii=False)}
SEO suggestions: {json.dumps(suggestions, ensure_ascii=False)}
internal_link_suggestions: {json.dumps(link_rows, ensure_ascii=False)}
""".strip()

    endpoint = GEMINI_ENDPOINT_TEMPLATE.format(model_name=model_name)
    def _request_cleanup(prompt_text: str) -> str:
        payload = {
            "contents": [{"parts": [{"text": prompt_text}]}],
            "generationConfig": {"temperature": 0.2, "topP": 0.8, "maxOutputTokens": 4096},
        }
        response = requests.post(endpoint, headers=headers, json=payload, timeout=60)
        response.raise_for_status()
        parsed = response.json()
        return parsed["candidates"][0]["content"]["parts"][0]["text"]

    headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}

    try:
        raw_text = _request_cleanup(prompt)
        data = _extract_json_object(raw_text)
        if data is None:
            logger.warning("AI SEO cleanup invalid model response. Preview=%s", (raw_text or "")[:300])
            retry_prompt = f"""
Your previous response was invalid. Return only a valid JSON object with these keys:
{{
  "title": "...",
  "meta_title": "...",
  "meta_description": "...",
  "excerpt": "...",
  "body_html": "...",
  "target_keyword": "...",
  "changes": ["..."]
}}
No markdown fences.
No commentary.
No explanation outside JSON.

Current fields JSON:
{json.dumps(safe_fields, ensure_ascii=False)}

SEO warnings: {json.dumps(warnings, ensure_ascii=False)}
SEO suggestions: {json.dumps(suggestions, ensure_ascii=False)}
internal_link_suggestions: {json.dumps(link_rows, ensure_ascii=False)}
""".strip()
            raw_text = _request_cleanup(retry_prompt)
            data = _extract_json_object(raw_text)
            if data is None:
                logger.warning("AI SEO cleanup invalid model response. Preview=%s", (raw_text or "")[:300])
                result["error"] = "AI SEO cleanup failed: Gemini returned text that could not be parsed as JSON."
                return result

        updated = safe_fields.copy()
        parsed_title = data.get("title") if isinstance(data.get("title"), str) else ""
        parsed_meta_title = data.get("meta_title") if isinstance(data.get("meta_title"), str) else ""
        parsed_meta_desc = data.get("meta_description") if isinstance(data.get("meta_description"), str) else ""
        parsed_excerpt = data.get("excerpt") if isinstance(data.get("excerpt"), str) else ""
        parsed_body_html = data.get("body_html") if isinstance(data.get("body_html"), str) else ""
        parsed_target_keyword = data.get("target_keyword") if isinstance(data.get("target_keyword"), str) else ""

        recovered_html = ""
        if not parsed_body_html and _looks_like_html(raw_text):
            recovered_html = raw_text.strip()
        final_body_html = (parsed_body_html or recovered_html or "").strip()
        if not final_body_html:
            result["error"] = "AI SEO cleanup failed: Gemini returned text that could not be parsed as JSON."
            return result

        final_title = parsed_title.strip() or safe_fields["title"]
        final_meta_title = parsed_meta_title.strip() or safe_fields.get("meta_title") or final_title
        final_meta_description = parsed_meta_desc.strip() or safe_fields.get("meta_description") or _first_chars_text(final_body_html, 220)
        final_excerpt = parsed_excerpt.strip() or safe_fields.get("excerpt") or _first_chars_text(final_body_html, 220)
        final_target_keyword = parsed_target_keyword.strip() or safe_fields["target_keyword"]

        updated["title"] = final_title
        updated["meta_title"] = final_meta_title
        updated["meta_description"] = final_meta_description
        updated["excerpt"] = final_excerpt
        updated["body_html"] = final_body_html
        updated["target_keyword"] = final_target_keyword

        raw_changes = data.get("changes") if isinstance(data.get("changes"), list) else []
        changes = [str(x).strip() for x in raw_changes if str(x).strip()][:20] or ["AI SEO cleanup applied."]
        result["ok"] = True
        result["fields"] = updated
        result["changes"] = changes
        return result
    except requests.HTTPError as exc:
        summary = _gemini_error_summary(exc.response) if exc.response is not None else str(exc)[:200]
        result["error"] = f"AI SEO cleanup failed: {summary}"
        return result
    except requests.RequestException as exc:
        result["error"] = f"AI SEO cleanup failed: {str(exc)[:200]}"
        return result
    except Exception:
        result["error"] = "AI SEO cleanup failed: invalid model response."
        return result
