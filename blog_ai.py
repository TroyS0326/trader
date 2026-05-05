import json
import os
from typing import Any

try:
    import google.generativeai as genai
except Exception:  # optional dependency/runtime safety
    genai = None


DEFAULT_MODEL = "gemini-2.5-flash"
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


def _clean_text(value: str, fallback: str = "") -> str:
    return (value or fallback).strip()


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

    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        result["error"] = "AI draft generation is not configured. Missing GEMINI_API_KEY."
        return result

    if genai is None:
        result["error"] = "AI draft generation is temporarily unavailable. Missing google-generativeai dependency."
        return result

    model_name = os.getenv("GEMINI_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL

    system_instruction = (
        "You are an SEO content strategist and cautious financial education writer for XeanVI, "
        "an AI-powered trading automation and execution discipline platform. Write helpful educational content. "
        "Do not promise profits, do not give personalized investment advice, and do not make unsupported performance claims."
    )

    prompt = f"""
Create a JSON object only, with keys: title, meta_title, meta_description, excerpt, body_html, target_keyword.

Article requirements:
- Original, helpful, SEO-friendly educational content for retail day traders.
- Accurate and cautious tone.
- No guaranteed profit language, no get-rich-quick framing, no fake stats/citations.
- Do not claim XeanVI guarantees wins.
- No personalized investment advice.
- body_html must contain ONLY these tags: {ALLOWED_TAGS}.
- Never include script, style, iframe, form, img, table tags.
- No JavaScript links.
- Include internal links naturally when relevant from this set: {', '.join(SUGGESTED_INTERNAL_LINKS)}.
- Do not force all links into every draft.
- Focus on topics like trading discipline, automation workflows, playbooks, paper trading,
  risk management, ORB, VWAP, bracket orders, and day-trading workflow.

Input:
- Title: {title}
- Target keyword: {keyword or 'None provided'}
- Internal links requested by admin: {', '.join(requested_links) if requested_links else 'None'}
- Notes/Angle: {notes or 'None'}

Return valid JSON only.
""".strip()

    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(model_name=model_name, system_instruction=system_instruction)
        response = model.generate_content(prompt)
        raw_text = (getattr(response, "text", "") or "").strip()
        data = json.loads(raw_text)

        result.update({
            "ok": True,
            "title": _clean_text(data.get("title") or title, fallback=title),
            "meta_title": _clean_text(data.get("meta_title", "")),
            "meta_description": _clean_text(data.get("meta_description", "")),
            "excerpt": _clean_text(data.get("excerpt", "")),
            "body_html": _clean_text(data.get("body_html", "")),
            "target_keyword": _clean_text(data.get("target_keyword") or keyword, fallback=keyword),
            "error": None,
        })
        return result
    except Exception:
        result["error"] = "AI draft generation failed. Please try again or write manually."
        return result
