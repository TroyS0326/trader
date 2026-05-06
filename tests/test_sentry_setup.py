import importlib
import os
import sys
import types
from unittest.mock import patch

import sentry_setup


def _install_fake_sentry(monkeypatch):
    calls = {}
    sdk = types.ModuleType('sentry_sdk')

    def fake_init(**kwargs):
        calls['kwargs'] = kwargs

    sdk.init = fake_init

    flask_mod = types.ModuleType('sentry_sdk.integrations.flask')
    logging_mod = types.ModuleType('sentry_sdk.integrations.logging')
    celery_mod = types.ModuleType('sentry_sdk.integrations.celery')

    class FlaskIntegration:
        pass

    class LoggingIntegration:
        def __init__(self, level=None, event_level=None):
            self.level = level
            self.event_level = event_level

    class CeleryIntegration:
        pass

    flask_mod.FlaskIntegration = FlaskIntegration
    logging_mod.LoggingIntegration = LoggingIntegration
    celery_mod.CeleryIntegration = CeleryIntegration

    monkeypatch.setitem(sys.modules, 'sentry_sdk', sdk)
    monkeypatch.setitem(sys.modules, 'sentry_sdk.integrations.flask', flask_mod)
    monkeypatch.setitem(sys.modules, 'sentry_sdk.integrations.logging', logging_mod)
    monkeypatch.setitem(sys.modules, 'sentry_sdk.integrations.celery', celery_mod)
    return calls


def test_init_sentry_returns_false_when_dsn_blank(monkeypatch):
    monkeypatch.setenv('SENTRY_DSN', '   ')
    assert sentry_setup.init_sentry() is False


def test_init_sentry_calls_sdk_init(monkeypatch):
    calls = _install_fake_sentry(monkeypatch)
    monkeypatch.setenv('SENTRY_DSN', 'https://example@sentry.io/1')
    monkeypatch.setenv('FLASK_ENV', 'staging')
    monkeypatch.setenv('SENTRY_RELEASE', 'abc123')
    monkeypatch.setenv('SENTRY_TRACES_SAMPLE_RATE', '0.3')
    monkeypatch.setenv('SENTRY_PROFILES_SAMPLE_RATE', '0.2')
    monkeypatch.delenv('SENTRY_SEND_DEFAULT_PII', raising=False)

    assert sentry_setup.init_sentry('xeanvi-web') is True
    kwargs = calls['kwargs']
    assert kwargs['dsn'] == 'https://example@sentry.io/1'
    assert kwargs['environment'] == 'staging'
    assert kwargs['release'] == 'abc123'
    assert kwargs['traces_sample_rate'] == 0.3
    assert kwargs['profiles_sample_rate'] == 0.2
    assert kwargs['send_default_pii'] is False
    assert kwargs['server_name'] == 'xeanvi-web'
    assert callable(kwargs['before_send'])


def test_init_sentry_parses_bad_sample_rates_safely(monkeypatch):
    calls = _install_fake_sentry(monkeypatch)
    monkeypatch.setenv('SENTRY_DSN', 'https://example@sentry.io/1')
    monkeypatch.setenv('SENTRY_TRACES_SAMPLE_RATE', 'not-a-float')
    monkeypatch.setenv('SENTRY_PROFILES_SAMPLE_RATE', '')

    assert sentry_setup.init_sentry() is True
    kwargs = calls['kwargs']
    assert kwargs['traces_sample_rate'] == 0.0
    assert kwargs['profiles_sample_rate'] == 0.0


def test_before_send_scrubs_sensitive_fields_recursively():
    event = {
        'request': {
            'headers': {'Authorization': 'Bearer abc', 'X-Request-Id': 'req-1'},
            'cookies': {'session': 'token123'},
            'data': {'nested': {'api_key': 'secretvalue'}, 'ok': 'value'},
            'query_string': 'access_token=abc&foo=bar',
        },
        'extra': {'stripe_secret': 'sk_live_abc', 'safe': 'keep'},
        'contexts': {'db': {'database_url': 'postgres://u:p@h/db'}},
        'exception': {'values': [{'value': 'password=abcd'}]},
    }

    cleaned = sentry_setup.before_send(event, {})
    assert cleaned['request']['headers']['Authorization'] == '[Filtered]'
    assert cleaned['request']['headers']['X-Request-Id'] == 'req-1'
    assert cleaned['request']['cookies']['session'] == '[Filtered]'
    assert cleaned['request']['data']['nested']['api_key'] == '[Filtered]'
    assert cleaned['request']['data']['ok'] == 'value'
    assert cleaned['request']['query_string'] == '[Filtered]'
    assert cleaned['extra']['stripe_secret'] == '[Filtered]'
    assert cleaned['extra']['safe'] == 'keep'
    assert cleaned['contexts']['db']['database_url'] == '[Filtered]'
    assert cleaned['exception']['values'][0]['value'] == '[Filtered]'


def test_before_send_scrubs_message_logentry_breadcrumbs_tags_and_user():
    event = {
        'message': 'password=abcd',
        'logentry': {
            'message': 'api_key=xyz',
            'formatted': 'Authorization: Basic dGVzdDp0b2tlbg==',
            'params': ['ok', {'refresh_token': 'rt-123'}],
        },
        'breadcrumbs': {
            'values': [
                {'message': 'Bearer abc.def.ghi', 'category': 'auth', 'data': {'Authorization': 'Bearer topsecret'}},
                {'message': 'safe message', 'category': 'ui', 'data': {'status': 'ok'}},
            ]
        },
        'tags': {'api_key': 'abc', 'safe_tag': 'keep'},
        'user': {'id': 'u1', 'email': 'x@example.com', 'ip_address': '127.0.0.1', 'token': 'abc'},
        'extra': {'safe': 'keep'},
    }

    cleaned = sentry_setup.before_send(event, {})
    assert cleaned['message'] == '[Filtered]'
    assert cleaned['logentry']['message'] == '[Filtered]'
    assert cleaned['logentry']['formatted'] == '[Filtered]'
    assert cleaned['logentry']['params'][0] == 'ok'
    assert cleaned['logentry']['params'][1]['refresh_token'] == '[Filtered]'
    assert cleaned['breadcrumbs']['values'][0]['message'] == '[Filtered]'
    assert cleaned['breadcrumbs']['values'][0]['data']['Authorization'] == '[Filtered]'
    assert cleaned['breadcrumbs']['values'][1]['message'] == 'safe message'
    assert cleaned['tags']['api_key'] == '[Filtered]'
    assert cleaned['tags']['safe_tag'] == 'keep'
    assert cleaned['user']['token'] == '[Filtered]'
    assert cleaned['user']['email'] == 'x@example.com'
    assert cleaned['extra']['safe'] == 'keep'


def test_init_failure_does_not_crash(monkeypatch):
    monkeypatch.setenv('SENTRY_DSN', 'https://example@sentry.io/1')
    monkeypatch.setitem(sys.modules, 'sentry_sdk', types.SimpleNamespace(init=lambda **_: (_ for _ in ()).throw(RuntimeError('boom'))))
    monkeypatch.setitem(sys.modules, 'sentry_sdk.integrations.flask', types.SimpleNamespace(FlaskIntegration=object))
    monkeypatch.setitem(sys.modules, 'sentry_sdk.integrations.logging', types.SimpleNamespace(LoggingIntegration=object))

    assert sentry_setup.init_sentry() is False


def test_init_failure_logs_generic_scrubbed_message_without_exc_info(monkeypatch):
    monkeypatch.setenv('SENTRY_DSN', 'https://public:private@sentry.io/42')
    monkeypatch.setitem(
        sys.modules,
        'sentry_sdk',
        types.SimpleNamespace(init=lambda **_: (_ for _ in ()).throw(RuntimeError('bad dsn https://public:private@sentry.io/42'))),
    )
    monkeypatch.setitem(sys.modules, 'sentry_sdk.integrations.flask', types.SimpleNamespace(FlaskIntegration=object))
    monkeypatch.setitem(sys.modules, 'sentry_sdk.integrations.logging', types.SimpleNamespace(LoggingIntegration=object))

    with patch.object(sentry_setup.logger, 'warning') as warning_mock:
        assert sentry_setup.init_sentry() is False
        warning_mock.assert_called_once()
        call_args = warning_mock.call_args
        assert 'exc_info' not in call_args.kwargs
        assert 'Sentry initialization failed; continuing without Sentry.' in call_args.args[0]
        assert 'https://public:private@sentry.io/42' not in call_args.args[1]
