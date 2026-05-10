from pathlib import Path
import json
import re

BANNED_PHRASES = [
    'guarantee profits',
    'guaranteed returns',
    'risk-free',
    'beat the market',
    'sure wins',
    'automatic profits',
    'guaranteed accuracy',
    'no-loss',
    'foolproof',
    'set it and forget it',
    'effortless profits',
]

PAGE_EXPECTATIONS = {
    'templates/landing.html': 'https://xeanvi.com/',
    'templates/upgrade.html': 'https://xeanvi.com/pricing',
    'templates/signup.html': 'https://xeanvi.com/signup',
    'templates/login.html': 'https://xeanvi.com/login',
    'templates/features.html': 'https://xeanvi.com/features',
    'templates/playbook.html': 'https://xeanvi.com/playbook',
    'templates/broker_integration.html': 'https://xeanvi.com/broker-integration',
    'templates/transparency.html': 'https://xeanvi.com/transparency',
    'templates/blog_index.html': 'https://xeanvi.com/blog',
    'templates/trading_automation.html': 'https://xeanvi.com/trading-automation',
}
FAQ_SCHEMA_PAGES = {
    'templates/landing.html',
    'templates/upgrade.html',
    'templates/features.html',
    'templates/playbook.html',
    'templates/broker_integration.html',
    'templates/transparency.html',
}


def _read(path: str) -> str:
    return Path(path).read_text(encoding='utf-8')


def _extract_title(html: str) -> str:
    m = re.search(r'<title>(.*?)</title>', html, re.IGNORECASE | re.DOTALL)
    assert m
    return m.group(1).strip()


def _extract_jsonld_blocks(html: str) -> list[str]:
    return re.findall(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>\s*(.*?)\s*</script>',
        html,
        re.IGNORECASE | re.DOTALL,
    )


def _normalize_text(value: str) -> str:
    return re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9]+', ' ', value.lower())).strip()


def _strip_non_visible_content(html: str) -> str:
    without_comments = re.sub(r'<!--.*?-->', ' ', html, flags=re.DOTALL)
    without_scripts = re.sub(r'<script\b[^>]*>.*?</script>', ' ', without_comments, flags=re.IGNORECASE | re.DOTALL)
    without_styles = re.sub(r'<style\b[^>]*>.*?</style>', ' ', without_scripts, flags=re.IGNORECASE | re.DOTALL)
    return without_styles


def _contains_visible_question(html: str, question: str) -> bool:
    visible_html = _strip_non_visible_content(html)
    visible_text = _normalize_text(re.sub(r'<[^>]+>', ' ', visible_html))
    normalized_question = _normalize_text(question)
    if normalized_question in visible_text:
        return True

    question_tokens = [token for token in normalized_question.split() if len(token) > 2]
    if not question_tokens:
        return False
    matched = sum(token in visible_text for token in question_tokens)
    return matched / len(question_tokens) >= 0.8


def test_visible_question_check_ignores_jsonld_and_accepts_visible_markup():
    question = 'How does broker integration work?'
    jsonld_only_html = f'''
    <section>
      <script type="application/ld+json">{{"@type":"FAQPage","mainEntity":[{{"@type":"Question","name":"{question}"}}]}}</script>
    </section>
    '''
    visible_markup_html = f'''
    <section>
      <h3>{question}</h3>
      <p>We connect by API keys.</p>
      <script type="application/ld+json">{{"@type":"FAQPage","mainEntity":[{{"@type":"Question","name":"Different JSON-LD question"}}]}}</script>
    </section>
    '''

    assert _contains_visible_question(jsonld_only_html, question) is False
    assert _contains_visible_question(visible_markup_html, question) is True


def test_public_pages_have_unique_titles_and_descriptions():
    titles = []
    for path, canonical in PAGE_EXPECTATIONS.items():
        html = _read(path)
        title = _extract_title(html)
        titles.append(title)
        assert title
        assert '<meta name="description" content="' in html
        assert f'<link rel="canonical" href="{canonical}">' in html

        if path != 'templates/login.html':
            assert '<meta property="og:title" content="' in html
            assert '<meta property="og:description" content="' in html

        assert '<meta name="twitter:title" content="' in html
        assert '<meta name="twitter:description" content="' in html

    assert len(titles) == len(set(titles))


def test_banned_risky_phrases_are_absent_from_public_seo_templates():
    for path in PAGE_EXPECTATIONS:
        html_lower = _read(path).lower()
        for phrase in BANNED_PHRASES:
            assert phrase not in html_lower, f"Found banned phrase '{phrase}' in {path}"


def test_landing_schema_offer_points_to_pricing_not_upgrade():
    html = _read('templates/landing.html')
    assert '"url": "https://xeanvi.com/pricing"' in html
    assert '"url": "https://xeanvi.com/upgrade"' not in html


def test_signup_robots_noindex_follow():
    html = _read('templates/signup.html')
    assert '<meta name="robots" content="noindex, follow">' in html


def test_blog_post_keeps_dynamic_seo_and_jsonld_support():
    html = _read('templates/blog_post.html')
    assert '<title>{{ post.meta_title or post.title }}</title>' in html
    assert '<meta name="description" content="{{ post.meta_description or post.excerpt or \'\' }}">' in html
    assert '<link rel="canonical" href="{{ canonical_url }}">' in html
    assert '<meta property="og:title" content="{{ post.meta_title or post.title }}">' in html
    assert '<meta name="twitter:title" content="{{ post.meta_title or post.title }}">' in html
    assert '"@type":"BlogPosting"' in html


def test_public_pages_cross_link_key_xeanvi_routes():
    expected_links = {
        'templates/landing.html': ['/features', '/playbook', '/broker-integration', '/pricing', '/signup?plan=monthly', '/transparency'],
        'templates/features.html': ['/playbook', '/broker-integration', '/pricing', '/signup?plan=monthly', '/transparency'],
        'templates/playbook.html': ['/features', '/broker-integration', '/signup?plan=monthly', '/transparency', '/blog'],
        'templates/broker_integration.html': ['/playbook', '/pricing', '/signup?plan=monthly', '/transparency'],
        'templates/upgrade.html': ['/features', '/broker-integration', '/signup?plan=monthly', '/transparency'],
        'templates/transparency.html': ['/broker-integration', '/playbook', '/features', '/pricing'],
        'templates/blog_index.html': ['/features', '/playbook', '/broker-integration', '/pricing', '/signup?plan=monthly', '/transparency'],
        'templates/blog_post.html': ['/features', '/playbook', '/broker-integration', '/transparency', '/blog'],
        'templates/trading_automation.html': ['/features', '/playbook', '/broker-integration', '/transparency', '/pricing', '/blog', '/about'],
    }
    for path, links in expected_links.items():
        html = _read(path)
        for link in links:
            assert f'href="{link}"' in html, f"Missing expected link '{link}' in {path}"


def test_audited_templates_have_no_broken_internal_href_targets():
    audited_pages = [
        'templates/landing.html',
        'templates/features.html',
        'templates/playbook.html',
        'templates/broker_integration.html',
        'templates/upgrade.html',
        'templates/transparency.html',
        'templates/blog_index.html',
        'templates/blog_post.html',
        'templates/signup.html',
    ]
    valid_prefixes = (
        '/', '#', 'mailto:', 'http://', 'https://', '{{', '{%',
    )
    for path in audited_pages:
        html = _read(path)
        href_values = re.findall(r'href="([^"]+)"', html)
        for href in href_values:
            assert href.startswith(valid_prefixes), f"Unexpected href format '{href}' in {path}"


def test_generic_anchor_text_spam_not_added_to_audited_templates():
    audited_pages = [
        'templates/landing.html',
        'templates/features.html',
        'templates/playbook.html',
        'templates/broker_integration.html',
        'templates/upgrade.html',
        'templates/transparency.html',
        'templates/blog_index.html',
        'templates/blog_post.html',
    ]
    html = "\n".join(_read(path).lower() for path in audited_pages)
    assert '>click here<' not in html


def test_public_pages_cover_core_positioning_terms():
    audited_pages = [
        'templates/landing.html',
        'templates/features.html',
        'templates/playbook.html',
        'templates/broker_integration.html',
        'templates/upgrade.html',
        'templates/transparency.html',
        'templates/blog_index.html',
    ]
    html = "\n".join(_read(path).lower() for path in audited_pages)
    for term in (
        'automated scanning',
        'playbook',
        'broker',
        'paper testing',
        'bracket order',
        'risk controls',
        'transparency',
    ):
        assert term in html, f"Expected core positioning term '{term}' to appear across public pages"


def test_faq_schema_is_present_only_on_pages_with_visible_faq_content():
    for path in PAGE_EXPECTATIONS:
        html = _read(path)
        faq_blocks = []
        for block in _extract_jsonld_blocks(html):
            if '{%' in block or '{{' in block:
                continue
            data = json.loads(block)
            if data.get('@type') == 'FAQPage':
                faq_blocks.append(data)

        if path in FAQ_SCHEMA_PAGES:
            assert _extract_jsonld_blocks(html), f"Expected at least one JSON-LD block for {path}"
            assert faq_blocks, f"Expected FAQPage JSON-LD for {path}"
            assert 'faq' in html.lower(), f"Expected visible FAQ content for {path}"
        else:
            assert not faq_blocks, f"Unexpected FAQPage JSON-LD in {path}"


def test_faq_schema_structure_and_phrase_safety():
    for path in FAQ_SCHEMA_PAGES:
        html = _read(path)
        faq_blocks = []
        for block in _extract_jsonld_blocks(html):
            if '{%' in block or '{{' in block:
                continue
            data = json.loads(block)
            if data.get('@type') == 'FAQPage':
                faq_blocks.append(data)

        assert faq_blocks, f"Missing FAQPage schema for {path}"
        assert len(faq_blocks) == 1, f"Expected exactly one FAQPage schema block for {path}"

        faq_schema = faq_blocks[0]
        assert faq_schema.get('@context') == 'https://schema.org'
        assert faq_schema.get('@type') == 'FAQPage'
        assert isinstance(faq_schema.get('mainEntity'), list) and faq_schema['mainEntity']
        for entity in faq_schema['mainEntity']:
            assert entity.get('@type') == 'Question'
            assert isinstance(entity.get('name'), str) and entity['name'].strip()
            accepted = entity.get('acceptedAnswer', {})
            assert accepted.get('@type') == 'Answer'
            assert isinstance(accepted.get('text'), str) and accepted['text'].strip()
            assert _contains_visible_question(html, entity['name']), (
                f"FAQ schema question must appear visibly in {path}: {entity['name']}"
            )
            combined = f"{entity['name']} {accepted['text']}".lower()
            for phrase in BANNED_PHRASES:
                assert phrase not in combined, f"Found banned phrase '{phrase}' in FAQ schema for {path}"
