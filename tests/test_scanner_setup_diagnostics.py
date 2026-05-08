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


def test_calculate_premarket_dollar_volume_window_and_sum():
    bars = [
        _mk_bar('2026-05-08T08:10:00+00:00', c=8, v=10),   # 04:10 ET include
        _mk_bar('2026-05-08T13:20:00+00:00', c=12, v=5),   # 09:20 ET include
        _mk_bar('2026-05-08T13:40:00+00:00', c=100, v=5),  # 09:40 ET ignore
    ]
    out = scanner.calculate_premarket_dollar_volume("ABC", bars, {}, required_premarket_dollar_volume=100)
    assert out["actual_premarket_dollar_volume"] == 140
    assert out["premarket_bar_count"] == 2
    assert out["premarket_dollar_volume_passed"] is True


def test_calculate_premarket_dollar_volume_unavailable_reason():
    bars = [_mk_bar('2026-05-08T14:00:00+00:00', c=10, v=10)]
    out = scanner.calculate_premarket_dollar_volume("ABC", bars, {}, required_premarket_dollar_volume=100)
    assert out["actual_premarket_dollar_volume"] is None
    assert out["premarket_data_unavailable_reason"] == "NO_PREMARKET_BARS_IN_WINDOW"


def test_run_scan_raises_diagnostic_when_asset_filter_empties_candidates(monkeypatch):
    monkeypatch.setattr(scanner, 'update_dynamic_orb_state_from_market_data', lambda: None)
    monkeypatch.setattr(scanner, 'resolve_data_feed', lambda user=None: 'iex')
    monkeypatch.setattr(scanner, 'get_refined_universe', lambda user=None: ['AAPL'])
    monkeypatch.setattr(scanner, 'get_momentum_breakout_universe', lambda user=None: ([], []))
    monkeypatch.setattr(scanner, 'get_alpaca_movers', lambda limit: [])
    monkeypatch.setattr(scanner, 'get_premarket_leaders', lambda limit: [])
    monkeypatch.setattr(scanner, 'get_unusual_relvol', lambda limit: [])
    monkeypatch.setattr(scanner, 'apply_user_symbol_filters', lambda symbols, snapshots, quotes, user=None, candidate_source_map=None: symbols)
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
    monkeypatch.setattr(scanner, 'apply_user_symbol_filters', lambda symbols, snapshots, quotes, user=None, candidate_source_map=None: symbols)
    monkeypatch.setattr(scanner, 'get_snapshots', lambda symbols, feed='iex': {s: {'minuteBar': {'c': 10}, 'prevDailyBar': {'c': 9}} for s in symbols})
    monkeypatch.setattr(scanner, 'get_latest_quotes', lambda symbols, feed='iex': {s: {'ap': 10} for s in symbols})
    monkeypatch.setattr(scanner, 'get_company_profile', lambda symbol: {})
    def _asset_diag(symbol, user=None, source=None):
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
    monkeypatch.setattr(scanner, 'apply_user_symbol_filters', lambda symbols, snapshots, quotes, user=None, candidate_source_map=None: symbols)
    monkeypatch.setattr(scanner, 'get_snapshots', lambda symbols, feed='iex': {s: {'minuteBar': {'c': 10}, 'prevDailyBar': {'c': 9}} for s in symbols})
    monkeypatch.setattr(scanner, 'get_latest_quotes', lambda symbols, feed='iex': {s: {'ap': 10} for s in symbols})
    monkeypatch.setattr(scanner, 'get_company_profile', lambda symbol: {})
    monkeypatch.setattr(scanner, 'get_alpaca_asset_with_diagnostics', lambda symbol, user=None, source=None: ({}, {'symbol': symbol, 'endpoint_used': 'x', 'auth_source': 'server_api_key', 'status_code': 401, 'ok': False, 'failure_reason': 'HTTP_401', 'response_text_short': 'unauthorized', 'used_fallback': False}))
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


def test_get_alpaca_asset_with_diagnostics_defaults_source_unknown():
    _, diag = scanner.get_alpaca_asset_with_diagnostics('AAPL')
    assert diag['source'] == 'unknown'


def test_get_alpaca_asset_with_diagnostics_uses_explicit_source():
    _, diag = scanner.get_alpaca_asset_with_diagnostics('AAPL', source='fallback_market_candidates')
    assert diag['source'] == 'fallback_market_candidates'


def test_apply_user_symbol_filters_works_without_candidate_source_map():
    out = scanner.apply_user_symbol_filters(['AAPL'], snapshots={}, quotes={}, user=None)
    assert out == ['AAPL']


def test_apply_user_symbol_filters_candidate_source_map_tags_snapshot(monkeypatch):
    user = type('U', (), {'exclude_penny_stocks': False, 'exclude_biotech': False, 'esg_fossil_fuels': False, 'esg_weapons': False, 'esg_tobacco': False})()
    snapshots = {'AAPL': {'minuteBar': {'c': 10}}}
    quotes = {'AAPL': {'ap': 10}}
    monkeypatch.setattr(scanner, 'get_company_profile', lambda symbol: {})
    out = scanner.apply_user_symbol_filters(['AAPL'], snapshots=snapshots, quotes=quotes, user=user, candidate_source_map={'AAPL': 'momentum_breakout'})
    assert out == ['AAPL']


def test_source_priority_prefers_news_then_momentum_then_orb():
    assert scanner._select_primary_source(['orb_primary', 'momentum_breakout']) == 'momentum_breakout'
    assert scanner._select_primary_source(['fallback_market_candidates', 'news_catalyst']) == 'news_catalyst'
    assert scanner._select_primary_source([]) == 'unknown'


def test_get_news_catalyst_map_returns_headline_evidence(monkeypatch):
    monkeypatch.setattr(scanner, 'FINNHUB_API_KEY', 'x')
    monkeypatch.setattr(scanner, 'get_company_news', lambda symbol, lookback_days=1: [f"{symbol} wins contract"])
    out = scanner.get_news_catalyst_map(['AAPL'])
    assert out['AAPL']['headline_count'] == 1
    assert out['AAPL']['headline_samples']
    assert out['AAPL']['qualifies_as_news_catalyst'] is True


def test_get_news_catalyst_map_no_headlines_not_qualified(monkeypatch):
    monkeypatch.setattr(scanner, 'FINNHUB_API_KEY', 'x')
    monkeypatch.setattr(scanner, 'get_company_news', lambda symbol, lookback_days=1: [])
    out = scanner.get_news_catalyst_map(['AAPL'])
    assert out['AAPL']['news_lookup_status'] == 'NO_NEWS_FOUND'
    assert out['AAPL']['qualifies_as_news_catalyst'] is False


def test_get_news_catalyst_map_extracts_headline_from_dict(monkeypatch):
    monkeypatch.setattr(scanner, 'FINNHUB_API_KEY', 'x')
    monkeypatch.setattr(scanner, 'get_company_news', lambda symbol, lookback_days=1: [{'headline': 'FDA approval for AAPL', 'datetime': scanner.now_utc().timestamp()}])
    out = scanner.get_news_catalyst_map(['AAPL'])
    assert out['AAPL']['headline_samples'][0] == 'FDA approval for AAPL'
    assert '{' not in out['AAPL']['headline_samples'][0]
    assert 'contract' not in out['AAPL']['headline_samples'][0].lower()


def test_get_news_catalyst_map_latest_age_from_datetime(monkeypatch):
    monkeypatch.setattr(scanner, 'FINNHUB_API_KEY', 'x')
    monkeypatch.setattr(scanner, 'now_utc', lambda: scanner.datetime(2026, 5, 8, 13, 40, tzinfo=scanner.timezone.utc))
    monkeypatch.setattr(scanner, 'get_company_news', lambda symbol, lookback_days=1: [{'headline': 'AAPL contract win', 'datetime': scanner.datetime(2026, 5, 8, 13, 30, tzinfo=scanner.timezone.utc).timestamp()}])
    out = scanner.get_news_catalyst_map(['AAPL'])
    assert out['AAPL']['latest_headline_age_minutes'] == 10.0


def test_get_news_catalyst_map_keyword_hits(monkeypatch):
    monkeypatch.setattr(scanner, 'FINNHUB_API_KEY', 'x')
    monkeypatch.setattr(scanner, 'get_company_news', lambda symbol, lookback_days=1: [{'headline': 'AAPL wins major contract and FDA approval'}])
    out = scanner.get_news_catalyst_map(['AAPL'])
    assert 'contract' in out['AAPL']['positive_terms']
    assert 'approval' in out['AAPL']['positive_terms']


def test_get_news_catalyst_map_negative_terms(monkeypatch):
    monkeypatch.setattr(scanner, 'FINNHUB_API_KEY', 'x')
    monkeypatch.setattr(scanner, 'get_company_news', lambda symbol, lookback_days=1: [{'headline': 'AAPL announces offering and reverse split'}])
    out = scanner.get_news_catalyst_map(['AAPL'])
    assert 'offering' in out['AAPL']['negative_terms']
    assert 'reverse split' in out['AAPL']['negative_terms']


def test_build_catalyst_diagnostics_news_not_no_news():
    ml = {'headline_count': 1, 'recent_headline_count': 1, 'latest_headline_age_minutes': 10, 'keywords_hit': []}
    diag = scanner._build_catalyst_diagnostics(ml, {'reason': 'x'})
    assert diag['catalyst_headline_count'] > 0
    assert diag['catalyst_missing_reason'] != 'NO_NEWS_FOUND'


def test_run_scan_degraded_mode_blocks_known_etf_symbols(monkeypatch):
    monkeypatch.setattr(scanner, 'LEVERAGED_ETF_TRADING_ENABLED', False)
    monkeypatch.setattr(scanner, 'ETF_TRADING_ENABLED', False)
    monkeypatch.setattr(scanner, 'INVERSE_ETF_TRADING_ENABLED', False)
    monkeypatch.setattr(scanner, 'CRYPTO_ETF_TRADING_ENABLED', False)
    monkeypatch.setattr(scanner, 'update_dynamic_orb_state_from_market_data', lambda: None)
    monkeypatch.setattr(scanner, 'resolve_data_feed', lambda user=None: 'iex')
    monkeypatch.setattr(scanner, 'get_refined_universe', lambda user=None: ['TSLL', 'AAPL'])
    monkeypatch.setattr(scanner, 'get_momentum_breakout_universe', lambda user=None: ([], []))
    monkeypatch.setattr(scanner, 'get_alpaca_movers', lambda limit: [])
    monkeypatch.setattr(scanner, 'get_premarket_leaders', lambda limit: [])
    monkeypatch.setattr(scanner, 'get_unusual_relvol', lambda limit: [])
    monkeypatch.setattr(scanner, 'get_news_catalyst_map', lambda symbols, per_symbol=1: {})
    monkeypatch.setattr(scanner, 'apply_user_symbol_filters', lambda symbols, snapshots, quotes, user=None, candidate_source_map=None: symbols)
    monkeypatch.setattr(scanner, 'get_snapshots', lambda symbols, feed='iex': {s: {'minuteBar': {'c': 10}, 'prevDailyBar': {'c': 9}} for s in symbols})
    monkeypatch.setattr(scanner, 'get_latest_quotes', lambda symbols, feed='iex': {s: {'ap': 10} for s in symbols})
    monkeypatch.setattr(scanner, 'get_company_profile', lambda symbol: {})
    monkeypatch.setattr(scanner, 'get_alpaca_asset_with_diagnostics', lambda symbol, user=None, source=None: ({}, {'symbol': symbol, 'endpoint_used': 'x', 'status_code': 401, 'ok': False, 'failure_reason': 'HTTP_401'}))
    monkeypatch.setattr(scanner, 'get_bars', lambda symbols, *a, **k: {s: [{'t':'2026-05-08T13:30:00+00:00','o':1,'h':2,'l':1,'c':1.5,'v':1000}] for s in symbols})
    monkeypatch.setattr(scanner, 'fill_missing_bars_individually', lambda *a, **k: {'individual_bar_retry_attempted_count':0,'individual_bar_retry_success_count':0,'individual_bar_retry_failed_symbols':[]})
    monkeypatch.setattr(scanner, 'analyze_symbol', lambda symbol, *a, **k: {'symbol': symbol, 'decision': 'SKIP', 'setup_grade': 'NO TRADE', 'scores': {'catalyst': 2}, 'score_total': 0, 'details': {'open_relative_strength': {'edge': 0}, 'liquidity': {'spread': 0}, 'skip_reason_codes': []}})
    monkeypatch.setattr(scanner, 'get_stock_chart_pack', lambda *a, **k: {})
    out = scanner.run_scan()
    assert out['scan_diagnostics']['asset_metadata_degraded_rejection_counts'].get('LEVERAGED_ETF_BLOCKED_BY_SETTINGS', 0) >= 1


def test_run_scan_only_tags_qualified_news_catalyst(monkeypatch):
    monkeypatch.setattr(scanner, 'update_dynamic_orb_state_from_market_data', lambda: None)
    monkeypatch.setattr(scanner, 'resolve_data_feed', lambda user=None: 'iex')
    monkeypatch.setattr(scanner, 'get_refined_universe', lambda user=None: ['AAPL'])
    monkeypatch.setattr(scanner, 'get_momentum_breakout_universe', lambda user=None: ([], []))
    monkeypatch.setattr(scanner, 'get_alpaca_movers', lambda limit: [])
    monkeypatch.setattr(scanner, 'get_premarket_leaders', lambda limit: [])
    monkeypatch.setattr(scanner, 'get_unusual_relvol', lambda limit: [])
    monkeypatch.setattr(scanner, 'get_news_catalyst_map', lambda symbols, per_symbol=1: {
        'AAPL': {'symbol': 'AAPL', 'headline_count': 0, 'news_lookup_status': 'NO_NEWS_FOUND', 'qualifies_as_news_catalyst': False, 'headline_samples': [], 'keywords_hit': []},
        'MSFT': {'symbol': 'MSFT', 'headline_count': 1, 'news_lookup_status': 'FOUND', 'qualifies_as_news_catalyst': True, 'headline_samples': ['MSFT contract'], 'keywords_hit': ['contract']},
    })
    monkeypatch.setattr(scanner, 'apply_user_symbol_filters', lambda symbols, snapshots, quotes, user=None, candidate_source_map=None: symbols)
    monkeypatch.setattr(scanner, 'get_snapshots', lambda symbols, feed='iex': {s: {'minuteBar': {'c': 10}, 'prevDailyBar': {'c': 9}} for s in symbols})
    monkeypatch.setattr(scanner, 'get_latest_quotes', lambda symbols, feed='iex': {s: {'ap': 10} for s in symbols})
    monkeypatch.setattr(scanner, 'get_company_profile', lambda symbol: {})
    monkeypatch.setattr(scanner, 'get_alpaca_asset_with_diagnostics', lambda symbol, user=None, source=None: ({'tradable': True, 'fractionable': True, 'class': 'us_equity'}, {'ok': True, 'symbol': symbol}))
    monkeypatch.setattr(scanner, 'get_bars', lambda symbols, *a, **k: {s: [{'t':'2026-05-08T13:30:00+00:00','o':1,'h':2,'l':1,'c':1.5,'v':1000}] for s in symbols})
    monkeypatch.setattr(scanner, 'fill_missing_bars_individually', lambda *a, **k: {'individual_bar_retry_attempted_count':0,'individual_bar_retry_success_count':0,'individual_bar_retry_failed_symbols':[]})
    monkeypatch.setattr(scanner, 'analyze_symbol', lambda symbol, *a, **k: {'symbol': symbol, 'source': 'news_catalyst' if symbol == 'MSFT' else 'orb_primary', 'sources': ['news_catalyst'] if symbol == 'MSFT' else ['orb_primary'], 'decision': 'SKIP', 'setup_grade': 'NO TRADE', 'scores': {'catalyst': 2}, 'score_total': 0, 'details': {'open_relative_strength': {'edge': 0}, 'liquidity': {'spread': 0}, 'catalyst': {'catalyst_missing_reason': 'NO_NEWS_FOUND'}, 'skip_reason_codes': []}})
    monkeypatch.setattr(scanner, 'get_stock_chart_pack', lambda *a, **k: {})
    out = scanner.run_scan()
    diag = out['scan_diagnostics']
    assert diag['source_candidate_counts']['news_catalyst'] == 1
    assert 'AAPL' not in diag['news_catalyst_symbols_sample']
    assert 'AAPL' in diag['news_catalyst_nonqualifying_symbols_sample']
