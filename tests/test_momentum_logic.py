import os
for k in ["SECRET_KEY","TOKEN_ENCRYPTION_KEY","ALPACA_CLIENT_ID","ALPACA_CLIENT_SECRET","ALPACA_REDIRECT_URI","FINNHUB_API_KEY","GEMINI_API_KEY"]:
    os.environ.setdefault(k, "test")

import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parents[1]))
from datetime import datetime
from types import SimpleNamespace

import decision
import scanner
from asset_classifier import classify_asset


def test_cnsp_included_when_penny_allowed(monkeypatch):
    monkeypatch.setattr(scanner, 'get_alpaca_movers', lambda limit: ['CNSP'])
    monkeypatch.setattr(scanner, 'get_premarket_leaders', lambda limit: [])
    monkeypatch.setattr(scanner, 'get_unusual_relvol', lambda limit: [])
    monkeypatch.setattr(scanner, 'get_snapshots', lambda symbols, feed='iex': {'CNSP': {'minuteBar': {'c': 2.2}, 'prevDailyBar': {'c': 0.60}, 'dailyBar': {'v': 3_000_000}}})
    monkeypatch.setattr(scanner, 'get_latest_quotes', lambda symbols, feed='iex': {'CNSP': {'ap': 2.2}})
    monkeypatch.setattr(scanner, 'MOMENTUM_ALLOW_PENNY_STOCKS', True)
    user = SimpleNamespace(allow_penny_stocks=True, exclude_penny_stocks=True)
    valid, rejected = scanner.get_momentum_breakout_universe(user=user)
    assert 'CNSP' in valid
    assert rejected == []


def test_cnsp_rejected_when_penny_disallowed(monkeypatch):
    monkeypatch.setattr(scanner, 'get_alpaca_movers', lambda limit: ['CNSP'])
    monkeypatch.setattr(scanner, 'get_premarket_leaders', lambda limit: [])
    monkeypatch.setattr(scanner, 'get_unusual_relvol', lambda limit: [])
    monkeypatch.setattr(scanner, 'get_snapshots', lambda symbols, feed='iex': {'CNSP': {'minuteBar': {'c': 2.2}, 'prevDailyBar': {'c': 0.60}, 'dailyBar': {'v': 3_000_000}}})
    monkeypatch.setattr(scanner, 'get_latest_quotes', lambda symbols, feed='iex': {'CNSP': {'ap': 2.2}})
    monkeypatch.setattr(scanner, 'MOMENTUM_ALLOW_PENNY_STOCKS', False)
    user = SimpleNamespace(allow_penny_stocks=True, exclude_penny_stocks=False)
    valid, rejected = scanner.get_momentum_breakout_universe(user=user)
    assert 'CNSP' not in valid
    assert rejected[0]['rejection_reason'] == 'penny_stock_excluded'


def test_too_extended_returns_watch_for_pullback():
    out = decision.momentum_trade_decision({}, datetime.now(), 0.0, {
        'too_extended': True,
        'pullback_reclaim': False,
        'day_change_pct': 266,
        'rvol': 8,
        'above_vwap': True,
        'spread_ok': True,
    })
    assert out == 'WATCH FOR PULLBACK'


def test_controlled_entry_can_return_buy_now():
    out = decision.momentum_trade_decision({}, datetime.now(), 0.0, {
        'day_change_pct': 266,
        'rvol': 8,
        'above_vwap': True,
        'spread_ok': True,
        'too_extended': False,
        'pullback_reclaim': True,
    })
    assert out == 'BUY NOW'


def test_options_and_leveraged_etf_blocked_by_default():
    opt = classify_asset('AAPL240621C00180000', {'class': 'us_equity', 'name': 'Option Contract'}, {}, platform_flags={'options': False, 'etf': True, 'biotech': True, 'crypto_etf': True, 'leveraged_etf': False, 'inverse_etf': False}, user_flags={'options': True, 'etf': True, 'biotech': True, 'crypto_etf': True, 'leveraged_etf': True, 'inverse_etf': True})
    lev = classify_asset('TQQQ', {'class': 'us_equity', 'name': 'ProShares UltraPro QQQ ETF', 'exchange': 'NASDAQ', 'tradable': True}, {}, platform_flags={'options': False, 'etf': True, 'biotech': True, 'crypto_etf': True, 'leveraged_etf': False, 'inverse_etf': False}, user_flags={'options': False, 'etf': True, 'biotech': True, 'crypto_etf': True, 'leveraged_etf': True, 'inverse_etf': True})
    assert opt['rejection_reason'] == 'options_not_supported_yet'
    assert opt['rejection_reasons'] == ['options_not_supported_yet']
    assert lev['tradable_by_xeanvi'] is False


def test_common_stock_has_empty_rejection_reasons():
    stk = classify_asset(
        'AAPL',
        {'class': 'us_equity', 'name': 'Apple Inc.', 'tradable': True, 'exchange': 'NASDAQ'},
        {'name': 'Apple Inc.'},
        platform_flags={'options': False, 'etf': True, 'biotech': True, 'crypto_etf': True, 'leveraged_etf': False, 'inverse_etf': False},
        user_flags={'options': False, 'etf': True, 'biotech': True, 'crypto_etf': True, 'leveraged_etf': False, 'inverse_etf': False},
    )
    assert stk['asset_type'] == 'COMMON_STOCK'
    assert stk['rejection_reason'] is None
    assert stk['rejection_reasons'] == []
