import os
import sys
import types
from types import SimpleNamespace

for k in ["SECRET_KEY","TOKEN_ENCRYPTION_KEY","ALPACA_CLIENT_ID","ALPACA_CLIENT_SECRET","ALPACA_REDIRECT_URI","FINNHUB_API_KEY","GEMINI_API_KEY"]:
    os.environ.setdefault(k, "secure-value")

if 'redis' not in sys.modules:
    redis_stub = types.SimpleNamespace(
        Redis=types.SimpleNamespace(from_url=lambda *a, **k: types.SimpleNamespace()),
        from_url=lambda *a, **k: types.SimpleNamespace(),
    )
    sys.modules['redis'] = redis_stub
if 'requests' not in sys.modules:
    sys.modules['requests'] = types.SimpleNamespace(get=lambda *a, **k: None, post=lambda *a, **k: None)
if 'stripe' not in sys.modules:
    sys.modules['stripe'] = types.SimpleNamespace(api_key='test')

import app as app_module
import onboarding as onboarding_module


def _user(**overrides):
    base = dict(
        id=1,
        subscription_status='pro',
        trading_mode='paper',
        onboarding_completed=False,
        paper_bankroll_set=True,
        alpaca_paper_access_token='paper-token',
        alpaca_paper_account_id='paper-id',
        alpaca_live_access_token=None,
        alpaca_live_account_id=None,
        paper_bankroll=1000.0,
        live_bankroll=0.0,
        bankroll=1000.0,
        sync_legacy_bankroll_from_active_mode=lambda: None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_detect_live_connection_does_not_overwrite_paper(monkeypatch):
    user = _user()

    monkeypatch.setattr(onboarding_module, '_fetch_account_payload', lambda token, url: {'id': 'live-123', 'equity': '4500'} if 'api.alpaca.markets' in url and 'paper-api' not in url else None)
    monkeypatch.setattr(onboarding_module.db.session, 'commit', lambda: None)

    result = onboarding_module.detect_and_store_alpaca_connection(user, 'live-token', env='live')

    assert result['live_connected'] is True
    assert user.alpaca_live_access_token == 'live-token'
    assert user.alpaca_live_account_id == 'live-123'
    assert user.alpaca_paper_access_token == 'paper-token'
    assert user.alpaca_paper_account_id == 'paper-id'


def test_update_mode_rejects_live_when_not_unlocked(monkeypatch):
    user = _user(
        alpaca_live_access_token='live-token',
        alpaca_live_account_id='live-id',
        onboarding_completed=False,
        paper_bankroll_set=False,
        paper_bankroll=0.0,
    )

    with app_module.app.test_request_context('/api/update_mode', method='POST', json={'trading_mode': 'live'}):
        monkeypatch.setattr(app_module, 'current_user', user)
        response, status = app_module.update_mode.__wrapped__()
        assert status == 400
        payload = response.get_json()
        assert payload['ok'] is False
        assert payload['message'] == 'Connect Alpaca Live in onboarding before enabling LIVE mode.'


def test_update_mode_rejects_paper_when_disconnected(monkeypatch):
    user = _user(alpaca_paper_access_token=None, alpaca_paper_account_id=None, onboarding_completed=False)

    with app_module.app.test_request_context('/api/update_mode', method='POST', json={'trading_mode': 'paper'}):
        monkeypatch.setattr(app_module, 'current_user', user)
        response, status = app_module.update_mode.__wrapped__()
        assert status == 400
        payload = response.get_json()
        assert payload['ok'] is False
        assert payload['message'] == 'Connect Alpaca Paper in onboarding before enabling PAPER mode.'


def test_update_mode_accepts_live_when_connected_and_onboarding_done(monkeypatch):
    user = _user(
        alpaca_live_access_token='live-token',
        alpaca_live_account_id='live-id',
        onboarding_completed=True,
        paper_bankroll_set=True,
        paper_bankroll=1000.0,
    )

    with app_module.app.test_request_context('/api/update_mode', method='POST', json={'trading_mode': 'live'}):
        monkeypatch.setattr(app_module, 'current_user', user)
        monkeypatch.setattr(app_module, 'fetch_and_sync_bankroll', lambda u: None)
        monkeypatch.setattr(app_module.db.session, 'commit', lambda: None)
        monkeypatch.setattr(app_module.db.session, 'refresh', lambda u: None)
        response = app_module.update_mode.__wrapped__()
        payload = response.get_json()
        assert payload['ok'] is True
        assert user.trading_mode == 'live'


def test_onboarding_requirements_requires_positive_paper_bankroll():
    user = _user(alpaca_live_access_token='live-token', alpaca_live_account_id='live-id', paper_bankroll_set=True, paper_bankroll=0.0)
    assert app_module.onboarding_requirements_met(user) is False


def test_live_connection_without_paper_bankroll_does_not_unlock():
    user = _user(alpaca_live_access_token='live-token', alpaca_live_account_id='live-id', paper_bankroll_set=False, paper_bankroll=0.0, onboarding_completed=False)
    assert app_module.onboarding_requirements_met(user) is False
    assert app_module.live_mode_unlocked(user) is False


def test_alpaca_logout_paper_clears_bankroll_and_onboarding(monkeypatch):
    user = _user(
        onboarding_completed=True,
        paper_bankroll_set=True,
        paper_bankroll=1500.0,
        alpaca_live_access_token='live-token',
        alpaca_live_account_id='live-id',
    )
    with app_module.app.test_request_context('/alpaca/logout?env=paper'):
        monkeypatch.setattr(app_module, 'current_user', user)
        monkeypatch.setattr(app_module.db.session, 'commit', lambda: None)
        response = app_module.alpaca_logout.__wrapped__()
        assert response.status_code == 302
        assert user.paper_bankroll == 0.0
        assert user.paper_bankroll_set is False
        assert user.onboarding_completed is False
        assert user.alpaca_live_access_token == 'live-token'


def test_alpaca_logout_live_while_active_forces_paper_mode(monkeypatch):
    user = _user(
        trading_mode='live',
        onboarding_completed=True,
        paper_bankroll_set=True,
        paper_bankroll=1000.0,
        alpaca_live_access_token='live-token',
        alpaca_live_account_id='live-id',
    )
    with app_module.app.test_request_context('/alpaca/logout?env=live'):
        monkeypatch.setattr(app_module, 'current_user', user)
        monkeypatch.setattr(app_module.db.session, 'commit', lambda: None)
        app_module.alpaca_logout.__wrapped__()
        assert user.alpaca_live_access_token is None
        assert user.alpaca_live_account_id is None
        assert user.trading_mode == 'paper'
        assert user.onboarding_completed is False
