from pathlib import Path
import re

BANNED_PHRASES = [
    'guarantee profits',
    'guaranteed returns',
    'risk-free',
    'beat the market',
    'sure wins',
    'automatic profits',
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
}


def _read(path: str) -> str:
    return Path(path).read_text(encoding='utf-8')


def _extract_title(html: str) -> str:
    m = re.search(r'<title>(.*?)</title>', html, re.IGNORECASE | re.DOTALL)
    assert m
    return m.group(1).strip()


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
