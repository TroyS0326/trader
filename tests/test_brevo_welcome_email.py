import pytest

pytest.importorskip('redis')

import app as app_module


class DummyResp:
    def __init__(self, status_code=202, text='ok'):
        self.status_code = status_code
        self.text = text


class DummyUser:
    def __init__(self, email='user@example.com', full_name='Jane Trader', user_id=7):
        self.email = email
        self.full_name = full_name
        self.id = user_id


def test_send_welcome_email_success(monkeypatch):
    user = DummyUser()
    monkeypatch.setattr(app_module.config, 'BREVO_WELCOME_TEMPLATE_ENABLED', True)
    monkeypatch.setattr(app_module.config, 'BREVO_API_KEY', 'k')
    monkeypatch.setattr(app_module.config, 'BREVO_WELCOME_TEMPLATE_ID', '42')
    monkeypatch.setattr(app_module.config, 'BREVO_SENDER_NAME', 'XeanVI')
    monkeypatch.setattr(app_module.config, 'BREVO_SENDER_EMAIL', 'support@xeanvi.com')
    monkeypatch.setattr(app_module.config, 'APP_BASE_URL', 'https://xeanvi.com')

    captured = {}

    def _post(url, json, headers, timeout):
        captured['url'] = url
        captured['json'] = json
        captured['headers'] = headers
        captured['timeout'] = timeout
        return DummyResp(status_code=202)

    monkeypatch.setattr(app_module.requests, 'post', _post)
    assert app_module.send_welcome_email(user) is True
    assert captured['url'] == 'https://api.brevo.com/v3/smtp/email'
    assert captured['timeout'] == 15
    assert captured['json']['templateId'] == 42
    assert captured['json']['tags'] == ['welcome', 'signup']
    assert captured['json']['params']['first_name'] == 'Jane'
    assert captured['json']['params']['full_name'] == 'Jane Trader'
    assert captured['json']['params']['email'] == 'user@example.com'
    assert captured['json']['params']['app_url'] == 'https://xeanvi.com'
    assert captured['json']['params']['login_url'] == 'https://xeanvi.com/login'


def test_send_welcome_email_disabled(monkeypatch):
    user = DummyUser()
    monkeypatch.setattr(app_module.config, 'BREVO_WELCOME_TEMPLATE_ENABLED', False)
    called = {'n': 0}
    monkeypatch.setattr(app_module.requests, 'post', lambda *a, **k: called.__setitem__('n', called['n'] + 1))
    assert app_module.send_welcome_email(user) is False
    assert called['n'] == 0


def test_send_welcome_email_missing_api_key(monkeypatch):
    user = DummyUser()
    monkeypatch.setattr(app_module.config, 'BREVO_WELCOME_TEMPLATE_ENABLED', True)
    monkeypatch.setattr(app_module.config, 'BREVO_API_KEY', '')
    monkeypatch.setattr(app_module.config, 'BREVO_WELCOME_TEMPLATE_ID', '5')
    called = {'n': 0}
    monkeypatch.setattr(app_module.requests, 'post', lambda *a, **k: called.__setitem__('n', called['n'] + 1))
    assert app_module.send_welcome_email(user) is False
    assert called['n'] == 0


def test_send_welcome_email_missing_template_id(monkeypatch):
    user = DummyUser()
    monkeypatch.setattr(app_module.config, 'BREVO_WELCOME_TEMPLATE_ENABLED', True)
    monkeypatch.setattr(app_module.config, 'BREVO_API_KEY', 'k')
    monkeypatch.setattr(app_module.config, 'BREVO_WELCOME_TEMPLATE_ID', '')
    called = {'n': 0}
    monkeypatch.setattr(app_module.requests, 'post', lambda *a, **k: called.__setitem__('n', called['n'] + 1))
    assert app_module.send_welcome_email(user) is False
    assert called['n'] == 0


def test_send_welcome_email_non_numeric_template_id(monkeypatch):
    user = DummyUser()
    monkeypatch.setattr(app_module.config, 'BREVO_WELCOME_TEMPLATE_ENABLED', True)
    monkeypatch.setattr(app_module.config, 'BREVO_API_KEY', 'k')
    monkeypatch.setattr(app_module.config, 'BREVO_WELCOME_TEMPLATE_ID', 'abc')
    called = {'n': 0}
    monkeypatch.setattr(app_module.requests, 'post', lambda *a, **k: called.__setitem__('n', called['n'] + 1))
    assert app_module.send_welcome_email(user) is False
    assert called['n'] == 0


def test_send_welcome_email_non_2xx(monkeypatch):
    user = DummyUser()
    monkeypatch.setattr(app_module.config, 'BREVO_WELCOME_TEMPLATE_ENABLED', True)
    monkeypatch.setattr(app_module.config, 'BREVO_API_KEY', 'k')
    monkeypatch.setattr(app_module.config, 'BREVO_WELCOME_TEMPLATE_ID', '7')
    monkeypatch.setattr(app_module.requests, 'post', lambda *a, **k: DummyResp(status_code=500, text='bad request'))
    assert app_module.send_welcome_email(user) is False


def test_send_welcome_email_request_exception(monkeypatch):
    user = DummyUser()
    monkeypatch.setattr(app_module.config, 'BREVO_WELCOME_TEMPLATE_ENABLED', True)
    monkeypatch.setattr(app_module.config, 'BREVO_API_KEY', 'k')
    monkeypatch.setattr(app_module.config, 'BREVO_WELCOME_TEMPLATE_ID', '7')

    def _raise(*args, **kwargs):
        raise RuntimeError('network error')

    monkeypatch.setattr(app_module.requests, 'post', _raise)
    assert app_module.send_welcome_email(user) is False
