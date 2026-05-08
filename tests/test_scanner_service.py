from types import SimpleNamespace

import scanner_service


def test_dispatch_blocked_does_not_call_celery(monkeypatch):
    called = {'v': False}

    class FakeTask:
        @staticmethod
        def delay(*args, **kwargs):
            called['v'] = True

    monkeypatch.setattr(scanner_service, 'evaluate_execution_readiness', lambda *a, **k: {'execution_ready': False, 'blocked_reasons': [{'code': 'X'}], 'trading_mode': 'paper', 'symbol': 'AAPL', 'decision': 'BUY NOW', 'qty': 1})
    monkeypatch.setitem(__import__('sys').modules, 'tasks', SimpleNamespace(execute_user_trade_task=FakeTask))
    scanner_service._dispatch_execution_if_allowed(SimpleNamespace(id=1), {'scan_id': 1, 'best_pick': {}})
    assert called['v'] is False


def test_dispatch_ready_calls_celery(monkeypatch):
    called = {'v': False}

    class FakeTask:
        @staticmethod
        def delay(*args, **kwargs):
            called['v'] = True

    monkeypatch.setattr(scanner_service, 'evaluate_execution_readiness', lambda *a, **k: {'execution_ready': True, 'blocked_reasons': [], 'trading_mode': 'paper', 'symbol': 'AAPL', 'decision': 'BUY NOW', 'qty': 2})
    monkeypatch.setitem(__import__('sys').modules, 'tasks', SimpleNamespace(execute_user_trade_task=FakeTask))
    scanner_service._dispatch_execution_if_allowed(SimpleNamespace(id=1), {'scan_id': 1, 'best_pick': {'entry_price': 10, 'stop_price': 9, 'target_1': 11, 'target_2': 12}})
    assert called['v'] is True


def test_execution_readiness_endpoint_no_secrets(monkeypatch):
    import app as app_module

    user = SimpleNamespace(
        id=99,
        subscription_status='pro',
        trading_mode='paper',
        alpaca_access_token='secret',
        alpaca_paper_access_token='paper-secret',
        alpaca_live_access_token=None,
        onboarding_completed=True,
        paper_bankroll_set=True,
        playbook_reviewed=True,
        transparency_reviewed=True,
        broker_connection_started=True,
    )
    monkeypatch.setattr(app_module, 'current_user', user)
    monkeypatch.setattr(app_module, 'get_recent_scans', lambda: [])
    monkeypatch.setattr(app_module.redis_client, 'get', lambda *a, **k: None)
    with app_module.app.test_request_context('/api/execution-readiness', method='GET'):
        resp = app_module.api_execution_readiness.__wrapped__()
        payload = resp.get_json()
        data = payload['data']
        assert 'token' not in str(data).lower()
        codes = {r['code'] for r in data['latest_scan_evaluation']['blocked_reasons']}
        assert 'NO_RECENT_SCAN' in codes
