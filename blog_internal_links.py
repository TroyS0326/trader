import re
from html import unescape

_INTERNAL_LINK_MAP = [
    {
        "url": "/playbook",
        "page_title": "Trading Playbook",
        "anchors": ["trading playbook", "rules-based trading playbook", "XeanVI playbook"],
        "keywords": [
            "playbook", "rules", "setup criteria", "entry rules", "exit rules",
            "stop-loss", "target", "discipline", "trade plan", "risk rules",
        ],
        "reason": "This post discusses rules, setups, or execution discipline.",
        "strong_topic_match": True,
    },
    {
        "url": "/features",
        "page_title": "Features",
        "anchors": ["AI trading automation features", "XeanVI features", "trading automation tools"],
        "keywords": [
            "features", "automation", "scanner", "risk controls", "dashboard",
            "signals", "execution engine", "workflow", "alerts",
        ],
        "reason": "This post discusses platform features, automation, scanning, or risk controls.",
        "strong_topic_match": True,
    },
    {
        "url": "/broker-integration",
        "page_title": "Broker Integration",
        "anchors": ["broker-connected trading workflow", "broker integration", "Alpaca broker connection"],
        "keywords": [
            "broker", "alpaca", "api", "paper trading", "live trading",
            "execution", "orders", "bracket orders", "account connection",
        ],
        "reason": "This post discusses broker APIs, Alpaca, paper trading, or live execution.",
        "strong_topic_match": True,
    },
    {
        "url": "/pricing",
        "page_title": "Pricing",
        "anchors": ["XeanVI pricing", "subscription options", "platform pricing"],
        "keywords": [
            "pricing", "subscription", "cost", "plan", "upgrade", "pro",
            "paid", "monthly", "annual",
        ],
        "reason": "This post has buying intent or compares subscription value.",
        "strong_topic_match": False,
    },
    {
        "url": "/signup",
        "page_title": "Create Account",
        "anchors": ["create a XeanVI account", "start with XeanVI", "join XeanVI"],
        "keywords": ["start", "create account", "signup", "sign up", "onboarding", "begin", "try", "join"],
        "reason": "This post is near the bottom-funnel and suitable for a soft CTA.",
        "strong_topic_match": False,
    },
    {
        "url": "/transparency",
        "page_title": "AI Logic and Transparency",
        "anchors": ["XeanVI AI logic and transparency", "AI trading logic", "platform transparency"],
        "keywords": [
            "ai", "artificial intelligence", "logic", "transparency", "risk",
            "explain", "how it works", "automation safety", "decision",
        ],
        "reason": "This post discusses AI, automation, risk, or how the platform works.",
        "strong_topic_match": True,
    },
    {
        "url": "/blog",
        "page_title": "Blog",
        "anchors": ["XeanVI blog", "trading education articles", "day trading education"],
        "keywords": ["learn", "education", "guide", "beginner", "explained", "what is", "how to", "article"],
        "reason": "This post references related educational content.",
        "strong_topic_match": False,
    },
]


_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def _strip_html(text: str) -> str:
    plain = _HTML_TAG_RE.sub(" ", text or "")
    plain = unescape(plain)
    return _WHITESPACE_RE.sub(" ", plain).strip()


def _normalize(text: str) -> str:
    return (text or "").lower()


def _anchor_for_match(anchors, matched_keyword: str) -> str:
    kw = _normalize(matched_keyword)
    for anchor in anchors:
        if kw and kw in _normalize(anchor):
            return anchor
    return anchors[0] if anchors else ""


def suggest_internal_links(
    title: str = "",
    target_keyword: str = "",
    excerpt: str = "",
    body_html: str = "",
    existing_links: list[str] | None = None,
    max_suggestions: int = 6,
) -> list[dict]:
    title_text = _normalize(_strip_html(title))
    keyword_text = _normalize(_strip_html(target_keyword))
    excerpt_text = _normalize(_strip_html(excerpt))
    body_text = _normalize(_strip_html(body_html))

    blocked_urls = set()
    for url in existing_links or []:
        if url:
            blocked_urls.add(url.strip().lower())

    rendered_text = f"{body_html or ''} {body_text}"
    rendered_text_lower = rendered_text.lower()

    suggestions = []
    for link in _INTERNAL_LINK_MAP:
        url = link["url"]
        if url.lower() in blocked_urls:
            continue
        if url.lower() in rendered_text_lower:
            continue

        score = 0
        matched_keyword = ""
        for keyword in link["keywords"]:
            kw = keyword.lower()
            if not kw:
                continue
            if kw in title_text:
                score += 50
                matched_keyword = matched_keyword or keyword
            if kw in keyword_text:
                score += 35
                matched_keyword = matched_keyword or keyword
            if kw in excerpt_text:
                score += 25
                matched_keyword = matched_keyword or keyword
            if kw in body_text:
                score += 10
                matched_keyword = matched_keyword or keyword

        trading_risk_terms = ("trading", "risk", "setup", "execution", "rules", "discipline")
        if link["strong_topic_match"] and any(term in f"{title_text} {keyword_text} {excerpt_text} {body_text}" for term in trading_risk_terms):
            score += 10

        score = min(score, 100)
        if score < 20:
            continue

        suggestions.append({
            "page_title": link["page_title"],
            "url": url,
            "anchor_text": _anchor_for_match(link["anchors"], matched_keyword),
            "reason": link["reason"],
            "priority": score,
        })

    suggestions.sort(key=lambda item: (-item["priority"], item["page_title"]))
    return suggestions[: max(0, int(max_suggestions))]
