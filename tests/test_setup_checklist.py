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
        playbook_reviewed=False,
        transparency_reviewed=False,
        first_scan_completed=False,
        scan_preview_completed=False,
        alpaca_live_account_id=None,
        alpaca_live_access_token=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_setup_checklist_order_and_labels():
    with app_module.app.test_request_context('/'):
        checklist = app_module.get_user_setup_checklist(_user())

    labels = [item['label'] for item in checklist['items']]
    assert labels[0] == 'Connect Alpaca Paper Account'
    assert labels[1] == 'Set Paper Money'
    assert labels == [
        'Connect Alpaca Paper Account',
        'Set Paper Money',
        'Review Trading Playbook',
        'Review AI Logic',
        'Run First Paper Scan',
    ]

    ambiguous = {'Connect Alpaca', 'Connect Broker', 'Broker Connection'}
    assert all(item['label'] not in ambiguous for item in checklist['items'])


def test_setup_checklist_paper_and_live_constraints():
    with app_module.app.test_request_context('/'):
        checklist = app_module.get_user_setup_checklist(_user())

    paper_items = [i for i in checklist['items'] if 'Paper' in i['label'] and 'Alpaca' in i['label']]
    live_items = [i for i in checklist['items'] if 'Live' in i['label'] and 'Alpaca' in i['label']]

    assert len(paper_items) == 1
    assert len(live_items) <= 1
    assert len(live_items) == 0


def test_live_item_optional_and_after_required_only_when_core_complete():
    complete_user = _user(
        alpaca_paper_access_token='token',
        paper_bankroll_set=True,
        paper_bankroll=1000,
        playbook_reviewed=True,
        transparency_reviewed=True,
        scan_preview_completed=True,
    )
    with app_module.app.test_request_context('/'):
        checklist = app_module.get_user_setup_checklist(complete_user)

    assert checklist['completed_required'] == checklist['total_required'] == 5
    assert checklist['percent_complete'] == 100
    assert checklist['items'][-1]['label'] == 'Optional: Connect Alpaca Live Account'
    assert checklist['items'][-1]['optional'] is True
    assert checklist['items'][-1]['required'] is False


def test_required_completion_excludes_optional_live():
    with app_module.app.test_request_context('/'):
        checklist = app_module.get_user_setup_checklist(
            _user(
                alpaca_paper_access_token='token',
                paper_bankroll_set=True,
                paper_bankroll=1000,
                playbook_reviewed=True,
                transparency_reviewed=True,
                first_scan_completed=True,
                alpaca_live_access_token='live-token',
            )
        )

    assert checklist['total_required'] == 5
    assert checklist['completed_required'] == 5
    assert checklist['percent_complete'] == 100


def test_setup_checklist_route_renders_with_authenticated_user(monkeypatch):
    user = _user(id=123, is_authenticated=True)

    with app_module.app.test_request_context('/setup-checklist'):
        monkeypatch.setattr(app_module, 'track_user_event', lambda *args, **kwargs: None)
        monkeypatch.setattr(app_module, 'get_user_setup_checklist', lambda _u: {'items': [], 'completed_required': 0, 'total_required': 5, 'percent_complete': 0, 'core_complete': False})
        monkeypatch.setattr(app_module, 'render_template', lambda *args, **kwargs: 'ok')
        monkeypatch.setattr(app_module, 'current_user', user)

        resp = app_module.setup_checklist.__wrapped__()

    assert resp == 'ok'
