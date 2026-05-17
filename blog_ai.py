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
ALLOWED_TAGS = "h2, h3, p, ul, ol, li, strong, em, blockquote, a"
SUGGESTED_INTERNAL_LINKS = [
    "/playbook",
    "/features",
    "/broker-integration",
    "/pricing",
    "/signup",
    "/transparency",
    "/blog",
]
logger = logging.getLogger(__name__)


def _clean_text(value: str, fallback: str = "") -> str:
    return (value or fallback).strip()


def _strip_html_tags(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", text).strip()


def _truncate_text(value: str, max_length: int) -> str:
    text = _clean_text(value)
    if len(text) <= max_length:
        return text
    return text[:max_length].rstrip()


def _strip_json_fences(text: str) -> str:
    cleaned = (text or "").strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[len("```json"):].strip()
    elif cleaned.startswith("```"):
        cleaned = cleaned[len("```"):].strip()

    if cleaned.endswith("```"):
        cleaned = cleaned[:-len("```")].strip()

    return cleaned


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


def generate_blog_draft(
    title: str,
    target_keyword: str = "",
    internal_links: list[str] | None = None,
    notes: str = "",
) -> dict[str, Any]:
    title = _clean_text(title)
    keyword = _clean_text(target_keyword)
    notes = _clean_text(notes)
    requested_links = [link.strip() for link in (internal_links or []) if (link or "").strip()]

    result: dict[str, Any] = {
        "ok": False,
        "title": title,
        "meta_title": "",
        "meta_description": "",
        "excerpt": "",
        "body_html": "",
        "target_keyword": keyword,
        "error": None,
    }

    if not title:
        result["error"] = "A title is required to generate an AI draft."
        return result

    api_key = os.getenv("GEMINI_API_KEY")
    if not _clean_text(api_key):
        result["error"] = "AI draft generation is not configured. Missing GEMINI_API_KEY."
        return result
    api_key = _clean_text(api_key)

    model_name = os.getenv("GEMINI_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL

    prompt = f"""
You are an SEO content strategist and cautious financial education writer for XeanVI, an AI-powered trading automation and execution discipline platform.

Return strict JSON only. Do not include markdown fences or commentary outside JSON.
The JSON object should include title, body_html, meta_title, meta_description, excerpt, and target_keyword when available. If no target keyword is provided, use an empty string.

Return this JSON object shape:
{{
  "title": "...",
  "meta_title": "...",
  "meta_description": "...",
  "excerpt": "...",
  "body_html": "...",
  "target_keyword": "..."
}}

Article requirements:
- Original, helpful, SEO-friendly educational content for retail day traders.
- Accurate and cautious tone.
- No fake statistics.
- No guaranteed profits.
- No financial advice or personalized investment advice.
- Do not claim XeanVI guarantees wins.
- body_html must use only these tags: {ALLOWED_TAGS}.
- body_html must not include script, style, iframe, form, img, table, or JavaScript links.
- Include internal links naturally when relevant from this set: {', '.join(SUGGESTED_INTERNAL_LINKS)}.
- Do not force all links into every draft.
- Focus on topics like trading discipline, automation workflows, playbooks, paper trading, risk management, ORB, VWAP, bracket orders, and day-trading workflow.

Input:
- Title: {title}
- Target keyword: {keyword or 'None provided'}
- Internal links requested by admin: {', '.join(requested_links) if requested_links else 'None'}
- Notes/Angle: {notes or 'None'}
""".strip()

    endpoint = GEMINI_ENDPOINT_TEMPLATE.format(model_name=model_name)
    payload = {
        "contents": [
            {
                "parts": [
                    {
                        "text": prompt,
                    }
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.4,
            "topP": 0.9,
            "maxOutputTokens": 4096,
        },
    }
    headers = {
        "x-goog-api-key": api_key,
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(endpoint, headers=headers, json=payload, timeout=60)
        response.raise_for_status()
        parsed = response.json()
        try:
            raw_text = parsed["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError):
            result["error"] = "AI draft generation returned no text."
            return result

        text = _strip_json_fences(raw_text)
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("Gemini blog draft returned invalid JSON prefix: %s", text[:300])
            result["error"] = "AI draft generation returned invalid JSON. Try again."
            return result

        if not isinstance(data, dict):
            result["error"] = "AI draft generation returned invalid JSON. Try again."
            return result

        parsed_title = str(data.get("title") or title or "").strip()
        body_html = str(data.get("body_html") or "").strip()

        if not body_html:
            return {
                "ok": False,
                "title": parsed_title or title,
                "meta_title": "",
                "meta_description": "",
                "excerpt": "",
                "body_html": "",
                "target_keyword": keyword or "",
                "error": "AI draft generation returned no usable article body. Try again.",
            }

        plain_body = _strip_html_tags(body_html)

        excerpt = str(data.get("excerpt") or plain_body[:220]).strip()
        meta_title = str(data.get("meta_title") or parsed_title or title).strip()
        meta_description = str(data.get("meta_description") or excerpt[:158]).strip()
        final_target_keyword = str(data.get("target_keyword") or keyword or "").strip()

        return {
            "ok": True,
            "title": parsed_title or title,
            "meta_title": meta_title,
            "meta_description": meta_description,
            "excerpt": excerpt,
            "body_html": body_html,
            "target_keyword": final_target_keyword,
            "error": None,
        }
    except requests.HTTPError as exc:
        response = exc.response
        summary = _gemini_error_summary(response) if response is not None else str(exc)[:200]
        result["error"] = f"AI draft generation failed: {summary}"
        return result
    except requests.RequestException as exc:
        result["error"] = f"AI draft generation failed: {str(exc)[:200]}"
        return result
    except ValueError:
        result["error"] = "AI draft generation failed: invalid API response."
        return result
