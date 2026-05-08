from pathlib import Path
import sys
from types import SimpleNamespace
import json

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.modules.setdefault('redis', SimpleNamespace(Redis=SimpleNamespace(from_url=lambda *a, **k: SimpleNamespace(get=lambda *x, **y: None))))
sys.modules.setdefault('requests', SimpleNamespace())
sys.modules.setdefault('stripe', SimpleNamespace(api_key=''))
import types

aps_bg = types.SimpleNamespace(BackgroundScheduler=object)
aps_cron = types.SimpleNamespace(CronTrigger=object)
sys.modules.setdefault('apscheduler', types.SimpleNamespace())
sys.modules.setdefault('apscheduler.schedulers', types.SimpleNamespace())
sys.modules.setdefault('apscheduler.schedulers.background', aps_bg)
sys.modules.setdefault('apscheduler.triggers', types.SimpleNamespace())
sys.modules.setdefault('apscheduler.triggers.cron', aps_cron)

import scanner_service


def test_dispatch_blocked_does_not_call_celery(monkeypatch):
    called = {'v': False}

    class FakeTask:
        @staticmethod
        def delay(*args, **kwargs):
            called['v'] = True

    monkeypatch.setattr(scanner_service, 'evaluate_execution_readiness', lambda *a, **k: {'execution_ready': False, 'blocked_reasons': [{'code': 'X'}], 'trading_mode': 'paper', 'symbol': 'AAPL', 'decision': 'BUY NOW', 'qty': 1, 'order_fields': None})
    monkeypatch.setitem(__import__('sys').modules, 'tasks', SimpleNamespace(execute_user_trade_task=FakeTask))
    scanner_service._dispatch_execution_if_allowed(SimpleNamespace(id=1), {'scan_id': 1, 'best_pick': {}})
    assert called['v'] is False


def test_dispatch_ready_calls_celery_with_diag_order_fields(monkeypatch):
    called = {'args': None}

    class FakeTask:
        @staticmethod
        def delay(*args, **kwargs):
            called['args'] = args

    diag = {
        'execution_ready': True,
        'blocked_reasons': [],
        'trading_mode': 'paper',
        'symbol': 'AAPL',
        'decision': 'BUY NOW',
        'qty': 2,
        'order_fields': {'symbol': 'MSFT', 'qty': 3, 'entry_price': 100.0, 'stop_price': 99.0, 'target_1': 101.0, 'target_2': 102.0},
    }
    monkeypatch.setattr(scanner_service, 'evaluate_execution_readiness', lambda *a, **k: diag)
    monkeypatch.setitem(__import__('sys').modules, 'tasks', SimpleNamespace(execute_user_trade_task=FakeTask))
    scanner_service._dispatch_execution_if_allowed(SimpleNamespace(id=1), {'scan_id': 1, 'best_pick': {'entry_price': 10, 'stop_price': 9, 'target_1': 11, 'target_2': 12}})
    assert called['args'] == (1, 1, 'MSFT', 3, 100.0, 99.0, 101.0, 102.0)


def test_dispatch_paper_ready_even_if_live_not_connected(monkeypatch):
    called = {'v': False}

    class FakeTask:
        @staticmethod
        def delay(*args, **kwargs):
            called['v'] = True

    diag = {
        'execution_ready': True,
        'active_mode': 'paper',
        'paper_execution_ready': True,
        'live_execution_ready': False,
        'active_mode_blocked_reasons': [],
        'paper_blocked_reasons': [],
        'live_blocked_reasons': [{'code': 'LIVE_NOT_CONNECTED'}],
        'trading_mode': 'paper',
        'symbol': 'AAPL',
        'decision': 'BUY NOW',
        'qty': 1,
        'order_fields': {'symbol': 'AAPL', 'qty': 1, 'entry_price': 10.0, 'stop_price': 9.0, 'target_1': 11.0, 'target_2': 12.0},
    }
    monkeypatch.setattr(scanner_service, 'evaluate_execution_readiness', lambda *a, **k: diag)
    monkeypatch.setitem(__import__('sys').modules, 'tasks', SimpleNamespace(execute_user_trade_task=FakeTask))
    scanner_service._dispatch_execution_if_allowed(SimpleNamespace(id=1), {'scan_id': 1, 'best_pick': {}})
    assert called['v'] is True


def test_dispatch_live_blocked_does_not_call_celery(monkeypatch):
    called = {'v': False}

    class FakeTask:
        @staticmethod
        def delay(*args, **kwargs):
            called['v'] = True

    diag = {
        'execution_ready': False,
        'active_mode': 'live',
        'paper_execution_ready': True,
        'live_execution_ready': False,
        'active_mode_blocked_reasons': [{'code': 'LIVE_NOT_CONNECTED'}],
        'paper_blocked_reasons': [],
        'live_blocked_reasons': [{'code': 'LIVE_NOT_CONNECTED'}],
        'trading_mode': 'live',
        'symbol': 'AAPL',
        'decision': 'BUY NOW',
        'qty': 1,
        'order_fields': None,
    }
    monkeypatch.setattr(scanner_service, 'evaluate_execution_readiness', lambda *a, **k: diag)
    monkeypatch.setitem(__import__('sys').modules, 'tasks', SimpleNamespace(execute_user_trade_task=FakeTask))
    scanner_service._dispatch_execution_if_allowed(SimpleNamespace(id=1), {'scan_id': 1, 'best_pick': {}})
    assert called['v'] is False


def test_execution_readiness_endpoint_no_secrets_and_no_recent_scan(monkeypatch):
    import app as app_module

    user = SimpleNamespace(
        id=99,
        subscription_status='pro',
        trading_mode='paper',
        alpaca_access_token='secret',
        alpaca_paper_access_token='paper-secret',
        alpaca_live_access_token='live-secret',
        onboarding_completed=True,
        paper_bankroll_set=True,
        paper_bankroll=1000,
    )
    monkeypatch.setattr(app_module, 'current_user', user)
    monkeypatch.setattr(app_module, 'get_recent_scans', lambda: [])
    monkeypatch.setattr(app_module.redis_client, 'get', lambda *a, **k: None)
    with app_module.app.test_request_context('/api/execution-readiness', method='GET'):
        resp = app_module.api_execution_readiness.__wrapped__()
        payload = resp.get_json()
        data = payload['data']
        as_json = json.dumps(data).lower()
        assert 'secret' not in as_json
        assert 'paper-secret' not in as_json
        assert 'live-secret' not in as_json
        latest_codes = {r['code'] for r in data['latest_scan_evaluation']['blocked_reasons']}
        active_codes = {r['code'] for r in data['active_mode_blocked_reasons']}
        paper_codes = {r['code'] for r in data['paper_blocked_reasons']}
        live_codes = {r['code'] for r in data['live_blocked_reasons']}
        assert 'NO_RECENT_SCAN' in latest_codes
        assert 'NO_RECENT_SCAN' in active_codes
        assert 'NO_RECENT_SCAN' in paper_codes
        assert 'NO_RECENT_SCAN' in live_codes
        assert data['latest_scan_evaluation']['execution_ready'] is False
        assert data['execution_ready'] is False
        assert data['paper_setup_ready'] == data['paper_execution_ready']
        assert data['live_onboarding_ready'] == data['live_execution_ready']
        assert 'paper_execution_ready' in data
        assert 'live_execution_ready' in data
        assert 'paper_blocked_reasons' in data
        assert 'live_blocked_reasons' in data
        assert 'active_mode_blocked_reasons' in data
