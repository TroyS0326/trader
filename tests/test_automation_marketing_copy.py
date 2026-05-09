from pathlib import Path


def test_landing_automation_message_present():
    landing = Path('templates/landing.html').read_text(encoding='utf-8')
    assert ('Automate Your Trading' in landing) or ('Automate Your Trading Rules' in landing)
    assert ('Click Scan' in landing) or ('click Scan' in landing)

    lower = landing.lower()
    for token in ('onboarding', 'broker', 'scan', 'automation'):
        assert token in lower


def test_features_automation_message_present():
    features = Path('templates/features.html').read_text(encoding='utf-8')
    assert ('Automated Broker Execution' in features) or ('Automated Bracket Order Routing' in features)

    lower = features.lower()
    assert ('automation follows' in lower) or ('configured rules' in lower)


def test_risk_disclaimers_preserved():
    combined = (
        Path('templates/landing.html').read_text(encoding='utf-8')
        + '\n'
        + Path('templates/features.html').read_text(encoding='utf-8')
    ).lower()
    assert 'does not provide financial advice' in combined
    assert 'trading involves risk' in combined
