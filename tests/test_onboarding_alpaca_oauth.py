import sys
from pathlib import Path
import re
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import os
import types
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

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

REQUIRED_DISCLOSURE_TEXT = (
    "By allowing XeanVI to access your Alpaca account, you are granting XeanVI access to your account information "
    "and authorization to place transactions at your direction. Alpaca does not warrant or guarantee that XeanVI "
    "will work as advertised or expected. Before authorizing, learn more about XeanVI."
)


def _normalize_rendered_text(html: str) -> str:
    without_tags = re.sub(r"<[^>]+>", " ", html)
    normalized = " ".join(without_tags.split())
    return re.sub(r"\s+([.,;:!?])", r"\1", normalized)


def _user(**overrides):
    base = dict(
        id=123,
        onboarding_completed=False,
        broker_connection_started=False,
        alpaca_paper_access_token=None,
        alpaca_paper_account_id=None,
        alpaca_live_access_token=None,
        alpaca_live_account_id=None,
        paper_bankroll_set=False,
        trading_mode='paper',
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


def test_onboarding_disclosure_present_before_both_connect_ctas_when_disconnected(monkeypatch):
    user = _user()

    with app_module.app.test_request_context('/onboarding'):
        monkeypatch.setattr(app_module, 'current_user', user)
        html = app_module.render_template('onboarding.html', current_user=user, setup_checklist={})

    normalized = _normalize_rendered_text(html)
    assert REQUIRED_DISCLOSURE_TEXT in normalized
    legacy_wording = 'authorization to place transactions in your account at your ' + 'direction'
    assert legacy_wording not in normalized

    assert html.count('class="alpaca-auth-disclosure"') == 2
    paper_idx = html.index('/alpaca/login?env=paper')
    live_idx = html.index('/alpaca/login?env=live')

    first_disclosure_idx = html.index('class="alpaca-auth-disclosure"')
    second_disclosure_idx = html.index('class="alpaca-auth-disclosure"', first_disclosure_idx + 1)
    assert first_disclosure_idx < paper_idx
    assert second_disclosure_idx < live_idx


def test_onboarding_live_disclosure_hidden_when_live_already_connected(monkeypatch):
    user = _user(alpaca_live_access_token='live-token')

    with app_module.app.test_request_context('/onboarding'):
        monkeypatch.setattr(app_module, 'current_user', user)
        html = app_module.render_template('onboarding.html', current_user=user, setup_checklist={})

    assert html.count('class="alpaca-auth-disclosure"') == 1
    assert '/alpaca/login?env=live' not in html


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


def test_alpaca_login_accepts_live_env(monkeypatch):
    user = _user()
    with app_module.app.test_request_context('/alpaca/login?env=live'):
        monkeypatch.setattr(app_module, 'current_user', user)
        monkeypatch.setattr(app_module, 'track_user_event', lambda *args, **kwargs: None)
        monkeypatch.setattr(app_module.db.session, 'commit', lambda: None)
        response = app_module.alpaca_login.__wrapped__()
        query = parse_qs(urlparse(response.location).query)
        assert query.get('env') == ['live']


def test_live_mode_unlocked_requires_onboarding_and_live_connection():
    assert app_module.live_mode_unlocked(_user(onboarding_completed=False, alpaca_live_access_token='x')) is False
    assert app_module.live_mode_unlocked(_user(onboarding_completed=True, alpaca_live_access_token=None)) is False
    assert app_module.live_mode_unlocked(_user(onboarding_completed=True, alpaca_live_access_token='x', alpaca_paper_access_token='p', paper_bankroll_set=True, paper_bankroll=1000.0)) is True


def test_alpaca_login_missing_config_flashes_and_redirects_onboarding(monkeypatch):
    user = _user()

    with app_module.app.test_request_context('/alpaca/login?env=paper'):
        monkeypatch.setattr(app_module, 'current_user', user)
        monkeypatch.setattr(app_module, 'track_user_event', lambda *args, **kwargs: None)

        monkeypatch.setitem(app_module.app.config, 'ALPACA_CLIENT_ID', '')

        response = app_module.alpaca_login.__wrapped__()
        assert response.status_code == 302
        assert response.location.endswith('/onboarding')


def test_password_reset_and_brevo_helpers_do_not_reference_scanner_diag(monkeypatch):
    user = SimpleNamespace(id=7, email='user@example.com', full_name='Jane Doe', password_hash='hash', subscription_status='pro')

    class Resp:
        def __init__(self, status_code=202, text='ok'):
            self.status_code = status_code
            self.text = text
        def json(self):
            return {'access_token': 'token'}

    monkeypatch.setenv('BREVO_API_KEY', 'k')
    monkeypatch.setenv('BREVO_RESET_PASSWORD_TEMPLATE_ID', '123')
    monkeypatch.setattr(app_module.requests, 'post', lambda *a, **k: Resp())
    monkeypatch.setattr(app_module.requests, 'put', lambda *a, **k: Resp(200))

    token = app_module.generate_password_reset_token(user)
    assert isinstance(token, str) and token
    assert app_module.send_password_reset_email(user, 'https://x/reset') is True
    assert app_module.add_signup_user_to_brevo(user) in {True, False}
    assert app_module.update_brevo_contact_attributes(user, {'foo': 'bar'}) is True


def test_alpaca_callback_path_no_nameerror(monkeypatch):
    user = _user(id=123, onboarding_completed=True, alpaca_paper_access_token='paper', alpaca_paper_account_id='acct')
    monkeypatch.setattr(app_module, 'current_user', user)
    monkeypatch.setattr(app_module, 'detect_and_store_alpaca_connection', lambda *a, **k: {'paper_connected': True, 'live_connected': False})
    monkeypatch.setattr(app_module, 'onboarding_requirements_met', lambda *a, **k: True)
    monkeypatch.setattr(app_module, 'update_brevo_contact_attributes', lambda *a, **k: True)
    monkeypatch.setattr(app_module.db.session, 'commit', lambda: None)

    class Resp:
        status_code = 200
        def json(self):
            return {'access_token': 'tok'}

    monkeypatch.setattr(app_module.requests, 'post', lambda *a, **k: Resp())

    with app_module.app.test_request_context('/alpaca/callback?state=ok&code=abc'):
        from flask import session
        session['oauth_state'] = 'ok'
        session['alpaca_oauth_user_id'] = 123
        session['alpaca_oauth_env'] = 'paper'
        resp = app_module.alpaca_callback.__wrapped__()
        assert resp.status_code == 302
