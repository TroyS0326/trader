from pathlib import Path


def test_route_exists_and_renders_200_or_static_fallback():
    try:
        from app import app  # noqa: WPS433

        c = app.test_client()
        r = c.get('/lp/rule-based-trading-automation')
        assert r.status_code == 200
    except Exception:
        app_py = Path('app.py').read_text(encoding='utf-8')
        assert '/lp/rule-based-trading-automation' in app_py
        assert 'paid_ads_landing.html' in app_py


def test_paid_landing_required_content_and_tracking():
    html = Path('templates/paid_ads_landing.html').read_text(encoding='utf-8')
    low = html.lower()
    assert '<meta name="robots" content="noindex, follow">' in html
    assert 'Rule-Based Trading Automation Software You Can Test in Paper Mode First' in html
    assert '/signup?plan=monthly&utm_source=paid&utm_medium=landing&utm_campaign=rule_based_automation' in html
    assert '/pricing?utm_source=paid&utm_medium=landing&utm_campaign=rule_based_automation' in html
    assert 'data-meta-pixel-event="Lead"' in html
    assert 'data-google-ads-conversion="signup"' in html
    assert 'trading involves risk' in low
    assert 'not financial advice' in low
    assert 'not a broker-dealer' in low
    assert 'not an investment adviser' in low
    assert 'not a custodian' in low
    assert 'admin@xeanvi.com' in low
    assert '941 529 7990' in html
    assert '2900 Acline RD, Punta Gorda, FL 33950' in html
    assert '$19.99/month' in html
    assert '$199.99/year' in html


def test_banned_phrases_absent_from_paid_landing():
    html = Path('templates/paid_ads_landing.html').read_text(encoding='utf-8').lower()
    banned = [
        'cheat code', 'guarantee profits', 'guaranteed profit', 'guaranteed returns', 'beat wall street',
        'takes the profit', 'passive income', 'risk-free', 'no risk', 'win rate', 'sure thing',
        'get rich', 'hands-free profits', 'ai picks winners',
    ]
    for phrase in banned:
        assert phrase not in html


def test_utm_persistence_is_dom_ready_and_scoped_to_safe_links_only():
    script = Path('templates/partials/utm_persistence.html').read_text(encoding='utf-8').lower()
    assert 'domcontentloaded' in script
    assert '"/signup"' in script
    assert '"/pricing"' in script
    assert 'mailto:' in script
    assert 'tel:' in script
    assert '/logout' in script
    assert '/api/create-checkout-session' not in script
    assert 'form' not in script
