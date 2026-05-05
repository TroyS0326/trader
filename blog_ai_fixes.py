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


def _gemini_error_summary(response: requests.Response) -> str:
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
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "topP": 0.8, "maxOutputTokens": 4096},
    }
    headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}

    try:
        response = requests.post(endpoint, headers=headers, json=payload, timeout=60)
        response.raise_for_status()
        parsed = response.json()
        raw_text = parsed["candidates"][0]["content"]["parts"][0]["text"]
        cleaned = _strip_json_fences(raw_text)
        data = json.loads(cleaned)
        if not isinstance(data, dict):
            raise ValueError("non-object JSON")

        updated = safe_fields.copy()
        for key in ("title", "meta_title", "meta_description", "excerpt", "body_html", "target_keyword"):
            if key in data and isinstance(data.get(key), str):
                updated[key] = data.get(key).strip() if key != "body_html" else (data.get(key) or "")
        if not updated.get("title"):
            updated["title"] = safe_fields["title"]
        changes = data.get("changes") if isinstance(data.get("changes"), list) else []
        result["ok"] = True
        result["fields"] = updated
        result["changes"] = [str(x).strip() for x in changes if str(x).strip()][:20]
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
