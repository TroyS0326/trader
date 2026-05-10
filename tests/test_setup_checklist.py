import os
from types import SimpleNamespace

for k in ["SECRET_KEY","TOKEN_ENCRYPTION_KEY","ALPACA_CLIENT_ID","ALPACA_CLIENT_SECRET","ALPACA_REDIRECT_URI","FINNHUB_API_KEY","GEMINI_API_KEY"]:
    os.environ.setdefault(k, "secure-value")

import app as app_module


def _user(**overrides):
    base = dict(
        alpaca_paper_access_token=None,
        alpaca_paper_account_id=None,
        paper_bankroll_set=False,
        paper_bankroll=0,
        live_bankroll=0,
        playbook_reviewed=False,
        transparency_reviewed=False,
        first_scan_completed=False,
        scan_preview_completed=False,
        alpaca_live_account_id=None,
        alpaca_live_access_token=None,
        subscription_status='free',
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_setup_checklist_sections_and_required_items():
    with app_module.app.test_request_context('/'):
        checklist = app_module.get_user_setup_checklist(_user())

    assert [i['label'] for i in checklist['paper_items']] == [
        'Connect Alpaca Paper Account',
        'Configure Paper Bankroll',
        'Run First Paper Scan',
    ]
    assert [i['label'] for i in checklist['live_items']] == [
        'Live Plan Access',
        'Connect Alpaca Live Account',
        'Configure Live Risk Controls',
    ]
    assert [i['label'] for i in checklist['recommended_items']] == [
        'Review Trading Playbook',
        'Review AI Logic',
    ]


def test_recommended_reviews_are_non_blocking_for_required_completion():
    with app_module.app.test_request_context('/'):
        checklist = app_module.get_user_setup_checklist(_user(
            alpaca_paper_access_token='paper-token',
            paper_bankroll_set=True,
            paper_bankroll=1000,
            live_bankroll=2500,
            subscription_status='pro',
            alpaca_live_access_token='live-token',
            playbook_reviewed=False,
            transparency_reviewed=False,
        ))

    assert checklist['total_required'] == 5
    assert checklist['completed_required'] == 5
    assert checklist['percent_complete'] == 100


def test_live_risk_controls_not_completed_by_paper_bankroll_only():
    with app_module.app.test_request_context('/'):
        checklist = app_module.get_user_setup_checklist(_user(
            paper_bankroll_set=True,
            paper_bankroll=1000,
            live_bankroll=0,
            subscription_status='pro',
            alpaca_live_access_token='live-token',
        ))

    live_risk = next(i for i in checklist['live_items'] if i['field'] == 'live_risk_controls')
    assert live_risk['label'] == 'Configure Live Risk Controls'
    assert live_risk['completed'] is False
    assert live_risk['status'] == 'Required'


def test_live_plan_access_past_due_counts_as_grace_period():
    with app_module.app.test_request_context('/'):
        checklist = app_module.get_user_setup_checklist(_user(subscription_status='past_due'))

    live_plan = next(i for i in checklist['live_items'] if i['field'] == 'live_plan_access')
    assert live_plan['completed'] is True
    assert 'grace-period' in live_plan['description']
    assert 'grace period' in live_plan['completed_note']


def test_live_connect_cta_present_without_playbook_review():
    with app_module.app.test_request_context('/'):
        checklist = app_module.get_user_setup_checklist(_user(subscription_status='pro', playbook_reviewed=False))

    live_connect = next(i for i in checklist['live_items'] if i['field'] == 'alpaca_live_connected')
    assert live_connect['action_label'] == 'Connect Live Account'
    assert live_connect['completed'] is False


def test_setup_checklist_route_renders_with_expected_context(monkeypatch):
    user = _user(id=123, is_authenticated=True)

    with app_module.app.test_request_context('/setup-checklist'):
        monkeypatch.setattr(app_module, 'track_user_event', lambda *args, **kwargs: None)
        monkeypatch.setattr(app_module, 'get_user_setup_checklist', lambda _u: {'paper_items': [], 'live_items': [], 'recommended_items': [], 'items': [], 'completed_required': 0, 'total_required': 5, 'percent_complete': 0, 'core_complete': False})
        monkeypatch.setattr(app_module, 'render_template', lambda *args, **kwargs: 'ok')
        monkeypatch.setattr(app_module, 'current_user', user)

        resp = app_module.setup_checklist.__wrapped__()

    assert resp == 'ok'
