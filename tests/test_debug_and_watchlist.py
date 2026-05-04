import os
for k in ["SECRET_KEY","TOKEN_ENCRYPTION_KEY","ALPACA_CLIENT_ID","ALPACA_CLIENT_SECRET","ALPACA_REDIRECT_URI","FINNHUB_API_KEY","GEMINI_API_KEY"]:
    os.environ.setdefault(k, "test")

from types import SimpleNamespace
from pathlib import Path
import sys
sys.path.append(str(Path(__file__).resolve().parents[1]))
import scanner
from watchlist import WatchlistManager
import app as app_module


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


def test_dashboard_watchlist_empty_state_colspan():
    html = Path('templates/dashboard.html').read_text()
    assert 'renderWatchlist(items)' in html
    assert 'colspan="6"' in html
    assert 'colspan="4"' not in html


def test_debug_symbol_degraded_analysis_has_clear_rejection(monkeypatch):
    monkeypatch.setattr(app_module, 'current_user', SimpleNamespace(
        alpaca_data_feed='iex',
        allow_biotech=True,
        allow_etf_trading=True,
        allow_leveraged_etfs=False,
        allow_inverse_etfs=False,
        allow_crypto_etfs=True,
        allow_options_trading=False,
    ))
    monkeypatch.setattr(app_module, 'resolve_data_feed', lambda user: 'iex')
    monkeypatch.setattr(app_module, 'get_snapshots', lambda symbols, feed='iex': {symbols[0]: {'minuteBar': {'c': 0}, 'prevDailyBar': {'c': 0}}})
    monkeypatch.setattr(app_module, 'get_latest_quotes', lambda symbols, feed='iex': {symbols[0]: {}})
    monkeypatch.setattr(app_module, 'get_company_profile', lambda symbol: {})
    monkeypatch.setattr(app_module, 'get_alpaca_asset', lambda symbol: {'class': 'us_equity', 'name': 'Acme Inc.', 'tradable': True, 'exchange': 'NYSE'})
    monkeypatch.setattr(app_module, 'get_bars', lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('bars unavailable')))

    with app_module.app.test_request_context('/api/debug-symbol/ACME'):
        resp = app_module.api_debug_symbol.__wrapped__('ACME')
        data = resp.get_json()

    assert data['rejected'] is True
    assert 'diagnostic_data_unavailable' in data['rejection_reasons']
    assert 'deep_analysis_failed' in data['rejection_reasons']
    assert 'incomplete' in data['final_explanation'].lower()


def test_debug_symbol_skip_includes_setup_rejection_reasons(monkeypatch):
    monkeypatch.setattr(app_module, 'current_user', SimpleNamespace(
        alpaca_data_feed='iex',
        allow_biotech=True,
        allow_etf_trading=True,
        allow_leveraged_etfs=False,
        allow_inverse_etfs=False,
        allow_crypto_etfs=True,
        allow_options_trading=False,
    ))
    monkeypatch.setattr(app_module, 'resolve_data_feed', lambda user: 'iex')
    monkeypatch.setattr(app_module, 'get_snapshots', lambda symbols, feed='iex': {symbols[0]: {'minuteBar': {'c': 8.73}, 'prevDailyBar': {'c': 2.2}}})
    monkeypatch.setattr(app_module, 'get_latest_quotes', lambda symbols, feed='iex': {symbols[0]: {'ap': 8.73}})
    monkeypatch.setattr(app_module, 'get_company_profile', lambda symbol: {})
    monkeypatch.setattr(app_module, 'get_alpaca_asset', lambda symbol: {'class': 'us_equity', 'name': 'CNS Pharmaceuticals', 'tradable': True, 'exchange': 'NASDAQ'})
    monkeypatch.setattr(app_module, 'get_bars', lambda *args, **kwargs: {'CNSP': [], 'SPY': []})
    monkeypatch.setattr(app_module, 'analyze_symbol', lambda *args, **kwargs: {
        'setup_grade': 'NO TRADE', 'decision': 'SKIP', 'buy_lower': 10.2, 'buy_upper': 10.6,
        'entry_price': 10.45, 'stop_price': 9.07, 'target_1': 11.2, 'target_2': 12.0, 'qty': 0,
        'details': {'spread_pct': 0.1008, 'vwap_hold_reclaim': {'vwap': 8.98, 'reclaimed_vwap': False}, 'quick_notes': []},
        'scores': {}, 'score_total': 0
    })

    with app_module.app.test_request_context('/api/debug-symbol/CNSP'):
        resp = app_module.api_debug_symbol.__wrapped__('CNSP')
        data = resp.get_json()

    assert data['rejected'] is True
    for reason in ['below_stop', 'below_vwap', 'spread_too_wide', 'price_below_entry', 'no_controlled_entry', 'setup_grade_no_trade', 'decision_skip', 'qty_zero']:
        assert reason in data['rejection_reasons']
    assert 'should not chase it' in data['final_explanation']
