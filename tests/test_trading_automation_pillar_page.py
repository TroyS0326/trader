import os
for k in ["SECRET_KEY","TOKEN_ENCRYPTION_KEY","ALPACA_CLIENT_ID","ALPACA_CLIENT_SECRET","ALPACA_REDIRECT_URI","FINNHUB_API_KEY","GEMINI_API_KEY"]:
    os.environ.setdefault(k, "test")

import json
import re
from pathlib import Path
import sys

sys.path.append(str(Path(__file__).resolve().parents[1]))
import app as app_module

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

FAQ_QUESTIONS = [
    'What is trading workflow automation?',
    'Is trading automation financial advice?',
    'Can I test automation before live trading?',
    'Does broker-connected automation control my funds?',
    'Do automated trading workflows remove risk?',
]


def test_trading_automation_route_renders_and_template_exists():
    assert Path('templates/trading_automation.html').exists()
    client = app_module.app.test_client()
    response = client.get('/trading-automation')
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert '<title>Trading Automation Software Guide | XeanVI</title>' in html


def test_trading_automation_seo_tags_and_internal_links():
    html = Path('templates/trading_automation.html').read_text(encoding='utf-8')
    assert '<meta name="description" content="' in html
    assert '<link rel="canonical" href="https://xeanvi.com/trading-automation">' in html
    assert '<meta property="og:title" content="Trading Automation Software Guide | XeanVI">' in html
    assert '<meta property="og:description" content="' in html
    assert '<meta name="twitter:title" content="Trading Automation Software Guide | XeanVI">' in html
    assert '<meta name="twitter:description" content="' in html
    assert 'https://xeanvi.com/static/seo-banner.webp' in html

    for link in ['/features', '/playbook', '/broker-integration', '/transparency', '/pricing', '/blog', '/about']:
        assert f'href="{link}"' in html


def test_trading_automation_has_scoped_accessible_link_styles():
    html = Path('templates/trading_automation.html').read_text(encoding='utf-8')
    assert '<main class="trading-automation-page">' in html
    assert '.trading-automation-page a {' in html
    assert 'color: var(--accent-blue);' in html
    assert 'text-decoration: underline;' in html
    assert 'text-underline-offset: 0.2em;' in html
    assert '.trading-automation-page a:hover,' in html
    assert '.trading-automation-page a:focus-visible {' in html
    assert 'text-decoration-color: var(--accent);' in html
    assert 'color: var(--text-main);' in html


def test_trading_automation_risk_language_and_banned_phrases():
    html = Path('templates/trading_automation.html').read_text(encoding='utf-8')
    html_lower = html.lower()

    assert 'does not remove market risk' in html_lower
    assert 'does not provide financial advice' in html_lower
    assert 'paper trading is not a guarantee of live performance' in html_lower
    assert 'you remain responsible' in html_lower

    for phrase in BANNED_PHRASES:
        assert phrase not in html_lower


def test_trading_automation_faq_jsonld_matches_visible_questions():
    html = Path('templates/trading_automation.html').read_text(encoding='utf-8')
    blocks = re.findall(r'<script[^>]*type="application/ld\+json"[^>]*>\s*(.*?)\s*</script>', html, re.DOTALL)
    faq_blocks = [json.loads(block) for block in blocks if '"@type": "FAQPage"' in block]
    assert faq_blocks, 'Expected FAQPage JSON-LD block'
    faq = faq_blocks[0]
    names = [q['name'] for q in faq['mainEntity']]

    for question in FAQ_QUESTIONS:
        assert question in names
        assert question in html


def test_sitemap_contains_trading_automation_page():
    with app_module.app.test_request_context('/sitemap.xml'):
        response = app_module.sitemap_xml()
        xml = response.get_data(as_text=True)
    assert 'https://xeanvi.com/trading-automation' in xml
