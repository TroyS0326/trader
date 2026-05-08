from pathlib import Path
import sys
from types import SimpleNamespace

sys.modules.setdefault('requests', SimpleNamespace())

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from execution_diagnostics import evaluate_execution_readiness


def _user(**kwargs):
    base = dict(
        id=1,
        subscription_status='pro',
        alpaca_access_token='token',
        alpaca_paper_access_token='paper-token',
        alpaca_live_access_token='live-token',
        alpaca_paper_account_id='paper-acct',
        alpaca_live_account_id='live-acct',
        trading_mode='paper',
        onboarding_completed=True,
        paper_bankroll_set=True,
        paper_bankroll=1000,
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
    monkeypatch.setattr('execution_diagnostics.buy_window_open', lambda: True)
    d = evaluate_execution_readiness(_user(), _payload())
    assert 'EXECUTION_DISABLED' in _codes(d)


def test_all_other_gates(monkeypatch):
    monkeypatch.setenv('CENTRAL_SCANNER_EXECUTION_ENABLED', '1')
    monkeypatch.setenv('CENTRAL_SCANNER_LIVE_EXECUTION_ENABLED', '0')
    monkeypatch.setenv('CENTRAL_SCANNER_REQUIRE_COMPLETED_ONBOARDING', '1')
    monkeypatch.setattr('execution_diagnostics.buy_window_open', lambda: True)

    assert 'NON_PRO_USER' in _codes(evaluate_execution_readiness(_user(subscription_status='free'), _payload()))
    assert 'NO_ACTIVE_ALPACA_TOKEN' in _codes(evaluate_execution_readiness(_user(alpaca_access_token=None, alpaca_paper_access_token=None), _payload()))
    assert 'PAPER_NOT_CONNECTED' in _codes(evaluate_execution_readiness(_user(alpaca_paper_access_token=None, alpaca_paper_account_id=None), _payload()))
    assert 'LIVE_NOT_CONNECTED' in _codes(evaluate_execution_readiness(_user(trading_mode='live', alpaca_live_access_token=None, alpaca_live_account_id=None), _payload()))
    assert 'PAPER_BANKROLL_NOT_SET' in _codes(evaluate_execution_readiness(_user(paper_bankroll_set=False), _payload()))
    assert 'PAPER_BANKROLL_ZERO' in _codes(evaluate_execution_readiness(_user(paper_bankroll=0), _payload()))
    assert 'LIVE_ONBOARDING_NOT_COMPLETED' in _codes(evaluate_execution_readiness(_user(trading_mode='live', onboarding_completed=False), _payload()))
    assert 'DECISION_NOT_ELIGIBLE' in _codes(evaluate_execution_readiness(_user(), _payload(decision='WAIT')))
    assert 'MISSING_ORDER_FIELDS' in _codes(evaluate_execution_readiness(_user(), _payload(entry_price=None)))
    assert 'QTY_BELOW_1' in _codes(evaluate_execution_readiness(_user(), _payload(qty=0)))
    assert 'LIVE_EXECUTION_DISABLED' in _codes(evaluate_execution_readiness(_user(trading_mode='live'), _payload()))


def test_buy_window_closed(monkeypatch):
    monkeypatch.setenv('CENTRAL_SCANNER_EXECUTION_ENABLED', '1')
    monkeypatch.setattr('execution_diagnostics.buy_window_open', lambda: False)
    d = evaluate_execution_readiness(_user(), _payload())
    assert 'BUY_WINDOW_CLOSED' in _codes(d)


def test_ready(monkeypatch):
    monkeypatch.setenv('CENTRAL_SCANNER_EXECUTION_ENABLED', '1')
    monkeypatch.setenv('CENTRAL_SCANNER_LIVE_EXECUTION_ENABLED', '1')
    monkeypatch.setattr('execution_diagnostics.buy_window_open', lambda: True)
    d = evaluate_execution_readiness(_user(), _payload())
    assert d['execution_ready'] is True
    assert d['order_fields'] == {
        'symbol': 'AAPL', 'qty': 2, 'entry_price': 10.0, 'stop_price': 9.0, 'target_1': 11.0, 'target_2': 12.0,
    }


def test_paper_ready_without_live_connection_or_onboarding(monkeypatch):
    monkeypatch.setenv('CENTRAL_SCANNER_EXECUTION_ENABLED', '1')
    monkeypatch.setenv('CENTRAL_SCANNER_REQUIRE_COMPLETED_ONBOARDING', '1')
    monkeypatch.setattr('execution_diagnostics.buy_window_open', lambda: True)
    d = evaluate_execution_readiness(_user(alpaca_live_access_token=None, alpaca_live_account_id=None, onboarding_completed=False, trading_mode='paper'), _payload())
    assert d['paper_execution_ready'] is True
    assert d['execution_ready'] is True


def test_paper_blocked_when_token_missing(monkeypatch):
    monkeypatch.setenv('CENTRAL_SCANNER_EXECUTION_ENABLED', '1')
    monkeypatch.setattr('execution_diagnostics.buy_window_open', lambda: True)
    d = evaluate_execution_readiness(_user(alpaca_access_token=None, alpaca_paper_access_token=None), _payload())
    assert 'NO_ACTIVE_ALPACA_TOKEN' in _codes(d)


def test_live_ready_only_with_live_flags(monkeypatch):
    monkeypatch.setenv('CENTRAL_SCANNER_EXECUTION_ENABLED', '1')
    monkeypatch.setenv('CENTRAL_SCANNER_LIVE_EXECUTION_ENABLED', '1')
    monkeypatch.setenv('CENTRAL_SCANNER_REQUIRE_COMPLETED_ONBOARDING', '1')
    monkeypatch.setattr('execution_diagnostics.buy_window_open', lambda: True)
    d = evaluate_execution_readiness(_user(trading_mode='live', onboarding_completed=True), _payload())
    assert d['live_execution_ready'] is True
    assert d['execution_ready'] is True


def test_paper_and_live_readiness_split(monkeypatch):
    monkeypatch.setenv('CENTRAL_SCANNER_EXECUTION_ENABLED', '1')
    monkeypatch.setenv('CENTRAL_SCANNER_LIVE_EXECUTION_ENABLED', '0')
    monkeypatch.setenv('CENTRAL_SCANNER_REQUIRE_COMPLETED_ONBOARDING', '1')
    monkeypatch.setattr('execution_diagnostics.buy_window_open', lambda: True)
    d = evaluate_execution_readiness(_user(trading_mode='paper', onboarding_completed=False, alpaca_live_access_token=None, alpaca_live_account_id=None), _payload())
    assert d['paper_execution_ready'] is True
    assert d['live_execution_ready'] is False
