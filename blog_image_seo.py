import re


def _clean_text(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def generate_image_alt_caption(title: str, target_keyword: str = "", excerpt: str = "", body_html: str = "") -> dict:
    clean_title = _clean_text(title)
    clean_keyword = _clean_text(target_keyword)
    clean_excerpt = _clean_text(excerpt)
    clean_body = _clean_text(body_html)

    base_phrase = clean_keyword if clean_keyword else clean_title
    if clean_title and clean_keyword and clean_keyword.lower() not in clean_title.lower():
        alt_text = f"{clean_title} with focus on {clean_keyword} and disciplined market execution context"
    elif base_phrase:
        alt_text = f"{base_phrase} highlighting structured analysis and risk-aware trading workflow"
    else:
        alt_text = "Structured trading analysis layout with risk-aware market workflow notes"

    alt_text = re.sub(r"\s+", " ", alt_text).strip(" .")
    if len(alt_text) < 80:
        fallback_context = clean_excerpt or clean_body[:80] or "market context and disciplined risk planning"
        alt_text = f"{alt_text} for {fallback_context}".strip()
    if len(alt_text) > 140:
        alt_text = alt_text[:137].rstrip(" ,.-") + "..."

    caption_seed = clean_excerpt or clean_title or clean_keyword or "market analysis"
    if clean_keyword and clean_keyword.lower() not in caption_seed.lower():
        caption = f"{caption_seed} with practical context for {clean_keyword} and disciplined decision-making."
    else:
        caption = f"{caption_seed} with practical context and disciplined decision-making."
    caption = re.sub(r"\s+", " ", caption).strip()
    if len(caption) > 180:
        caption = caption[:177].rstrip(" ,.-") + "..."

    return {"alt_text": alt_text, "caption": caption}
