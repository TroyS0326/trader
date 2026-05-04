import os
for k in ["SECRET_KEY","TOKEN_ENCRYPTION_KEY","ALPACA_CLIENT_ID","ALPACA_CLIENT_SECRET","ALPACA_REDIRECT_URI","FINNHUB_API_KEY","GEMINI_API_KEY"]:
    os.environ.setdefault(k, "test")

from types import SimpleNamespace
import scanner
from watchlist import WatchlistManager


def test_momentum_rejections_include_asset_schema(monkeypatch):
    monkeypatch.setattr(scanner, 'get_alpaca_movers', lambda limit: ['AAPL240621C00180000'])
    monkeypatch.setattr(scanner, 'get_premarket_leaders', lambda limit: [])
    monkeypatch.setattr(scanner, 'get_unusual_relvol', lambda limit: [])
    monkeypatch.setattr(scanner, 'get_snapshots', lambda symbols, feed='iex': {'AAPL240621C00180000': {'minuteBar': {'c': 3.0}, 'prevDailyBar': {'c': 2.0}, 'dailyBar': {'v': 1000}}})
    monkeypatch.setattr(scanner, 'get_latest_quotes', lambda symbols, feed='iex': {'AAPL240621C00180000': {'ap': 3.0}})
    monkeypatch.setattr(scanner, 'get_alpaca_asset', lambda s: {'class': 'us_equity', 'name': 'Option Contract'})
    monkeypatch.setattr(scanner, 'get_company_profile', lambda s: {'name': 'Option Contract'})

    _valid, rejected = scanner.get_momentum_breakout_universe(user=SimpleNamespace())
    row = rejected[0]
    for key in ['asset_type', 'asset_type_reason', 'platform_allowed', 'user_allowed', 'tradable_by_xeanvi', 'rejection_reasons']:
        assert key in row
    assert row['rejection_reasons'] == ['options_not_supported_yet']


def test_watchlist_reason_labels(monkeypatch):
    mgr = WatchlistManager()
    mgr.set_items([{'symbol': 'ABC', 'stop_price': 10.0, 'entry_price': 11.0, 'buy_upper': 11.2, 'vwap': 10.5, 'buy_window_open': True}])
    monkeypatch.setattr('watchlist.get_latest_quotes', lambda symbols, feed='iex': {'ABC': {'ap': 9.5, 't': 'now'}})
    items = mgr.refresh(user=SimpleNamespace(alpaca_data_feed='iex'))
    assert items[0]['live_signal'] == 'SETUP BROKEN: BELOW STOP'
    assert 'invalidated' in items[0]['live_signal_reason'].lower()
