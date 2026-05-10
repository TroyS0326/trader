from pathlib import Path
import json
import re


def _read(path: str) -> str:
    return Path(path).read_text(encoding='utf-8')


def test_about_page_route_exists_and_template_present():
    app_py = _read('app.py')
    about_html = _read('templates/about.html')
    assert "@app.route('/about')" in app_py
    assert "def about():" in app_py
    assert '<title>About XeanVI' in about_html


def test_about_page_links_core_trust_routes():
    html = _read('templates/about.html')
    for href in ['/transparency', '/playbook', '/broker-integration', '/pricing', '/contact']:
        assert f'href="{href}"' in html


def test_about_page_jsonld_is_valid_aboutpage():
    html = _read('templates/about.html')
    blocks = re.findall(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', html, re.DOTALL | re.IGNORECASE)
    assert blocks
    data = json.loads(blocks[0].strip())
    assert data.get('@type') == 'AboutPage'
    assert data.get('url') == 'https://xeanvi.com/about'


def test_blog_post_supports_byline_dates_disclosure_and_safeguard_links():
    html = _read('templates/blog_post.html')
    assert "post.author_name" in html
    assert "Published" in html
    assert "Updated" in html
    assert "not financial advice" in html.lower()
    assert "trading involves risk" in html.lower()
    for href in ['/transparency', '/playbook', '/broker-integration', '/about']:
        assert f'href="{href}"' in html


def test_blog_post_safeguards_section_appears_before_footer_include():
    html = _read('templates/blog_post.html')
    safeguards_index = html.index('Explore XeanVI safeguards')
    footer_index = html.index("{% include 'footer.html' %}")
    assert safeguards_index < footer_index


def test_banned_risky_phrases_absent_from_trust_templates():
    phrases = [
        'guarantee profits','guaranteed returns','risk-free','beat the market','sure wins',
        'automatic profits','guaranteed accuracy','no-loss','foolproof','set it and forget it','effortless profits',
    ]
    joined = '\n'.join(_read(p).lower() for p in ['templates/about.html', 'templates/blog_post.html', 'templates/blog_index.html'])
    for phrase in phrases:
        assert phrase not in joined
