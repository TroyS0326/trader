import os
from types import SimpleNamespace

from execution_diagnostics import evaluate_execution_readiness


def _user(**kwargs):
    base = dict(
        id=1,
        subscription_status='pro',
        alpaca_access_token='token',
        trading_mode='paper',
        onboarding_completed=True,
        paper_bankroll_set=True,
        playbook_reviewed=True,
        transparency_reviewed=True,
        broker_connection_started=True,
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def _payload(**best):
    return {'best_pick': {'decision': 'BUY NOW', 'symbol': 'AAPL', 'qty': 2, 'entry_price': 10, 'stop_price': 9, 'target_1': 11, 'target_2': 12, **best}}


def _codes(diag):
    return {x['code'] for x in diag['blocked_reasons']}


def test_exec_disabled(monkeypatch):
    monkeypatch.setenv('CENTRAL_SCANNER_EXECUTION_ENABLED', '0')
    d = evaluate_execution_readiness(_user(), _payload())
    assert 'EXECUTION_DISABLED' in _codes(d)


def test_all_other_gates(monkeypatch):
    monkeypatch.setenv('CENTRAL_SCANNER_EXECUTION_ENABLED', '1')
    monkeypatch.setenv('CENTRAL_SCANNER_LIVE_EXECUTION_ENABLED', '0')

    assert 'NON_PRO_USER' in _codes(evaluate_execution_readiness(_user(subscription_status='free'), _payload()))
    assert 'NO_ACTIVE_ALPACA_TOKEN' in _codes(evaluate_execution_readiness(_user(alpaca_access_token=None), _payload()))
    assert 'ONBOARDING_INCOMPLETE' in _codes(evaluate_execution_readiness(_user(onboarding_completed=False), _payload()))
    assert 'DECISION_NOT_ELIGIBLE' in _codes(evaluate_execution_readiness(_user(), _payload(decision='WAIT')))
    assert 'MISSING_ORDER_FIELDS' in _codes(evaluate_execution_readiness(_user(), _payload(entry_price=None)))
    assert 'QTY_BELOW_1' in _codes(evaluate_execution_readiness(_user(), _payload(qty=0)))
    assert 'LIVE_EXECUTION_DISABLED' in _codes(evaluate_execution_readiness(_user(trading_mode='live'), _payload()))


def test_ready(monkeypatch):
    monkeypatch.setenv('CENTRAL_SCANNER_EXECUTION_ENABLED', '1')
    monkeypatch.setenv('CENTRAL_SCANNER_LIVE_EXECUTION_ENABLED', '1')
    d = evaluate_execution_readiness(_user(), _payload())
    assert d['execution_ready'] is True
