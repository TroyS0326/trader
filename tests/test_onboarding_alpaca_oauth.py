import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import os
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

for k in ["SECRET_KEY","TOKEN_ENCRYPTION_KEY","ALPACA_CLIENT_ID","ALPACA_CLIENT_SECRET","ALPACA_REDIRECT_URI","FINNHUB_API_KEY","GEMINI_API_KEY"]:
    os.environ.setdefault(k, "secure-value")

import app as app_module


def _user(**overrides):
    base = dict(
        id=123,
        onboarding_completed=False,
        broker_connection_started=False,
        alpaca_paper_access_token=None,
        alpaca_paper_account_id=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_onboarding_template_removes_logout_warning_and_uses_paper_oauth_link(monkeypatch):
    user = _user()

    with app_module.app.test_request_context('/onboarding'):
        monkeypatch.setattr(app_module, 'current_user', user)
        html = app_module.render_template('onboarding.html', current_user=user, setup_checklist={})

    assert 'Already logged into the wrong Alpaca account?' not in html
    assert 'Open Alpaca Logout' not in html
    assert 'Connect Alpaca Paper Account' in html
    assert '/alpaca/login?env=paper' in html


def test_alpaca_login_redirects_to_authorize_with_paper_env(monkeypatch):
    user = _user()

    with app_module.app.test_request_context('/alpaca/login?env=paper'):
        monkeypatch.setattr(app_module, 'current_user', user)
        monkeypatch.setattr(app_module, 'track_user_event', lambda *args, **kwargs: None)

        committed = {'called': False}
        monkeypatch.setattr(app_module.db.session, 'commit', lambda: committed.__setitem__('called', True))

        response = app_module.alpaca_login.__wrapped__()

        assert committed['called'] is True
        assert response.status_code == 302
        location = response.location

        parsed = urlparse(location)
        assert parsed.scheme == 'https'
        assert parsed.netloc == 'app.alpaca.markets'
        assert parsed.path == '/oauth/authorize'

        query = parse_qs(parsed.query)
        assert query.get('response_type') == ['code']
        assert query.get('client_id') == [app_module.app.config['ALPACA_CLIENT_ID']]
        assert query.get('redirect_uri') == [app_module.app.config['ALPACA_REDIRECT_URI']]
        assert query.get('env') == ['paper']
        assert query.get('state') and query['state'][0]


def test_alpaca_login_missing_config_flashes_and_redirects_onboarding(monkeypatch):
    user = _user()

    with app_module.app.test_request_context('/alpaca/login?env=paper'):
        monkeypatch.setattr(app_module, 'current_user', user)
        monkeypatch.setattr(app_module, 'track_user_event', lambda *args, **kwargs: None)

        monkeypatch.setitem(app_module.app.config, 'ALPACA_CLIENT_ID', '')

        response = app_module.alpaca_login.__wrapped__()
        assert response.status_code == 302
        assert response.location.endswith('/onboarding')
