from pathlib import Path


def test_landing_schema_offer_points_to_pricing_not_upgrade():
    html = Path('templates/landing.html').read_text(encoding='utf-8')
    assert '"url": "https://xeanvi.com/pricing"' in html
    assert '"url": "https://xeanvi.com/upgrade"' not in html


def test_signup_robots_noindex_follow():
    html = Path('templates/signup.html').read_text(encoding='utf-8')
    assert '<meta name="robots" content="noindex, follow">' in html


def test_upgrade_template_phrase_cleaned_up():
    html = Path('templates/upgrade.html').read_text(encoding='utf-8')
    assert 'Paper-mode testing for testing' not in html
    assert 'before live use' in html
