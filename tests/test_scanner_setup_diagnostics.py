from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.modules.setdefault('requests', SimpleNamespace())
sys.modules.setdefault('dotenv', SimpleNamespace(load_dotenv=lambda *a, **k: None))

import scanner
import pytest


def test_build_setup_grade_diagnostics_reports_no_trade_failures():
    diagnostics = scanner.build_setup_grade_diagnostics(
        total=10,
        catalyst_score=1,
        liquidity_score=1,
        sector_score=0,
        confirm_score=1,
        vwap_score=1,
        pullback_score=1,
        premarket_gap_pct=0.5,
        premarket_notional=100_000,
    )
    assert diagnostics["setup_grade"] == "NO TRADE"
    assert "TOTAL_SCORE_BELOW_WATCH_THRESHOLD" in diagnostics["failed_watch_requirements"]
    assert "CATALYST_SCORE_BELOW_WATCH_THRESHOLD" in diagnostics["failed_watch_requirements"]
    assert "LIQUIDITY_SCORE_BELOW_A_THRESHOLD" in diagnostics["failed_a_requirements"]
    assert "CONFIRM_SCORE_BELOW_A_THRESHOLD" in diagnostics["failed_a_requirements"]
    assert "VWAP_SCORE_BELOW_A_THRESHOLD" in diagnostics["failed_a_requirements"]
    assert "PREMARKET_GAP_BELOW_A_THRESHOLD" in diagnostics["failed_a_requirements"]
    assert "PREMARKET_DOLLAR_VOLUME_BELOW_A_THRESHOLD" in diagnostics["failed_a_requirements"]


def test_execution_eligibility_reason_uses_allowlist_semantics():
    executable = scanner.build_setup_grade_diagnostics(
        total=scanner.A_SCORE,
        catalyst_score=4,
        liquidity_score=3,
        sector_score=scanner.MIN_SECTOR_SYMPATHY_SCORE,
        confirm_score=3,
        vwap_score=3,
        pullback_score=2,
        premarket_gap_pct=scanner.MIN_PREMARKET_GAP_PCT,
        premarket_notional=scanner.MIN_PREMARKET_DOLLAR_VOL,
    )
    assert executable["setup_grade"] in {"A", "A+"}


def _mk_bar(ts, o=10,h=11,l=9,c=10,v=1000):
    return {'t': ts, 'o': o, 'h': h, 'l': l, 'c': c, 'v': v}


def test_opening_range_reason_no_opening_range_bars(monkeypatch):
    monkeypatch.setattr(scanner, 'buy_window_open', lambda: True)
    bars=[_mk_bar('2026-05-08T14:00:00+00:00'), _mk_bar('2026-05-08T14:50:00+00:00')]
    stats=scanner.get_opening_range_stats(bars)
    assert stats['opening_range_complete_reason']=='NO_OPENING_RANGE_BARS'


def test_opening_range_reason_latest_before_end(monkeypatch):
    monkeypatch.setattr(scanner, 'buy_window_open', lambda: True)
    bars=[_mk_bar('2026-05-08T13:30:00+00:00'), _mk_bar('2026-05-08T13:35:00+00:00')]
    stats=scanner.get_opening_range_stats(bars)
    assert stats['opening_range_complete_reason']=='LATEST_BAR_BEFORE_OR_END'


def test_opening_range_complete(monkeypatch):
    monkeypatch.setattr(scanner, 'buy_window_open', lambda: True)
    bars=[]
    for i in range(15):
        bars.append(_mk_bar(f'2026-05-08T13:{30+i:02d}:00+00:00'))
    stats=scanner.get_opening_range_stats(bars)
    assert stats['expected_opening_range_bar_count'] == 15
    assert stats['opening_range_complete'] is True


def test_opening_range_complete_with_minor_gap(monkeypatch):
    monkeypatch.setattr(scanner, 'buy_window_open', lambda: True)
    bars=[_mk_bar(f'2026-05-08T13:{30+i:02d}:00+00:00') for i in range(14)]
    bars.append(_mk_bar('2026-05-08T13:49:00+00:00'))
    stats=scanner.get_opening_range_stats(bars)
    assert stats['opening_range_complete'] is True
    assert stats['opening_range_complete_reason'] == 'COMPLETE_WITH_MINOR_BAR_GAP'


def test_opening_range_missing_too_many_bars(monkeypatch):
    monkeypatch.setattr(scanner, 'buy_window_open', lambda: True)
    bars=[_mk_bar(f'2026-05-08T13:{30+i:02d}:00+00:00') for i in range(10)]
    bars.append(_mk_bar('2026-05-08T13:49:00+00:00'))
    stats=scanner.get_opening_range_stats(bars)
    assert stats['opening_range_complete'] is False
    assert stats['opening_range_complete_reason'] == 'MISSING_TOO_MANY_OR_BARS'


def test_data_helpers_skip_empty_symbols_without_http(monkeypatch):
    calls = {'count': 0}
    monkeypatch.setattr(scanner, '_get_json', lambda *a, **k: calls.__setitem__('count', calls['count'] + 1) or {})
    assert scanner.get_snapshots([]) == {}
    assert scanner.get_latest_quotes([]) == {}
    assert scanner.get_bars([], '1Day', scanner.now_utc(), scanner.now_utc(), 10) == {}
    assert calls['count'] == 0


def test_get_alpaca_asset_uses_trading_base(monkeypatch):
    captured = {}
    def _fake_get_json(url, **kwargs):
        captured['url'] = url
        return {'symbol': 'AAPL', 'tradable': True}
    monkeypatch.setattr(scanner, '_get_json', _fake_get_json)
    payload = scanner.get_alpaca_asset('AAPL')
    assert payload['symbol'] == 'AAPL'
    assert captured['url'].startswith(f"{scanner.config.ALPACA_ASSETS_BASE}/v2/assets/")


def test_run_scan_raises_diagnostic_when_asset_filter_empties_candidates(monkeypatch):
    monkeypatch.setattr(scanner, 'update_dynamic_orb_state_from_market_data', lambda: None)
    monkeypatch.setattr(scanner, 'resolve_data_feed', lambda user=None: 'iex')
    monkeypatch.setattr(scanner, 'get_refined_universe', lambda user=None: ['AAPL'])
    monkeypatch.setattr(scanner, 'get_momentum_breakout_universe', lambda user=None: ([], []))
    monkeypatch.setattr(scanner, 'get_alpaca_movers', lambda limit: [])
    monkeypatch.setattr(scanner, 'get_premarket_leaders', lambda limit: [])
    monkeypatch.setattr(scanner, 'get_unusual_relvol', lambda limit: [])
    monkeypatch.setattr(scanner, 'apply_user_symbol_filters', lambda symbols, snapshots, quotes, user=None: symbols)
    monkeypatch.setattr(scanner, 'get_snapshots', lambda symbols, feed='iex': {'AAPL': {'minuteBar': {'c': 10}, 'prevDailyBar': {'c': 9}}})
    monkeypatch.setattr(scanner, 'get_latest_quotes', lambda symbols, feed='iex': {'AAPL': {'ap': 10}})
    monkeypatch.setattr(scanner, 'get_company_profile', lambda symbol: {})
    monkeypatch.setattr(scanner, 'get_alpaca_asset', lambda symbol: {'name': 'Bad Asset', 'tradable': False})
    with pytest.raises(scanner.ScanError, match='No symbols remained after asset quality filtering'):
        scanner.run_scan()


def test_run_scan_degrades_when_all_asset_metadata_lookups_fail(monkeypatch):
    monkeypatch.setattr(scanner, 'update_dynamic_orb_state_from_market_data', lambda: None)
    monkeypatch.setattr(scanner, 'resolve_data_feed', lambda user=None: 'iex')
    monkeypatch.setattr(scanner, 'get_refined_universe', lambda user=None: ['AAPL', 'MSFT'])
    monkeypatch.setattr(scanner, 'get_momentum_breakout_universe', lambda user=None: ([], []))
    monkeypatch.setattr(scanner, 'get_alpaca_movers', lambda limit: [])
    monkeypatch.setattr(scanner, 'get_premarket_leaders', lambda limit: [])
    monkeypatch.setattr(scanner, 'get_unusual_relvol', lambda limit: [])
    monkeypatch.setattr(scanner, 'apply_user_symbol_filters', lambda symbols, snapshots, quotes, user=None: symbols)
    monkeypatch.setattr(scanner, 'get_snapshots', lambda symbols, feed='iex': {s: {'minuteBar': {'c': 10}, 'prevDailyBar': {'c': 9}} for s in symbols})
    monkeypatch.setattr(scanner, 'get_latest_quotes', lambda symbols, feed='iex': {s: {'ap': 10} for s in symbols})
    monkeypatch.setattr(scanner, 'get_company_profile', lambda symbol: {})
    def _asset_diag(symbol, user=None):
        return {}, {'symbol': symbol, 'endpoint_used': 'x', 'auth_source': 'server_api_key', 'status_code': 401, 'ok': False, 'failure_reason': 'HTTP_401', 'response_text_short': 'unauthorized', 'used_fallback': False}
    monkeypatch.setattr(scanner, 'get_alpaca_asset_with_diagnostics', _asset_diag)
    monkeypatch.setattr(scanner, 'get_bars', lambda symbols, *a, **k: {s: [{'t':'2026-05-08T13:30:00+00:00','o':1,'h':2,'l':1,'c':1.5,'v':1000}] for s in symbols})
    monkeypatch.setattr(scanner, 'fill_missing_bars_individually', lambda *a, **k: {'individual_bar_retry_attempted_count':0,'individual_bar_retry_success_count':0,'individual_bar_retry_failed_symbols':[]})
    monkeypatch.setattr(scanner, 'analyze_symbol', lambda symbol, *a, **k: {'symbol': symbol, 'decision': 'SKIP', 'setup_grade': 'NO TRADE', 'scores': {'catalyst': 0}, 'score_total': 0, 'details': {'open_relative_strength': {'edge': 0}, 'liquidity': {'spread': 0}, 'skip_reason_codes': []}})
    monkeypatch.setattr(scanner, 'get_stock_chart_pack', lambda *a, **k: {})
    out = scanner.run_scan()
    diag = out['scan_diagnostics']
    assert diag['asset_metadata_degraded_mode'] is True
    assert diag['asset_metadata_global_failure'] is True
    assert diag['asset_metadata_degraded_allowed_count'] == 2
    assert set(diag['asset_metadata_degraded_allowed_symbols']) == {'AAPL', 'MSFT'}
    assert diag['asset_metadata_degraded_rejection_counts'] == {}
    assert 'ASSET_METADATA_DEGRADED_MODE' in diag['scanner_starvation_flags']


def test_run_scan_degraded_mode_still_blocks_warrant_like_symbols(monkeypatch):
    monkeypatch.setattr(scanner, 'update_dynamic_orb_state_from_market_data', lambda: None)
    monkeypatch.setattr(scanner, 'resolve_data_feed', lambda user=None: 'iex')
    monkeypatch.setattr(scanner, 'get_refined_universe', lambda user=None: ['ABCWS', 'AAPL'])
    monkeypatch.setattr(scanner, 'get_momentum_breakout_universe', lambda user=None: ([], []))
    monkeypatch.setattr(scanner, 'get_alpaca_movers', lambda limit: [])
    monkeypatch.setattr(scanner, 'get_premarket_leaders', lambda limit: [])
    monkeypatch.setattr(scanner, 'get_unusual_relvol', lambda limit: [])
    monkeypatch.setattr(scanner, 'apply_user_symbol_filters', lambda symbols, snapshots, quotes, user=None: symbols)
    monkeypatch.setattr(scanner, 'get_snapshots', lambda symbols, feed='iex': {s: {'minuteBar': {'c': 10}, 'prevDailyBar': {'c': 9}} for s in symbols})
    monkeypatch.setattr(scanner, 'get_latest_quotes', lambda symbols, feed='iex': {s: {'ap': 10} for s in symbols})
    monkeypatch.setattr(scanner, 'get_company_profile', lambda symbol: {})
    monkeypatch.setattr(scanner, 'get_alpaca_asset_with_diagnostics', lambda symbol, user=None: ({}, {'symbol': symbol, 'endpoint_used': 'x', 'auth_source': 'server_api_key', 'status_code': 401, 'ok': False, 'failure_reason': 'HTTP_401', 'response_text_short': 'unauthorized', 'used_fallback': False}))
    monkeypatch.setattr(scanner, 'get_bars', lambda symbols, *a, **k: {s: [{'t':'2026-05-08T13:30:00+00:00','o':1,'h':2,'l':1,'c':1.5,'v':1000}] for s in symbols})
    monkeypatch.setattr(scanner, 'fill_missing_bars_individually', lambda *a, **k: {'individual_bar_retry_attempted_count':0,'individual_bar_retry_success_count':0,'individual_bar_retry_failed_symbols':[]})
    monkeypatch.setattr(scanner, 'analyze_symbol', lambda symbol, *a, **k: {'symbol': symbol, 'decision': 'SKIP', 'setup_grade': 'NO TRADE', 'scores': {'catalyst': 0}, 'score_total': 0, 'details': {'open_relative_strength': {'edge': 0}, 'liquidity': {'spread': 0}, 'skip_reason_codes': []}})
    monkeypatch.setattr(scanner, 'get_stock_chart_pack', lambda *a, **k: {})
    out = scanner.run_scan()
    diag = out['scan_diagnostics']
    assert diag['asset_metadata_degraded_allowed_count'] == 1
    assert diag['asset_metadata_degraded_allowed_symbols'] == ['AAPL']
    assert diag['asset_metadata_degraded_rejection_counts']['WARRANT_OR_RIGHT'] == 1


def test_apply_user_symbol_filters_has_no_degraded_mode_locals():
    local_names = set(scanner.apply_user_symbol_filters.__code__.co_varnames)
    assert 'asset_metadata_degraded_allowed_symbols' not in local_names
    assert 'asset_metadata_degraded_rejections' not in local_names


def test_get_alpaca_asset_with_diagnostics_401(monkeypatch):
    class Resp:
        status_code = 401
        text = 'unauthorized secret'
        def json(self):
            return {}
    monkeypatch.setattr(scanner.requests, 'get', lambda *a, **k: Resp())
    asset, diag = scanner.get_alpaca_asset_with_diagnostics('AAPL')
    assert asset == {}
    assert diag['failure_reason'] == 'HTTP_401'
    assert diag['ok'] is False
    assert len(diag['response_text_short']) <= 180
