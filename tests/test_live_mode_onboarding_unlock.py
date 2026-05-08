import os
from types import SimpleNamespace

for k in ["SECRET_KEY","TOKEN_ENCRYPTION_KEY","ALPACA_CLIENT_ID","ALPACA_CLIENT_SECRET","ALPACA_REDIRECT_URI","FINNHUB_API_KEY","GEMINI_API_KEY"]:
    os.environ.setdefault(k, "secure-value")

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
    user = _user(alpaca_live_access_token=None, onboarding_completed=False)

    with app_module.app.test_request_context('/api/update_mode', method='POST', json={'trading_mode': 'live'}):
        monkeypatch.setattr(app_module, 'current_user', user)
        response, status = app_module.update_mode.__wrapped__()
        assert status == 400
        payload = response.get_json()
        assert payload['ok'] is False


def test_update_mode_accepts_live_when_connected_and_onboarding_done(monkeypatch):
    user = _user(alpaca_live_access_token='live-token', alpaca_live_account_id='live-id', onboarding_completed=True)

    with app_module.app.test_request_context('/api/update_mode', method='POST', json={'trading_mode': 'live'}):
        monkeypatch.setattr(app_module, 'current_user', user)
        monkeypatch.setattr(app_module, 'fetch_and_sync_bankroll', lambda u: None)
        monkeypatch.setattr(app_module.db.session, 'commit', lambda: None)
        monkeypatch.setattr(app_module.db.session, 'refresh', lambda u: None)
        response = app_module.update_mode.__wrapped__()
        payload = response.get_json()
        assert payload['ok'] is True
        assert user.trading_mode == 'live'
