from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
import logging
from statistics import mean
from typing import Any, Dict, List, Tuple, Optional
from zoneinfo import ZoneInfo

import requests
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from decision import regime_trade_decision, momentum_trade_decision
from filters import passes_hard_gatekeeper
from indicators import calc_rvol as indicators_calc_rvol, calc_spread_pct, calc_trend_efficiency as indicators_calc_trend_efficiency, calc_value_area
from models import ScoreTriplet, SymbolMarketStats, ComponentScores, WatchPanelDef, SymbolAnalysisResult
from setups import detect_orb
from utils import filter_bars_for_today_session, filter_bars_in_et_window, safe_num

from feature_store import store
import dynamic_orb
import market_state
from asset_classifier import classify_asset
import config
from config import (
    ALPACA_API_KEY,
    ALPACA_API_SECRET,
    ALPACA_DATA_BASE,
    CURRENT_BANKROLL,
    DEFAULT_RISK_CAPITAL,
    FINNHUB_API_KEY,
    MAX_BUY_SHARES,
    MAX_FLOAT,
    MAX_ENTRY_EXTENSION_PCT,
    MAX_PORTFOLIO_HEAT,
    MAX_SPREAD_PCT,
    MARKET_INTERNALS_ADD_SYMBOL,
    MARKET_INTERNALS_BLOCK_ENABLED,
    MARKET_INTERNALS_TICK_SYMBOL,
    MIN_CATALYST_SCORE,
    MIN_PREMARKET_DOLLAR_VOL,
    MIN_RVOL,
    MIN_PREMARKET_GAP_PCT,
    MIN_SECTOR_SYMPATHY_SCORE,
    A_PLUS_SCORE,
    A_SCORE,
    ATR_STOP_MULT,
    RS_SECTOR_MULT,
    MIN_SCORE_TO_EXECUTE,
    NO_BUY_BEFORE_ET,
    OPENING_RANGE_END_ET,
    OPENING_RANGE_START_ET,
    OR_BREAKOUT_BUFFER_PCT,
    PULLBACK_MAX_RETRACE_PCT,
    KELLY_FRACTION,
    SCAN_CANDIDATE_LIMIT,
    TIMEZONE_LABEL,
    VA_PERCENT,
    VIX_CIRCUIT_BREAKER_PCT,
    VIX_PENALTY_MULTIPLIER,
    VIX_SYMBOL,
    WATCHLIST_SIZE,
    MOMENTUM_BREAKOUT_MODE_ENABLED, MOMENTUM_WATCHLIST_SIZE, MOMENTUM_MIN_DAY_CHANGE_PCT, MOMENTUM_MIN_RVOL, MOMENTUM_MAX_SPREAD_PCT, MOMENTUM_MAX_ENTRY_EXTENSION_PCT, MOMENTUM_SCAN_CANDIDATE_LIMIT, MOMENTUM_MIN_DOLLAR_VOLUME, MOMENTUM_EXTREME_DAY_CHANGE_PCT, MOMENTUM_MIN_PRICE, MOMENTUM_MAX_PRICE, MOMENTUM_ALLOW_PENNY_STOCKS, BIOTECH_TRADING_ENABLED, ETF_TRADING_ENABLED, LEVERAGED_ETF_TRADING_ENABLED, INVERSE_ETF_TRADING_ENABLED, CRYPTO_ETF_TRADING_ENABLED, OPTIONS_TRADING_ENABLED, MOMENTUM_DEBUG_REJECTIONS_LIMIT,
)
TIMEOUT = 20
HIGH_GAP_THRESHOLD_PCT = 20.0
HIGH_GAP_MIN_PREMARKET_DOLLAR_VOL = 5_000_000
logger = logging.getLogger(__name__)

VETERAN_BLACKLIST = {
    'NVD', 'NVDL', 'NVDX', 'NVDQ', 'TQQQ', 'SQQQ', 'QLD', 'QID', 'SOXL', 'SOXS',
    'UPRO', 'SPXU', 'SPXL', 'SPXS', 'UVXY', 'VIXY', 'SVIX', 'BOIL', 'KOLD', 'UCO',
    'SCO', 'YINN', 'YANG', 'JNUG', 'JDST', 'FAS', 'FAZ'
}


class ScanError(Exception):
    pass




def _scanner_verbose_debug_enabled() -> bool:
    return str(os.getenv("SCANNER_VERBOSE_DEBUG", "0")).strip().lower() in {"1", "true", "yes", "on"}


def _scanner_debug(message: str, *args: Any) -> None:
    if _scanner_verbose_debug_enabled():
        logger.debug(message, *args)


def resolve_data_feed(user: Optional[Any] = None) -> str:
    candidate = (getattr(user, 'alpaca_data_feed', '') or '').strip().lower()
    return candidate if candidate in {'iex', 'sip'} else 'iex'


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_et() -> datetime:
    return datetime.now(ZoneInfo(TIMEZONE_LABEL))


def parse_hhmm(value: str) -> Tuple[int, int]:
    hh, mm = [int(x) for x in value.split(':', 1)]
    return hh, mm


def buy_window_open() -> bool:
    hh, mm = parse_hhmm(NO_BUY_BEFORE_ET)
    start = now_et().replace(hour=hh, minute=mm, second=0, microsecond=0)
    return now_et() >= start


def _alpaca_headers() -> Dict[str, str]:
    if not ALPACA_API_KEY or not ALPACA_API_SECRET:
        raise ScanError('Missing Alpaca API credentials. Put ALPACA_API_KEY and ALPACA_API_SECRET in .env')
    return {
        'accept': 'application/json',
        'APCA-API-KEY-ID': ALPACA_API_KEY,
        'APCA-API-SECRET-KEY': ALPACA_API_SECRET,
    }


def _get_json(url: str, params: Dict[str, Any] | None = None, headers: Dict[str, str] | None = None) -> Any:
    resp = _get_json_with_retry(url, params=params, headers=headers)
    if resp.status_code >= 400:
        resp.raise_for_status()
    return resp.json()


def _is_retryable_request_error(exc: BaseException) -> bool:
    if isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
        return True
    if isinstance(exc, requests.exceptions.HTTPError):
        response = getattr(exc, 'response', None)
        return bool(response is not None and response.status_code >= 500)
    return False


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=10),
    retry=retry_if_exception(_is_retryable_request_error),
    reraise=True,
)
def _get_json_with_retry(
    url: str,
    params: Dict[str, Any] | None = None,
    headers: Dict[str, str] | None = None,
) -> requests.Response:
    resp = requests.get(url, params=params or {}, headers=headers or {}, timeout=TIMEOUT)
    if resp.status_code >= 500:
        resp.raise_for_status()
    return resp


def bar_dt_et(bar: Dict[str, Any]) -> datetime | None:
    ts = bar.get('t', '')
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace('Z', '+00:00')).astimezone(ZoneInfo(TIMEZONE_LABEL))
    except Exception:
        return None


def get_market_candidates(limit: int = SCAN_CANDIDATE_LIMIT) -> List[str]:
    headers = _alpaca_headers()
    candidates: List[str] = []
    endpoints = (
        '/v1beta1/screener/stocks/most-actives',
        '/v1beta1/screener/stocks/movers',
    )
    for endpoint in endpoints:
        try:
            data = _get_json(f'{ALPACA_DATA_BASE}{endpoint}', params={'top': limit}, headers=headers)
        except requests.RequestException:
            continue
        if isinstance(data, dict):
            for key in ('most_actives', 'gainers', 'data'):
                items = data.get(key) or []
                if isinstance(items, list):
                    for item in items:
                        symbol = (item.get('symbol') or '').upper()
                        if symbol and symbol.isalpha() and len(symbol) <= 5 and symbol not in VETERAN_BLACKLIST:
                            candidates.append(symbol)
    deduped, seen = [], set()
    for symbol in candidates:
        if symbol not in seen:
            seen.add(symbol)
            deduped.append(symbol)
    if 'SPY' not in seen:
        deduped.append('SPY')
    return deduped[: max(limit, 8)]



def _extract_symbols(items: List[Dict[str, Any]]) -> List[str]:
    out: List[str] = []
    for item in items:
        symbol = str(item.get('symbol') or '').upper().strip()
        if symbol and symbol.isalpha() and len(symbol) <= 5 and symbol not in VETERAN_BLACKLIST:
            out.append(symbol)
    return out


def get_alpaca_movers(limit: int = SCAN_CANDIDATE_LIMIT) -> List[str]:
    try:
        data = _get_json(f'{ALPACA_DATA_BASE}/v1beta1/screener/stocks/movers', params={'top': limit}, headers=_alpaca_headers())
    except requests.RequestException:
        return []
    return _extract_symbols(data.get('gainers', []) if isinstance(data, dict) else [])


def get_premarket_leaders(limit: int = SCAN_CANDIDATE_LIMIT) -> List[str]:
    try:
        data = _get_json(f'{ALPACA_DATA_BASE}/v1beta1/screener/stocks/most-actives', params={'top': limit}, headers=_alpaca_headers())
    except requests.RequestException:
        return []
    return _extract_symbols(data.get('most_actives', []) if isinstance(data, dict) else [])


def get_unusual_relvol(limit: int = SCAN_CANDIDATE_LIMIT) -> List[str]:
    try:
        data = _get_json(f'{ALPACA_DATA_BASE}/v1beta1/screener/stocks/movers', params={'top': limit}, headers=_alpaca_headers())
    except requests.RequestException:
        return []
    return _extract_symbols(data.get('gainers', []) if isinstance(data, dict) else [])


def get_news_catalyst_map(candidates: List[str], per_symbol: int = 1) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    if not FINNHUB_API_KEY:
        for symbol in candidates[: max(6, min(len(candidates), SCAN_CANDIDATE_LIMIT))]:
            out[symbol] = {
                'symbol': symbol,
                'headline_count': 0,
                'recent_headline_count': 0,
                'latest_headline_age_minutes': None,
                'headline_samples': [],
                'keywords_hit': [],
                'positive_terms': [],
                'negative_terms': [],
                'news_source': 'finnhub',
                'news_lookup_status': 'FINNHUB_KEY_MISSING',
                'raw_news_count': 0,
                'qualifies_as_news_catalyst': False,
            }
        return out
    for symbol in candidates[: max(6, min(len(candidates), SCAN_CANDIDATE_LIMIT))]:
        try:
            headlines = get_company_news(symbol, lookback_days=1)
        except Exception:
            out[symbol] = {
                'symbol': symbol,
                'headline_count': 0,
                'recent_headline_count': 0,
                'latest_headline_age_minutes': None,
                'headline_samples': [],
                'keywords_hit': [],
                'positive_terms': [],
                'negative_terms': [],
                'news_source': 'finnhub',
                'news_lookup_status': 'API_ERROR',
                'raw_news_count': 0,
                'qualifies_as_news_catalyst': False,
            }
            continue
        if not isinstance(headlines, list):
            out[symbol] = {
                'symbol': symbol,
                'headline_count': 0,
                'recent_headline_count': 0,
                'latest_headline_age_minutes': None,
                'headline_samples': [],
                'keywords_hit': [],
                'positive_terms': [],
                'negative_terms': [],
                'news_source': 'finnhub',
                'news_lookup_status': 'INVALID_RESPONSE',
                'raw_news_count': 0,
                'qualifies_as_news_catalyst': False,
            }
            continue
        extracted = [_extract_news_headline(item) for item in headlines]
        clean_headlines = [h for h in extracted if h]
        samples = [_truncate_headline(h) for h in clean_headlines[:3]]
        keyword_diag = _extract_news_keywords(clean_headlines)
        timestamps = [ts for ts in [_extract_news_timestamp(item) for item in headlines] if ts is not None]
        latest_age_minutes = None
        if timestamps:
            newest = max(timestamps)
            latest_age_minutes = max(0.0, round((now_utc() - newest).total_seconds() / 60.0, 2))
        news_lookup_status = 'FOUND' if clean_headlines else 'NO_NEWS_FOUND'
        qualifies = bool(len(clean_headlines) >= per_symbol and news_lookup_status == 'FOUND')
        out[symbol] = {
            'symbol': symbol,
            'headline_count': len(clean_headlines),
            'recent_headline_count': len(clean_headlines),
            'latest_headline_age_minutes': latest_age_minutes,
            'headline_samples': samples,
            'keywords_hit': keyword_diag['keywords_hit'],
            'positive_terms': keyword_diag['positive_terms'],
            'negative_terms': keyword_diag['negative_terms'],
            'news_source': 'finnhub',
            'news_lookup_status': news_lookup_status,
            'raw_news_count': len(headlines),
            'qualifies_as_news_catalyst': qualifies,
        }
    return out


def get_news_catalyst_list(candidates: List[str], per_symbol: int = 1) -> List[str]:
    return [
        s for s, payload in get_news_catalyst_map(candidates, per_symbol=per_symbol).items()
        if int(payload.get('headline_count', 0) or 0) >= per_symbol
    ]


def _matches_industry(industry: str, keywords: List[str]) -> bool:
    text = (industry or '').lower()
    return any(keyword in text for keyword in keywords)


def apply_user_symbol_filters(
    symbols: List[str],
    snapshots: Dict[str, Any],
    quotes: Dict[str, Any],
    user: Optional[Any] = None,
    candidate_source_map: Optional[Dict[str, str]] = None,
) -> List[str]:
    candidate_source_map = candidate_source_map or {}
    if user is None:
        return symbols

    filtered: List[str] = []
    for symbol in symbols:
        if symbol == 'SPY':
            filtered.append(symbol)
            continue

        snapshot = dict(snapshots.get(symbol, {}) or {})
        snapshot['_candidate_source'] = candidate_source_map.get(symbol, 'unknown')
        quote = quotes.get(symbol, {})
        daily = snapshot.get('dailyBar', {})
        minute = snapshot.get('minuteBar', {})
        prev = snapshot.get('prevDailyBar', {})
        price = safe_num(quote.get('ap')) or safe_num(minute.get('c')) or safe_num(daily.get('c')) or safe_num(prev.get('c'))
        if price <= 0:
            continue
        if bool(getattr(user, 'exclude_penny_stocks', True)) and price > 0 and price < 5.0:
            continue

        profile = get_company_profile(symbol)
        industry = str(profile.get('finnhubIndustry') or profile.get('gind') or '')
        if bool(getattr(user, 'exclude_biotech', False)) and _matches_industry(industry, ['biotech', 'biotechnology', 'pharmaceutical']):
            continue
        if bool(getattr(user, 'esg_fossil_fuels', False)) and _matches_industry(industry, ['oil', 'gas', 'coal', 'energy']):
            continue
        if bool(getattr(user, 'esg_weapons', False)) and _matches_industry(industry, ['defense', 'firearm', 'weapons', 'aerospace']):
            continue
        if bool(getattr(user, 'esg_tobacco', False)) and _matches_industry(industry, ['tobacco', 'nicotine']):
            continue

        filtered.append(symbol)

    if 'SPY' not in filtered and 'SPY' in symbols:
        filtered.append('SPY')
    return filtered


def get_refined_universe(limit: int = SCAN_CANDIDATE_LIMIT, user: Optional[Any] = None) -> List[str]:
    candidates = set()
    candidates.update(get_alpaca_movers(limit))
    candidates.update(get_premarket_leaders(limit))
    candidates.update(get_unusual_relvol(limit))
    candidates.update(get_news_catalyst_list(list(candidates) or get_market_candidates(limit)))

    if 'SPY' not in candidates:
        candidates.add('SPY')

    feed = resolve_data_feed(user)
    snapshots = get_snapshots(list(candidates), feed=feed)
    quotes = get_latest_quotes(list(candidates), feed=feed)

    valid: List[str] = []
    for symbol in candidates:
        snap = snapshots.get(symbol, {})
        quote = quotes.get(symbol, {})
        daily = snap.get('dailyBar', {})
        minute = snap.get('minuteBar', {})
        prev = snap.get('prevDailyBar', {})
        price = safe_num(quote.get('ap')) or safe_num(minute.get('c')) or safe_num(daily.get('c')) or safe_num(prev.get('c'))
        if price <= 0:
            continue

        # FIX 1: Allow stocks up to $500.00
        if symbol != 'SPY' and not (1.0 <= price <= 500.0):
            continue

        day_vol = safe_num(daily.get('v')) or safe_num(prev.get('v'))
        dollar_volume = day_vol * max(price, 0)

        # TEMPORARY: Lowering volume requirement slightly so we definitely get symbols
        if symbol != 'SPY' and dollar_volume < 1_000_000:
            continue

        bid = safe_num(quote.get('bp'))
        ask = safe_num(quote.get('ap'))
        spread_pct = calc_spread_pct(bid, ask, price)

        strict_scanner = config.STRICT_PRODUCTION_SCANNER or config.IS_PRODUCTION
        if symbol != 'SPY' and strict_scanner:
            market_stats = SymbolMarketStats(
                symbol=symbol,
                price=price,
                daily_dollar_volume=dollar_volume,
                spread_pct=spread_pct,
            )
            keep, _ = passes_hard_gatekeeper(market_stats)
            if not keep:
                continue
        valid.append(symbol)

    if 'SPY' not in valid:
        valid.append('SPY')
    return valid[: max(limit, 12)]


def get_snapshots(symbols: List[str], feed: str = 'iex') -> Dict[str, Any]:
    if not symbols:
        logger.debug("Skipping get_snapshots() because symbols list is empty.")
        return {}
    data = _get_json(
        f'{ALPACA_DATA_BASE}/v2/stocks/snapshots',
        params={'symbols': ','.join(symbols), 'feed': feed},
        headers=_alpaca_headers(),
    )
    return data.get('snapshots', data)


def get_latest_quotes(symbols: List[str], feed: str = 'iex') -> Dict[str, Any]:
    if not symbols:
        logger.debug("Skipping get_latest_quotes() because symbols list is empty.")
        return {}
    data = _get_json(
        f'{ALPACA_DATA_BASE}/v2/stocks/quotes/latest',
        params={'symbols': ','.join(symbols), 'feed': feed},
        headers=_alpaca_headers(),
    )
    return data.get('quotes', data)


def get_bars(
    symbols: List[str],
    timeframe: str,
    start: datetime,
    end: datetime,
    limit: int,
    feed: str = 'iex',
) -> Dict[str, List[Dict[str, Any]]]:
    if not symbols:
        logger.debug("Skipping get_bars() because symbols list is empty. timeframe=%s", timeframe)
        return {}
    data = _get_json(
        f'{ALPACA_DATA_BASE}/v2/stocks/bars',
        params={
            'symbols': ','.join(symbols),
            'timeframe': timeframe,
            'start': start.isoformat(),
            'end': end.isoformat(),
            'limit': limit,
            'adjustment': 'split',
            'feed': feed,
        },
        headers=_alpaca_headers(),
    )
    return data.get('bars', {})


def get_vix_change(feed: str = 'iex') -> float:
    """Calculates the 1-hour percentage change for VIXY proxy volatility."""
    end = now_utc()
    start = end - timedelta(hours=1)
    try:
        bars = get_bars([VIX_SYMBOL], '1Min', start, end, 60, feed=feed).get(VIX_SYMBOL, [])
    except Exception:
        return 0.0
    if len(bars) < 2:
        return 0.0
    start_price = safe_num(bars[0].get('c'))
    curr_price = safe_num(bars[-1].get('c'))
    return ((curr_price - start_price) / start_price * 100.0) if start_price > 0 else 0.0


def check_vix_circuit_breaker() -> bool:
    """Return True when VIX proxy volatility spikes beyond configured threshold."""
    return get_vix_change() >= VIX_CIRCUIT_BREAKER_PCT


def has_positive_mtf_vwap_trend(minute_bars: List[Dict[str, Any]], chunk_size: int = 5) -> bool:
    session = filter_bars_for_today_session(minute_bars)
    if len(session) < chunk_size * 6:
        return False
    five_minute_blocks = [session[i:i + chunk_size] for i in range(0, len(session), chunk_size)]
    recent_blocks = [b for b in five_minute_blocks if len(b) == chunk_size][-6:]
    if len(recent_blocks) < 4:
        return False
    vwap_series = [calc_vwap(block) for block in recent_blocks]
    return all(vwap_series[i] >= vwap_series[i - 1] for i in range(1, len(vwap_series)))


def get_company_news(symbol: str, lookback_days: Optional[int] = None) -> List[Dict[str, Any]]:
    if not FINNHUB_API_KEY:
        return []
    today = datetime.utcnow().date()
    if lookback_days is None:
        lookback_days = 3 if datetime.utcnow().weekday() == 0 else 1
    start = today - timedelta(days=lookback_days)
    try:
        payload = _get_json(
            'https://finnhub.io/api/v1/company-news',
            params={'symbol': symbol, 'from': start.isoformat(), 'to': today.isoformat(), 'token': FINNHUB_API_KEY},
        )
        return payload if isinstance(payload, list) else []
    except requests.RequestException:
        return []


def get_company_profile(symbol: str) -> Dict[str, Any]:
    if not FINNHUB_API_KEY:
        return {}
    try:
        payload = _get_json('https://finnhub.io/api/v1/stock/profile2', params={'symbol': symbol, 'token': FINNHUB_API_KEY})
        return payload if isinstance(payload, dict) else {}
    except requests.RequestException:
        return {}


def _asset_failure_reason(status_code: int | None, error: Exception | None = None, payload: Any = None) -> str:
    if error is not None:
        return 'REQUEST_EXCEPTION'
    if status_code == 401:
        return 'HTTP_401'
    if status_code == 403:
        return 'HTTP_403'
    if status_code == 404:
        return 'HTTP_404'
    if status_code == 429:
        return 'HTTP_429'
    if status_code is not None and status_code >= 500:
        return 'HTTP_5XX'
    if payload in ({}, None, ''):
        return 'EMPTY_RESPONSE'
    return 'INVALID_RESPONSE'


def get_alpaca_asset_with_diagnostics(
    symbol: str,
    user: Optional[Any] = None,
    source: Optional[str] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    token = None
    if user is not None:
        token = getattr(user, 'alpaca_live_access_token', None) or getattr(user, 'alpaca_paper_access_token', None) or getattr(user, 'alpaca_access_token', None)
    auth_source = 'user_oauth_token' if token else ('server_api_key' if ALPACA_API_KEY and ALPACA_API_SECRET else 'none')
    endpoint_used = f"{config.ALPACA_ASSETS_BASE}/v2/assets/{symbol}"
    diag = {
        'symbol': symbol,
        'source': source or 'unknown',
        'endpoint_used': endpoint_used,
        'auth_source': auth_source,
        'status_code': None,
        'ok': False,
        'failure_reason': None,
        'response_text_short': '',
        'used_fallback': False,
    }
    headers = {'accept': 'application/json'}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    elif ALPACA_API_KEY and ALPACA_API_SECRET:
        headers.update(_alpaca_headers())
    else:
        diag['failure_reason'] = 'REQUEST_EXCEPTION'
        return {}, diag
    try:
        resp = requests.get(endpoint_used, headers=headers, timeout=TIMEOUT)
        diag['status_code'] = resp.status_code
        diag['response_text_short'] = (resp.text or '')[:180]
        if resp.status_code >= 400:
            diag['failure_reason'] = _asset_failure_reason(resp.status_code)
            return {}, diag
        payload = resp.json()
        if not isinstance(payload, dict) or not payload:
            diag['failure_reason'] = _asset_failure_reason(resp.status_code, payload=payload)
            return {}, diag
        diag['ok'] = True
        return payload, diag
    except requests.RequestException as exc:
        diag['failure_reason'] = _asset_failure_reason(None, error=exc)
        diag['response_text_short'] = str(exc)[:180]
        return {}, diag
    except ValueError:
        diag['failure_reason'] = 'INVALID_RESPONSE'
        return {}, diag


def get_alpaca_asset(symbol: str) -> Dict[str, Any]:
    payload, _ = get_alpaca_asset_with_diagnostics(symbol)
    return payload


def extract_float_shares(profile: Dict[str, Any], asset: Dict[str, Any]) -> float:
    float_candidates = (
        asset.get('float'),
        asset.get('shares_float'),
        asset.get('float_shares'),
        profile.get('floatShares'),
        profile.get('shareFloat'),
    )
    for raw in float_candidates:
        val = safe_num(raw)
        if val > 0:
            return val
    finnhub_float_millions = safe_num(profile.get('shareOutstanding'))
    if finnhub_float_millions > 0:
        return finnhub_float_millions * 1_000_000
    return 0.0


def calc_atr(bars: List[Dict[str, Any]], period: int = 14) -> float:
    if len(bars) < 2:
        return 0.0
    true_ranges = []
    prev_close = safe_num(bars[0].get('c'))
    for bar in bars[1:]:
        high = safe_num(bar.get('h'))
        low = safe_num(bar.get('l'))
        close = safe_num(bar.get('c'))
        true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
        prev_close = close
    sample = true_ranges[-period:] if len(true_ranges) >= period else true_ranges
    return mean(sample) if sample else 0.0


def calc_vwap(minute_bars: List[Dict[str, Any]]) -> float:
    total_pv = 0.0
    total_v = 0.0
    for b in minute_bars:
        typical = (safe_num(b.get('h')) + safe_num(b.get('l')) + safe_num(b.get('c'))) / 3.0
        vol = safe_num(b.get('v'))
        total_pv += typical * vol
        total_v += vol
    return total_pv / total_v if total_v > 0 else 0.0
    
def calc_daily_volume_poc(minute_bars: List[Dict[str, Any]], min_tick: float = 0.01) -> float:
    session = filter_bars_for_today_session(minute_bars)
    if not session:
        return 0.0
    ladder: Dict[float, float] = {}
    tick = max(0.0001, min_tick)
    for bar in session:
        typical = (safe_num(bar.get('h')) + safe_num(bar.get('l')) + safe_num(bar.get('c'))) / 3.0
        vol = safe_num(bar.get('v'))
        if typical <= 0 or vol <= 0:
            continue
        px = round(round(typical / tick) * tick, 4)
        ladder[px] = ladder.get(px, 0.0) + vol
    if not ladder:
        return 0.0
    return max(ladder.items(), key=lambda kv: kv[1])[0]


def premarket_dollar_volume(minute_bars: List[Dict[str, Any]]) -> float:
    total = 0.0
    for b in minute_bars:
        dt = bar_dt_et(b)
        if not dt:
            continue
        mins = dt.hour * 60 + dt.minute
        if 4 * 60 <= mins < 9 * 60 + 30:
            total += safe_num(b.get('c')) * safe_num(b.get('v'))
    return total


def calculate_premarket_dollar_volume(symbol: str, minute_bars: List[Dict[str, Any]], snapshot: Dict[str, Any], scanner_now_et: Optional[datetime] = None, required_premarket_dollar_volume: Optional[float] = None) -> Dict[str, Any]:
    now_ref = scanner_now_et if isinstance(scanner_now_et, datetime) else now_et()
    if now_ref.tzinfo is None:
        now_ref = ET.localize(now_ref)
    window_start = now_ref.replace(hour=4, minute=0, second=0, microsecond=0)
    window_end = now_ref.replace(hour=9, minute=30, second=0, microsecond=0)
    bars_in_window: List[Dict[str, Any]] = []
    bad_timestamps = 0
    for bar in minute_bars or []:
        dt = bar_dt_et(bar)
        if not dt:
            bad_timestamps += 1
            continue
        if window_start <= dt < window_end:
            bars_in_window.append(bar)

    total_dollar_volume = 0.0
    total_volume = 0.0
    earliest_dt = None
    latest_dt = None
    for bar in bars_in_window:
        dt = bar_dt_et(bar)
        if not dt:
            continue
        earliest_dt = dt if earliest_dt is None else min(earliest_dt, dt)
        latest_dt = dt if latest_dt is None else max(latest_dt, dt)
        close_px = safe_num(bar.get('c'))
        if close_px <= 0:
            close_px = (safe_num(bar.get('o')) + safe_num(bar.get('h')) + safe_num(bar.get('l')) + safe_num(bar.get('c'))) / 4.0
        vol = max(0.0, safe_num(bar.get('v')))
        total_dollar_volume += (close_px * vol)
        total_volume += vol

    source = 'unavailable'
    unavailable_reason = None
    actual = round(total_dollar_volume, 2) if total_volume > 0 else None
    if actual is not None:
        source = 'minute_bars_extended_hours'
    elif minute_bars:
        unavailable_reason = 'NO_PREMARKET_BARS_IN_WINDOW' if bad_timestamps == 0 else 'BAD_BAR_TIMESTAMPS'
    else:
        unavailable_reason = 'NO_MINUTE_BARS'

    feed_used = str((snapshot or {}).get('_feed_used') or '').lower().strip()
    if actual is None and feed_used == 'iex':
        unavailable_reason = 'FEED_DOES_NOT_INCLUDE_EXTENDED_HOURS'
    passed = bool(actual is not None and required_premarket_dollar_volume is not None and actual >= required_premarket_dollar_volume)
    gap = None if actual is None or required_premarket_dollar_volume is None else round(actual - required_premarket_dollar_volume, 2)
    return {
        'symbol': symbol,
        'source': snapshot.get('_candidate_source', 'unknown'),
        'actual_premarket_dollar_volume': actual,
        'premarket_bar_count': len(bars_in_window),
        'premarket_volume': round(total_volume, 2),
        'premarket_vwap_or_avg_price': round(total_dollar_volume / total_volume, 4) if total_volume > 0 else None,
        'premarket_window_start_et': window_start.isoformat(),
        'premarket_window_end_et': window_end.isoformat(),
        'earliest_premarket_bar_et': earliest_dt.isoformat() if earliest_dt else None,
        'latest_premarket_bar_et': latest_dt.isoformat() if latest_dt else None,
        'premarket_data_available': actual is not None,
        'premarket_data_source': source,
        'premarket_data_unavailable_reason': unavailable_reason or 'UNKNOWN',
        'required_premarket_dollar_volume': round(required_premarket_dollar_volume, 2) if required_premarket_dollar_volume is not None else None,
        'premarket_dollar_volume_gap': gap,
        'premarket_dollar_volume_passed': passed,
    }


def to_chart_bars(bars: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for b in bars:
        ts = b.get('t')
        if not ts:
            continue
        try:
            epoch = int(datetime.fromisoformat(ts.replace('Z', '+00:00')).timestamp())
        except Exception:
            continue
        out.append({
            'time': epoch,
            'open': round(safe_num(b.get('o')), 4),
            'high': round(safe_num(b.get('h')), 4),
            'low': round(safe_num(b.get('l')), 4),
            'close': round(safe_num(b.get('c')), 4),
            'value': round(safe_num(b.get('v')), 2),
        })
    return out


def get_stock_chart_pack(symbol: str, user: Optional[Any] = None) -> Dict[str, Any]:
    feed = resolve_data_feed(user)
    end = now_utc()
    daily_start = end - timedelta(days=260)
    intraday_start = end - timedelta(days=3)
    daily = get_bars([symbol], '1Day', daily_start, end, 260, feed=feed).get(symbol, [])
    intraday = get_bars([symbol], '1Min', intraday_start, end, 1000, feed=feed).get(symbol, [])
    return {'symbol': symbol, 'daily': to_chart_bars(daily[-220:]), 'intraday': to_chart_bars(intraday[-390:])}


def score_float_liquidity(profile: Dict[str, Any], asset: Dict[str, Any], premarket_notional: float, day_volume: float, spread: float, atr: float, current_price: float) -> Tuple[int, Dict[str, Any]]:
    shares_out = safe_num(profile.get('shareOutstanding')) * 1_000_000
    float_shares = extract_float_shares(profile, asset)
    high_float_block = bool(float_shares and float_shares > MAX_FLOAT)
    float_proxy_ok = 10_000_000 <= shares_out <= 50_000_000 if shares_out > 0 else False
    spread_pct = spread / current_price if current_price > 0 else 1.0
    score = 1
    if premarket_notional >= 5_000_000 and spread_pct <= 0.0015 and atr > 0.25 and float_proxy_ok:
        score = 5
    elif premarket_notional >= 2_500_000 and spread_pct <= 0.0025 and atr > 0.18 and float_proxy_ok:
        score = 4
    elif premarket_notional >= 1_500_000 and spread_pct <= MAX_SPREAD_PCT and atr > 0.12:
        score = 3
    elif day_volume >= 1_000_000 and spread_pct <= 0.005:
        score = 2
    if high_float_block:
        score = 1
    return score, {
        'shares_outstanding_proxy': round(shares_out, 0) if shares_out else None,
        'float_shares': round(float_shares, 0) if float_shares else None,
        'high_float_block': high_float_block,
        'float_sweet_spot_proxy': float_proxy_ok,
        'premarket_dollar_volume': round(premarket_notional, 2),
        'spread': round(spread, 4),
        'spread_pct': round(spread_pct, 4),
        'atr': round(atr, 4),
        'wide_spread_block': spread_pct > MAX_SPREAD_PCT,
    }


def score_catalyst(symbol: str, price_change_pct: float) -> Tuple[int, Dict[str, Any]]:
    _ = price_change_pct
    ml_features = store.get_symbol_features(symbol)
    p_success = float(ml_features.get('p_success', 0.50) or 0.50)
    sentiment = float(ml_features.get('finbert_sentiment', 0.0) or 0.0)
    keyword_boost = float(ml_features.get('keyword_boost', 0.0) or 0.0)
    catalyst_score = max(1, min(5, int(round(p_success * 5))))

    return catalyst_score, {
        'used_ai': True,
        'model': 'FinBERT + XGBoost',
        'sentiment_score': sentiment,
        'p_success': p_success,
        'keyword_boost': keyword_boost,
        'headline_count': int(ml_features.get('headline_count', 0) or 0),
        'hard_pass': p_success < 0.20,
        'catalyst_category_weight': catalyst_score,
        'direction': 'bullish' if sentiment >= 0 else 'mixed',
        'confidence': 'medium',
        'reason': 'Loaded from pre-market feature store.',
    }


SECTOR_ETF_MAP = {
    'technology': 'XLK',
    'semiconductors': 'SMH',
    'financial services': 'XLF',
    'banks': 'KBE',
    'healthcare': 'XLV',
    'biotechnology': 'XBI',
    'consumer defensive': 'XLP',
    'consumer cyclical': 'XLY',
    'communication services': 'XLC',
    'industrials': 'XLI',
    'energy': 'XLE',
    'utilities': 'XLU',
    'real estate': 'XLRE',
    'materials': 'XLB',
}


def classify_setup_grade(total: int, catalyst_score: int, liquidity_score: int, sector_score: int, confirm_score: int, vwap_score: int, pullback_score: int, premarket_gap_pct: float, premarket_notional: float) -> str:
    if (
        total >= A_PLUS_SCORE
        and catalyst_score >= 5
        and liquidity_score >= 4
        and sector_score >= 4
        and confirm_score >= 4
        and vwap_score >= 4
        and pullback_score >= 4
        and premarket_gap_pct >= max(8.0, MIN_PREMARKET_GAP_PCT)
        and premarket_notional >= max(3_500_000, MIN_PREMARKET_DOLLAR_VOL)
    ):
        return 'A+'
    if (
        total >= A_SCORE
        and catalyst_score >= 4
        and liquidity_score >= 3
        and sector_score >= MIN_SECTOR_SYMPATHY_SCORE
        and confirm_score >= 3
        and vwap_score >= 3
        and premarket_gap_pct >= MIN_PREMARKET_GAP_PCT
        and premarket_notional >= MIN_PREMARKET_DOLLAR_VOL
    ):
        return 'A'
    if total >= (A_SCORE - 4) and catalyst_score >= 4:
        return 'WATCH'
    return 'NO TRADE'


def build_setup_grade_diagnostics(
    total: int,
    catalyst_score: int,
    liquidity_score: int,
    sector_score: int,
    confirm_score: int,
    vwap_score: int,
    pullback_score: int,
    premarket_gap_pct: float,
    premarket_notional: float,
) -> Dict[str, Any]:
    a_plus_gap_threshold = max(8.0, MIN_PREMARKET_GAP_PCT)
    a_plus_notional_threshold = max(3_500_000, MIN_PREMARKET_DOLLAR_VOL)
    a_gap_threshold = MIN_PREMARKET_GAP_PCT
    a_notional_threshold = MIN_PREMARKET_DOLLAR_VOL
    watch_total_threshold = A_SCORE - 4
    watch_catalyst_threshold = 4

    failed_a_plus = []
    if total < A_PLUS_SCORE:
        failed_a_plus.append('TOTAL_SCORE_BELOW_A_PLUS_THRESHOLD')
    if catalyst_score < 5:
        failed_a_plus.append('CATALYST_SCORE_BELOW_A_PLUS_THRESHOLD')
    if liquidity_score < 4:
        failed_a_plus.append('LIQUIDITY_SCORE_BELOW_A_PLUS_THRESHOLD')
    if sector_score < 4:
        failed_a_plus.append('SECTOR_SCORE_BELOW_A_PLUS_THRESHOLD')
    if confirm_score < 4:
        failed_a_plus.append('CONFIRM_SCORE_BELOW_A_PLUS_THRESHOLD')
    if vwap_score < 4:
        failed_a_plus.append('VWAP_SCORE_BELOW_A_PLUS_THRESHOLD')
    if pullback_score < 4:
        failed_a_plus.append('PULLBACK_SCORE_BELOW_A_PLUS_THRESHOLD')
    if premarket_gap_pct < a_plus_gap_threshold:
        failed_a_plus.append('PREMARKET_GAP_BELOW_A_PLUS_THRESHOLD')
    if premarket_notional < a_plus_notional_threshold:
        failed_a_plus.append('PREMARKET_DOLLAR_VOLUME_BELOW_A_PLUS_THRESHOLD')

    failed_a = []
    if total < A_SCORE:
        failed_a.append('TOTAL_SCORE_BELOW_A_THRESHOLD')
    if catalyst_score < 4:
        failed_a.append('CATALYST_SCORE_BELOW_A_THRESHOLD')
    if liquidity_score < 3:
        failed_a.append('LIQUIDITY_SCORE_BELOW_A_THRESHOLD')
    if sector_score < MIN_SECTOR_SYMPATHY_SCORE:
        failed_a.append('SECTOR_SCORE_BELOW_A_THRESHOLD')
    if confirm_score < 3:
        failed_a.append('CONFIRM_SCORE_BELOW_A_THRESHOLD')
    if vwap_score < 3:
        failed_a.append('VWAP_SCORE_BELOW_A_THRESHOLD')
    if premarket_gap_pct < a_gap_threshold:
        failed_a.append('PREMARKET_GAP_BELOW_A_THRESHOLD')
    if premarket_notional < a_notional_threshold:
        failed_a.append('PREMARKET_DOLLAR_VOLUME_BELOW_A_THRESHOLD')

    failed_watch = []
    if total < watch_total_threshold:
        failed_watch.append('TOTAL_SCORE_BELOW_WATCH_THRESHOLD')
    if catalyst_score < watch_catalyst_threshold:
        failed_watch.append('CATALYST_SCORE_BELOW_WATCH_THRESHOLD')

    setup_grade = classify_setup_grade(total, catalyst_score, liquidity_score, sector_score, confirm_score, vwap_score, pullback_score, premarket_gap_pct, premarket_notional)
    setup_grade_reason = "Setup passed A/A+ scoring thresholds."
    if setup_grade == 'NO TRADE':
        setup_grade_reason = "Total score and component thresholds did not qualify for WATCH/A grades."
    elif setup_grade == 'WATCH':
        setup_grade_reason = "Setup met baseline quality checks but did not meet A/A+ execution thresholds."

    nearest_grade = 'A+'
    nearest_failed = failed_a_plus
    if setup_grade != 'A+':
        nearest_grade = 'A'
        nearest_failed = failed_a
    if setup_grade not in {'A+', 'A'}:
        nearest_grade = 'WATCH'
        nearest_failed = failed_watch

    threshold_comparisons = {
        'total_score': total,
        'required_total_score_a_plus': A_PLUS_SCORE,
        'required_total_score_a': A_SCORE,
        'required_total_score_watch': watch_total_threshold,
        'catalyst_score': catalyst_score,
        'required_catalyst_score_a_plus': 5,
        'required_catalyst_score_a': 4,
        'required_catalyst_score_watch': watch_catalyst_threshold,
        'liquidity_score': liquidity_score,
        'required_liquidity_score_a_plus': 4,
        'required_liquidity_score_a': 3,
        'sector_score': sector_score,
        'required_sector_score_a_plus': 4,
        'required_sector_score_a': MIN_SECTOR_SYMPATHY_SCORE,
        'confirm_score': confirm_score,
        'required_confirm_score_a_plus': 4,
        'required_confirm_score_a': 3,
        'vwap_score': vwap_score,
        'required_vwap_score_a_plus': 4,
        'required_vwap_score_a': 3,
        'pullback_score': pullback_score,
        'required_pullback_score_a_plus': 4,
        'premarket_gap_pct': round(premarket_gap_pct, 3),
        'required_premarket_gap_pct_a_plus': a_plus_gap_threshold,
        'required_premarket_gap_pct_a': a_gap_threshold,
        'premarket_dollar_volume': round(premarket_notional, 2),
        'required_premarket_dollar_volume_a_plus': round(a_plus_notional_threshold, 2),
        'required_premarket_dollar_volume_a': round(a_notional_threshold, 2),
    }

    return {
        'setup_grade': setup_grade,
        'setup_grade_reason': setup_grade_reason,
        'threshold_comparisons': threshold_comparisons,
        'failed_a_plus_requirements': failed_a_plus,
        'failed_a_requirements': failed_a,
        'failed_watch_requirements': failed_watch,
        'nearest_grade': nearest_grade,
        'nearest_grade_failed_requirements': nearest_failed,
    }

def required_premarket_volume_for_gap(premarket_gap_pct: float) -> float:
    return HIGH_GAP_MIN_PREMARKET_DOLLAR_VOL if premarket_gap_pct >= HIGH_GAP_THRESHOLD_PCT else MIN_PREMARKET_DOLLAR_VOL


def choose_sector_etf(profile: Dict[str, Any], symbol: str) -> str:
    text = ' '.join(str(profile.get(k, '')).lower() for k in ('finnhubIndustry', 'industry', 'name'))
    if any(k in symbol.upper() for k in ('ARM', 'NVDA', 'AMD', 'AVGO', 'MU', 'INTC')) or 'semiconductor' in text or 'chip' in text:
        return 'SMH'
    for key, etf in SECTOR_ETF_MAP.items():
        if key in text:
            return etf
    return 'SPY'


def score_sector_sympathy(symbol: str, symbol_change_pct: float, sector_symbol: str, sector_change_pct: float, catalyst_meta: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    edge = symbol_change_pct - sector_change_pct
    bullish = catalyst_meta.get('direction') not in {'bearish', 'mixed'}
    is_leader = symbol_change_pct >= (sector_change_pct * RS_SECTOR_MULT) if sector_change_pct > 0 else symbol_change_pct > 1.0
    score = 1
    if bullish and sector_change_pct > 0 and edge >= 4 and is_leader:
        score = 5
    elif bullish and sector_change_pct >= -0.2 and edge >= 2.5 and is_leader:
        score = 4
    elif edge >= 1.0:
        score = 3
    elif edge >= 0:
        score = 2
    return score, {
        'sector_symbol': sector_symbol,
        'sector_change_pct': round(sector_change_pct, 2),
        'edge_vs_sector_pct': round(edge, 2),
        'is_leader_vs_sector': is_leader,
    }


def score_daily_alignment(current_price: float, daily_bars: List[Dict[str, Any]]) -> Tuple[int, Dict[str, Any]]:
    highs20 = [safe_num(b.get('h')) for b in daily_bars[-20:]]
    closes200 = [safe_num(b.get('c')) for b in daily_bars[-200:]]
    highs60 = [safe_num(b.get('h')) for b in daily_bars[-60:]]
    ma200 = mean(closes200) if closes200 else current_price
    breakout_20 = max(highs20) if highs20 else current_price
    breakout_60 = max(highs60) if highs60 else current_price
    blue_sky = current_price >= breakout_20 * 0.995

    score = 1
    if blue_sky and current_price >= ma200 and current_price >= breakout_60 * 0.98:
        score = 5
    elif current_price >= ma200 and current_price >= breakout_20 * 0.985:
        score = 4
    elif current_price >= ma200:
        score = 3
    elif current_price >= ma200 * 0.97:
        score = 2
    return score, {'ma200': round(ma200, 2), 'breakout_20': round(breakout_20, 2), 'breakout_60': round(breakout_60, 2), 'blue_sky_proxy': blue_sky}


def _opening_range_expected_bar_count() -> int:
    start_h, start_m = parse_hhmm(OPENING_RANGE_START_ET)
    end_h, end_m = parse_hhmm(OPENING_RANGE_END_ET)
    start_total = start_h * 60 + start_m
    end_total = end_h * 60 + end_m
    return max(1, end_total - start_total)


def get_opening_range_stats(minute_bars: List[Dict[str, Any]]) -> Dict[str, Any]:
    now_dt = now_et()
    session_bars = filter_bars_for_today_session(minute_bars)
    or_bars = filter_bars_in_et_window(session_bars, OPENING_RANGE_START_ET, OPENING_RANGE_END_ET)
    now_bar_count = len(session_bars)
    def _dt(bar):
        return bar_dt_et(bar)
    latest_bar_dt = _dt(session_bars[-1]) if session_bars else (_dt(minute_bars[-1]) if minute_bars else None)
    earliest_dt = _dt(session_bars[0]) if session_bars else None
    or_end_h, or_end_m = [int(x) for x in OPENING_RANGE_END_ET.split(':', 1)]
    expected_or_bars = _opening_range_expected_bar_count()
    if not minute_bars:
        reason = 'NO_INTRADAY_BARS'
    elif not session_bars:
        reason = 'NO_TODAY_SESSION_BARS'
    elif not or_bars and latest_bar_dt and (latest_bar_dt.hour > or_end_h or (latest_bar_dt.hour == or_end_h and latest_bar_dt.minute >= or_end_m)):
        reason = 'NO_OPENING_RANGE_BARS'
    elif not or_bars:
        reason = 'LATEST_BAR_BEFORE_OR_END'
    else:
        reason = 'COMPLETE'
    if not session_bars:
        return {
            'session_bars': 0,
            'or_complete': False,
            'or_high': None,
            'or_low': None,
            'or_open': None,
            'or_close': None,
            'or_mid': None,
            'or_range': None,
            'current_price': None,
            'breakout_price': None,
            'breakout_confirmed': False,
            'bars_above_breakout': 0,
            'scanner_now_et': now_dt.isoformat(), 'intraday_bar_count': len(minute_bars), 'today_session_bar_count': 0, 'opening_range_bar_count': 0,
            'latest_bar_timestamp_et': latest_bar_dt.isoformat() if latest_bar_dt else None, 'earliest_today_bar_timestamp_et': None,
            'opening_range_start_et': OPENING_RANGE_START_ET, 'opening_range_end_et': OPENING_RANGE_END_ET,
            'opening_range_complete': False, 'opening_range_complete_reason': reason,
            'expected_opening_range_bar_count': expected_or_bars,
            'opening_range_bar_coverage_pct': 0.0,
            'opening_range_latest_bar_after_end': bool(latest_bar_dt and (latest_bar_dt.hour > or_end_h or (latest_bar_dt.hour == or_end_h and latest_bar_dt.minute >= or_end_m))),
            'breakout_confirmed_reason': 'OPENING_RANGE_NOT_COMPLETE',
        }

    if or_bars:
        or_high = max(safe_num(b.get('h')) for b in or_bars)
        or_low = min(safe_num(b.get('l')) for b in or_bars)
        or_open = safe_num(or_bars[0].get('o'))
        or_close = safe_num(or_bars[-1].get('c'))
        current_price = safe_num(session_bars[-1].get('c'))
        or_range = max(0.01, or_high - or_low)
        breakout_price = round(or_high * (1 + OR_BREAKOUT_BUFFER_PCT), 2)
        recent = session_bars[-3:]
        bars_above_breakout = sum(1 for b in recent if safe_num(b.get('c')) >= breakout_price)
        latest_bar_after_or_end = bool(latest_bar_dt and (latest_bar_dt.hour > or_end_h or (latest_bar_dt.hour == or_end_h and latest_bar_dt.minute >= or_end_m)))
        min_or_bars_with_tolerance = max(1, expected_or_bars - 1)
        complete = bool(buy_window_open() and latest_bar_after_or_end and len(or_bars) >= min_or_bars_with_tolerance)
        if not latest_bar_after_or_end:
            complete_reason = 'LATEST_BAR_BEFORE_OR_END'
        elif len(or_bars) == 0:
            complete_reason = 'NO_OPENING_RANGE_BARS'
        elif len(or_bars) >= expected_or_bars:
            complete_reason = 'COMPLETE'
        elif len(or_bars) >= min_or_bars_with_tolerance:
            complete_reason = 'COMPLETE_WITH_MINOR_BAR_GAP'
        else:
            complete_reason = 'MISSING_TOO_MANY_OR_BARS'
        breakout_confirmed = complete and bars_above_breakout >= 2 and current_price >= breakout_price
        return {
            'session_bars': now_bar_count,
            'or_complete': complete,
            'or_high': round(or_high, 2),
            'or_low': round(or_low, 2),
            'or_open': round(or_open, 2),
            'or_close': round(or_close, 2),
            'or_mid': round((or_high + or_low) / 2, 2),
            'or_range': round(or_range, 2),
            'current_price': round(current_price, 2),
            'breakout_price': breakout_price,
            'breakout_confirmed': breakout_confirmed,
            'bars_above_breakout': bars_above_breakout,
            'scanner_now_et': now_dt.isoformat(), 'intraday_bar_count': len(minute_bars), 'today_session_bar_count': len(session_bars), 'opening_range_bar_count': len(or_bars),
            'latest_bar_timestamp_et': latest_bar_dt.isoformat() if latest_bar_dt else None, 'earliest_today_bar_timestamp_et': earliest_dt.isoformat() if earliest_dt else None,
            'opening_range_start_et': OPENING_RANGE_START_ET, 'opening_range_end_et': OPENING_RANGE_END_ET,
            'opening_range_complete': complete, 'opening_range_complete_reason': complete_reason,
            'breakout_threshold_price': breakout_price,
            'expected_opening_range_bar_count': expected_or_bars,
            'opening_range_bar_coverage_pct': round((len(or_bars) / expected_or_bars) * 100, 2) if expected_or_bars else 0.0,
            'opening_range_latest_bar_after_end': latest_bar_after_or_end,
            'breakout_confirmed_reason': 'BREAKOUT_CONFIRMED' if breakout_confirmed else ('OPENING_RANGE_NOT_COMPLETE' if not complete else 'BREAKOUT_NOT_CONFIRMED'),
        }

    current_price = safe_num(session_bars[-1].get('c'))
    return {
        'session_bars': now_bar_count,
        'or_complete': False,
        'or_high': None,
        'or_low': None,
        'or_open': None,
        'or_close': None,
        'or_mid': None,
        'or_range': None,
        'current_price': round(current_price, 2),
        'breakout_price': None,
        'breakout_confirmed': False,
        'bars_above_breakout': 0,
        'scanner_now_et': now_dt.isoformat(), 'intraday_bar_count': len(minute_bars), 'today_session_bar_count': len(session_bars), 'opening_range_bar_count': 0,
        'latest_bar_timestamp_et': latest_bar_dt.isoformat() if latest_bar_dt else None, 'earliest_today_bar_timestamp_et': earliest_dt.isoformat() if earliest_dt else None,
        'opening_range_start_et': OPENING_RANGE_START_ET, 'opening_range_end_et': OPENING_RANGE_END_ET,
        'opening_range_complete': False, 'opening_range_complete_reason': reason,
        'expected_opening_range_bar_count': expected_or_bars,
        'opening_range_bar_coverage_pct': 0.0,
        'opening_range_latest_bar_after_end': bool(latest_bar_dt and (latest_bar_dt.hour > or_end_h or (latest_bar_dt.hour == or_end_h and latest_bar_dt.minute >= or_end_m))),
        'breakout_confirmed_reason': 'OPENING_RANGE_NOT_COMPLETE',
    }


def score_relative_strength_open(symbol_minute_bars: List[Dict[str, Any]], spy_minute_bars: List[Dict[str, Any]]) -> Tuple[int, Dict[str, Any]]:
    sym = filter_bars_for_today_session(symbol_minute_bars)
    spy = filter_bars_for_today_session(spy_minute_bars)
    if not sym or not spy:
        return 1, {'reason': 'Not enough opening session bars.'}
    sym_open = safe_num(sym[0].get('o')) or safe_num(sym[0].get('c'))
    sym_curr = safe_num(sym[-1].get('c'))
    spy_open = safe_num(spy[0].get('o')) or safe_num(spy[0].get('c'))
    spy_curr = safe_num(spy[-1].get('c'))
    sym_change = ((sym_curr - sym_open) / sym_open * 100.0) if sym_open else 0.0
    spy_change = ((spy_curr - spy_open) / spy_open * 100.0) if spy_open else 0.0
    edge = sym_change - spy_change
    score = 1
    if edge >= 3 and sym_change > 0:
        score = 5
    elif edge >= 2:
        score = 4
    elif edge >= 1:
        score = 3
    elif edge >= 0:
        score = 2
    return score, {
        'open_to_now_change_pct': round(sym_change, 2),
        'spy_open_to_now_change_pct': round(spy_change, 2),
        'edge': round(edge, 2),
    }

def detect_heavy_red_candle_trap(minute_bars: List[Dict[str, Any]]) -> Dict[str, Any]:
    morning = filter_bars_in_et_window(filter_bars_for_today_session(minute_bars), OPENING_RANGE_START_ET, OPENING_RANGE_END_ET)
    if len(morning) < 2:
        return {'triggered': False, 'reason': 'Not enough opening bars to evaluate red-candle trap.'}
    green_vols = [safe_num(b.get('v')) for b in morning if safe_num(b.get('c')) > safe_num(b.get('o'))]
    if not green_vols:
        return {'triggered': False, 'reason': 'No green candles in opening range to compare against.'}
    max_green_vol = max(green_vols)
    heavy_red = []
    for idx, bar in enumerate(morning):
        open_px = safe_num(bar.get('o'))
        close_px = safe_num(bar.get('c'))
        vol = safe_num(bar.get('v'))
        if close_px < open_px and vol > max_green_vol:
            heavy_red.append((idx, bar, vol))
    if not heavy_red:
        return {
            'triggered': False,
            'max_green_volume': round(max_green_vol, 2),
            'reason': 'No heavy red candle exceeded the strongest green volume.',
        }
    first_idx, first_bar, first_vol = heavy_red[0]
    return {
        'triggered': True,
        'first_red_index': first_idx,
        'first_red_open': round(safe_num(first_bar.get('o')), 4),
        'first_red_close': round(safe_num(first_bar.get('c')), 4),
        'first_red_volume': round(first_vol, 2),
        'max_green_volume': round(max_green_vol, 2),
        'reason': 'Opening red candle volume exceeded all green candles in the opening range.',
    }



def get_market_internals_bias(feed: str = 'iex') -> Dict[str, Any]:
    meta = {
        'enabled': MARKET_INTERNALS_BLOCK_ENABLED,
        'tick_symbol': MARKET_INTERNALS_TICK_SYMBOL,
        'add_symbol': MARKET_INTERNALS_ADD_SYMBOL,
        'tick_persistently_negative': False,
        'add_dropping': False,
        'longs_blocked': False,
        'reason': '',
    }
    if not MARKET_INTERNALS_BLOCK_ENABLED:
        meta['reason'] = 'Market internals block disabled.'
        return meta
    end = now_utc()
    start = end - timedelta(minutes=30)
    try:
        bars = get_bars([MARKET_INTERNALS_TICK_SYMBOL, MARKET_INTERNALS_ADD_SYMBOL], '1Min', start, end, 60, feed=feed)
    except Exception as exc:
        meta['reason'] = f'Could not fetch internals: {exc}'
        return meta
    tick_series = [safe_num(b.get('c')) for b in bars.get(MARKET_INTERNALS_TICK_SYMBOL, []) if safe_num(b.get('c')) != 0]
    add_series = [safe_num(b.get('c')) for b in bars.get(MARKET_INTERNALS_ADD_SYMBOL, []) if safe_num(b.get('c')) != 0]
    if len(tick_series) >= 5:
        last5 = tick_series[-5:]
        meta['tick_persistently_negative'] = all(v < 0 for v in last5)
        meta['tick_last'] = round(last5[-1], 2)
    if len(add_series) >= 5:
        recent = add_series[-5:]
        meta['add_dropping'] = (recent[-1] < recent[0]) and all(recent[i] <= recent[i - 1] for i in range(1, len(recent)))
        meta['add_last'] = round(recent[-1], 2)
    meta['longs_blocked'] = bool(meta['tick_persistently_negative'] and meta['add_dropping'])
    if meta['longs_blocked']:
        meta['reason'] = 'Blocked: $TICK is persistently below 0 while $ADD is falling.'
    else:
        meta['reason'] = 'Breadth filter is not blocking longs.'
    return meta




def score_vwap_hold_reclaim(minute_bars: List[Dict[str, Any]]) -> Tuple[int, Dict[str, Any]]:
    session = filter_bars_for_today_session(minute_bars)
    if len(session) < 8:
        return 1, {'reason': 'Not enough session bars for VWAP check.'}
    vwap = calc_vwap(session)
    closes = [safe_num(b.get('c')) for b in session]
    last5 = closes[-5:]
    holds = sum(1 for c in last5 if c >= vwap)
    dipped_below = any(c < vwap * 0.998 for c in closes[:-3])
    reclaimed = all(c >= vwap * 0.999 for c in closes[-3:])
    recent_vol = [safe_num(b.get('v')) for b in session[-5:]]
    prior_vol = [safe_num(b.get('v')) for b in session[-12:-5]]
    drying = bool(prior_vol) and mean(recent_vol) <= mean(prior_vol) * 1.1
    score = 1
    if holds >= 4 and reclaimed and drying:
        score = 5
    elif holds >= 4 and reclaimed:
        score = 4
    elif holds >= 3:
        score = 3
    elif closes[-1] >= vwap * 0.997:
        score = 2
    return score, {
        'vwap': round(vwap, 2),
        'holds_last5': holds,
        'dipped_below_vwap': dipped_below,
        'reclaimed_vwap': reclaimed,
        'drying_volume': drying,
    }


def score_first_pullback_quality(minute_bars: List[Dict[str, Any]], or_stats: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    session = filter_bars_for_today_session(minute_bars)
    if len(session) < 10 or not or_stats.get('or_high'):
        return 1, {'reason': 'Not enough data for first pullback score.'}
    breakout_price = safe_num(or_stats.get('breakout_price') or or_stats.get('or_high'))
    vwap = calc_vwap(session)
    breakout_index = None
    for idx, bar in enumerate(session):
        if safe_num(bar.get('h')) >= breakout_price:
            breakout_index = idx
            break
    recent_slice = session[breakout_index:] if breakout_index is not None else session[-10:]
    high_after_break = max(safe_num(b.get('h')) for b in recent_slice)
    low_after_break = min(safe_num(b.get('l')) for b in recent_slice[-8:])
    pullback = max(0.0, high_after_break - low_after_break)
    or_range = max(0.01, safe_num(or_stats.get('or_range'), 0.01))
    retrace_pct = pullback / or_range
    low_holds_vwap = low_after_break >= vwap * 0.995
    vol_recent = [safe_num(b.get('v')) for b in recent_slice[-4:]]
    vol_prior = [safe_num(b.get('v')) for b in recent_slice[-8:-4]]
    drying = bool(vol_prior) and mean(vol_recent) <= mean(vol_prior) * 0.95
    score = 1
    if retrace_pct <= PULLBACK_MAX_RETRACE_PCT and low_holds_vwap and drying:
        score = 5
    elif retrace_pct <= 0.55 and low_holds_vwap:
        score = 4
    elif retrace_pct <= 0.7:
        score = 3
    elif low_holds_vwap:
        score = 2
    return score, {
        'high_after_breakout': round(high_after_break, 2),
        'low_after_breakout': round(low_after_break, 2),
        'pullback_retrace_pct_of_or': round(retrace_pct, 2),
        'low_holds_vwap': low_holds_vwap,
        'drying_volume': drying,
    }


def score_entry_quality(current_price: float, daily_bars: List[Dict[str, Any]], minute_bars: List[Dict[str, Any]], or_stats: Dict[str, Any], vwap_meta: Dict[str, Any], pullback_meta: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    recent_high = max([safe_num(b.get('h')) for b in daily_bars[-10:]] or [current_price])
    recent_low = min([safe_num(b.get('l')) for b in daily_bars[-10:]] or [current_price])
    atr = calc_atr(daily_bars)
    session = filter_bars_for_today_session(minute_bars)
    minute_highs = [safe_num(b.get('h')) for b in session[-15:]] or [current_price]
    minute_lows = [safe_num(b.get('l')) for b in session[-15:]] or [current_price]
    coil_high = max(minute_highs)
    coil_low = min(minute_lows)
    or_breakout = safe_num(or_stats.get('breakout_price')) or max(recent_high, coil_high)
    entry = max(recent_high, coil_high, or_breakout) + max(0.02, atr * 0.03)
    stop = max(recent_low, entry - max(0.05, atr * ATR_STOP_MULT))
    risk = max(0.01, entry - stop)
    target1 = entry + risk * 3
    target2 = entry + risk * 4
    rr2 = (target2 - entry) / risk if risk > 0 else 0.0
    distance = abs(current_price - entry) / entry if entry > 0 else 9.99
    contraction = (coil_high - coil_low) <= max(0.25, atr * 0.8)
    extended = current_price > entry * (1 + MAX_ENTRY_EXTENSION_PCT)
    breakout_confirmed = bool(or_stats.get('breakout_confirmed'))
    reclaim_ok = bool(vwap_meta.get('reclaimed_vwap'))
    pullback_ok = bool(pullback_meta.get('low_holds_vwap'))

    score = 1
    if rr2 >= 3 and distance <= 0.0075 and breakout_confirmed and reclaim_ok and pullback_ok and not extended:
        score = 5
    elif rr2 >= 3 and breakout_confirmed and reclaim_ok and not extended:
        score = 4
    elif rr2 >= 2.5 and reclaim_ok:
        score = 3
    elif rr2 >= 2:
        score = 2

    return score, {
        'entry_price': round(entry, 2),
        'stop_price': round(stop, 2),
        'target_1': round(target1, 2),
        'target_2': round(target2, 2),
        'risk_per_share': round(risk, 2),
        'rr_ratio_1': round((target1 - entry) / risk if risk > 0 else 0.0, 2),
        'rr_ratio_2': round(rr2, 2),
        'contraction_proxy': contraction,
        'extended': extended,
        'distance_from_entry_pct': round(distance * 100, 2),
        'breakout_confirmed': breakout_confirmed,
    }


def score_opening_range_confirmation(current_price: float, or_stats: Dict[str, Any], vwap_meta: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    if not or_stats.get('or_high'):
        return 1, {'reason': 'Opening range not formed yet.'}
    breakout_confirmed = bool(or_stats.get('breakout_confirmed'))
    above_mid = current_price >= safe_num(or_stats.get('or_mid'))
    above_breakout = current_price >= safe_num(or_stats.get('breakout_price'))
    holds_vwap = bool(vwap_meta.get('holds_last5', 0) >= 3)
    score = 1
    if breakout_confirmed and holds_vwap:
        score = 5
    elif above_breakout and holds_vwap:
        score = 4
    elif above_mid:
        score = 3
    elif current_price >= safe_num(or_stats.get('or_low')):
        score = 2
    return score, {
        'breakout_confirmed': breakout_confirmed,
        'above_breakout': above_breakout,
        'above_mid': above_mid,
        'bars_above_breakout': or_stats.get('bars_above_breakout', 0),
    }



def calculate_rvol(minute_bars: List[Dict[str, Any]], lookback_days: int = 3) -> float:
    if not minute_bars:
        return 0.0
    session = filter_bars_for_today_session(minute_bars)
    current_volume = sum(safe_num(b.get('v')) for b in session)
    if current_volume <= 0:
        return 0.0
    latest_dt = bar_dt_et(minute_bars[-1])
    if not latest_dt:
        return 0.0
    cutoff = latest_dt.hour * 60 + latest_dt.minute
    volumes_by_day: Dict[Any, float] = {}
    for b in minute_bars:
        dt = bar_dt_et(b)
        if not dt or dt.date() == latest_dt.date():
            continue
        mins = dt.hour * 60 + dt.minute
        if 9 * 60 + 30 <= mins <= cutoff:
            volumes_by_day[dt.date()] = volumes_by_day.get(dt.date(), 0.0) + safe_num(b.get('v'))
    hist = list(volumes_by_day.values())[-lookback_days:]
    avg = mean(hist) if hist else 0.0
    return (current_volume / avg) if avg > 0 else 0.0


def calculate_trend_efficiency(minute_bars: List[Dict[str, Any]], window: int = 30) -> float:
    session = filter_bars_for_today_session(minute_bars)
    closes = [safe_num(b.get('c')) for b in session[-window:] if safe_num(b.get('c')) > 0]
    if len(closes) < 3:
        return 0.0
    net_move = abs(closes[-1] - closes[0])
    path = sum(abs(closes[i] - closes[i - 1]) for i in range(1, len(closes)))
    return (net_move / path) if path > 0 else 0.0


def calculate_halt_risk_probability(minute_bars: List[Dict[str, Any]], bars: int = 5) -> Dict[str, Any]:
    session = filter_bars_for_today_session(minute_bars)
    recent = session[-bars:]
    if not recent:
        return {'halt_risk': 'unknown', 'max_1m_range_pct': 0.0}
    max_range = 0.0
    for b in recent:
        h = safe_num(b.get('h'))
        l = safe_num(b.get('l'))
        if l > 0 and h >= l:
            max_range = max(max_range, (h - l) / l * 100.0)
    risk = 'high' if max_range > 8 else 'normal'
    return {'halt_risk': risk, 'max_1m_range_pct': round(max_range, 2)}


def build_model_scores(price_change_pct: float, rvol: float, float_shares: float, catalyst_weight: int, spread_pct: float, trend_efficiency: float, current_price: float, vwap: float, now_label: str) -> Dict[str, int]:
    gap_component = 100 if 8 <= price_change_pct <= 20 else (70 if price_change_pct > 5 else 40)
    rvol_component = min(100, int((rvol / max(0.1, MIN_RVOL)) * 100))
    float_component = 100 if 0 < float_shares <= 20_000_000 else (55 if float_shares <= MAX_FLOAT else 20)
    catalyst_component = catalyst_weight * 20
    opportunity = int(0.25 * catalyst_component + 0.20 * rvol_component + 0.15 * float_component + 0.15 * gap_component + 0.25 * 80)

    spread_component = 100 if spread_pct <= 0.003 else (70 if spread_pct <= 0.01 else 35)
    trend_component = min(100, int(trend_efficiency * 100))
    tradability = int(0.5 * spread_component + 0.5 * trend_component)

    extension = ((current_price - vwap) / vwap * 100.0) if vwap > 0 else 0.0
    extension_component = 100 if extension <= 1.5 else (70 if extension <= 3 else 30)
    tod_component = 95 if '09:' in now_label or '10:' in now_label else (45 if '12:' in now_label or '13:' in now_label else 70)
    entry_quality = int(0.6 * extension_component + 0.4 * tod_component)

    return {
        'opportunity': max(1, min(100, opportunity)),
        'tradability': max(1, min(100, tradability)),
        'entry_quality': max(1, min(100, entry_quality)),
    }


def get_trade_decision(model_scores: Dict[str, int], time_et: datetime, relative_strength_vs_spy: float) -> str:
    minutes = time_et.hour * 60 + time_et.minute
    if 9 * 60 + 30 <= minutes <= 10 * 60 + 30:
        if model_scores['opportunity'] > 80 and model_scores['tradability'] > 60:
            return 'BUY NOW'
    elif 11 * 60 <= minutes <= 14 * 60:
        if model_scores['opportunity'] > 95 and model_scores['entry_quality'] > 90:
            return 'BUY NOW'
        return 'WATCH FOR BREAKOUT'
    elif 15 * 60 <= minutes <= 16 * 60:
        if relative_strength_vs_spy > 2.0 and model_scores['tradability'] > 55:
            return 'BUY NOW'
    return 'WATCH FOR BREAKOUT'


def calculate_position_size(
    entry_price: float,
    stop_price: float,
    target_price: float,
    p_success: float,
    vix_spike_active: bool,
) -> Dict[str, Any]:
    """Calculates position size using Fractional Kelly driven by ML win probability."""

    risk_per_share = max(0.01, entry_price - stop_price)
    reward_per_share = max(0.01, target_price - entry_price)
    reward_to_risk = reward_per_share / risk_per_share

    # 1. Full Kelly Formula: f = (P * R - (1 - P)) / R
    kelly_full = (p_success * reward_to_risk - (1.0 - p_success)) / reward_to_risk

    # If Kelly is <= 0, the mathematical expectancy is negative. Skip trade.
    if kelly_full <= 0:
        return {
            'qty': 0, 'capital_qty': 0, 'risk_qty': 0,
            'max_dollar_loss': 0.0, 'buying_power_used': 0.0,
            'dynamic_risk_limit': 0.0, 'kelly_fraction_used': 0.0,
            'reason': 'Negative mathematical expectancy.',
        }

    # 2. Fractional Kelly & Volatility Brakes
    current_k_fraction = KELLY_FRACTION
    if vix_spike_active:
        current_k_fraction *= VIX_PENALTY_MULTIPLIER  # Cut risk in volatile regimes

    # Calculate optimal risk percentage, capped by max portfolio heat
    fractional_kelly_pct = min(current_k_fraction * kelly_full, MAX_PORTFOLIO_HEAT)
    dynamic_dollar_risk = CURRENT_BANKROLL * fractional_kelly_pct

    # 3. Share Quantity Calculation
    capital_qty = int(DEFAULT_RISK_CAPITAL // max(0.01, entry_price))
    risk_qty = int(dynamic_dollar_risk // risk_per_share)

    # We take the minimum of constraints to ensure capital limits are respected
    qty = max(0, min(MAX_BUY_SHARES, capital_qty, risk_qty))

    return {
        'qty': qty,
        'capital_qty': capital_qty,
        'risk_qty': risk_qty,
        'max_dollar_loss': round(qty * risk_per_share, 2),
        'buying_power_used': round(qty * entry_price, 2),
        'dynamic_risk_limit': round(dynamic_dollar_risk, 2),
        'kelly_fraction_used': round(fractional_kelly_pct, 4),
    }




def _truncate_headline(text: Any, max_len: int = 140) -> str:
    head = str(text or '').strip()
    if len(head) <= max_len:
        return head
    return head[: max_len - 1].rstrip() + '…'


def _extract_news_headline(item: Any) -> str:
    if isinstance(item, dict):
        value = item.get('headline') or item.get('summary') or ''
        return str(value).strip()
    if isinstance(item, str):
        return item.strip()
    return ''


def _extract_news_timestamp(item: Any) -> Optional[datetime]:
    if not isinstance(item, dict):
        return None
    for field in ('datetime', 'time'):
        value = item.get(field)
        if isinstance(value, (int, float)) and value > 0:
            try:
                return datetime.fromtimestamp(float(value), tz=timezone.utc)
            except Exception:
                pass
    published_at = item.get('published_at')
    if isinstance(published_at, str) and published_at.strip():
        try:
            return datetime.fromisoformat(published_at.replace('Z', '+00:00')).astimezone(timezone.utc)
        except Exception:
            return None
    return None


def _extract_news_keywords(headlines: List[str]) -> Dict[str, List[str]]:
    positive = ['fda', 'approval', 'contract', 'partnership', 'merger', 'acquisition', 'earnings', 'guidance', 'revenue', 'buyout', 'patent', 'trial', 'phase', 'launch', 'order', 'ai', 'offering closed', 'debt reduction', 'upgrade']
    negative = ['offering', 'dilution', 'bankruptcy', 'delisting', 'investigation', 'downgrade', 'reverse split', 'layoffs', 'subpoena']
    joined = " ".join([str(h or '').lower() for h in headlines if str(h or '').strip()])
    pos_hits = [term for term in positive if term in joined]
    neg_hits = [term for term in negative if term in joined]
    return {
        'keywords_hit': sorted(set(pos_hits + neg_hits)),
        'positive_terms': sorted(set(pos_hits)),
        'negative_terms': sorted(set(neg_hits)),
    }




def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(value)))


def _apply_news_evidence_fallback_features(ml_features: Dict[str, Any], news_catalyst: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    news = news_catalyst or {}
    headline_count = int(ml_features.get('headline_count', 0) or 0)
    qualifies = bool(ml_features.get('qualifies_as_news_catalyst'))
    positive_terms = ml_features.get('positive_terms') or []
    negative_terms = ml_features.get('negative_terms') or []
    if not isinstance(positive_terms, list):
        positive_terms = []
    if not isinstance(negative_terms, list):
        negative_terms = []

    prev_p = float(ml_features.get('p_success', 0.5) or 0.5)
    prev_k = float(ml_features.get('keyword_boost', 0.0) or 0.0)
    adjusted = False
    source = 'feature_store'
    reason = 'FEATURE_STORE_VALUES_USED'

    if qualifies and headline_count > 0:
        pos_count = len(positive_terms)
        neg_count = len(negative_terms)
        fallback_keyword_boost = _clamp((pos_count * 0.08) - (neg_count * 0.12), -0.25, 0.30)
        applied_k = prev_k
        applied_p = prev_p
        if abs(prev_k) < 1e-9:
            applied_k = fallback_keyword_boost
            adjusted = True
            reason = 'KEYWORD_BOOST_DERIVED_FROM_NEWS_TERMS'
        if abs(prev_p - 0.5) < 1e-9:
            applied_p = _clamp(0.50 + applied_k, 0.25, 0.80)
            adjusted = True
            reason = 'P_SUCCESS_DERIVED_FROM_NEWS_EVIDENCE' if applied_k != prev_k else 'P_SUCCESS_ADJUSTED_WITH_EXISTING_KEYWORD_BOOST'

        ml_features['keyword_boost'] = float(applied_k)
        ml_features['p_success'] = float(applied_p)
        ml_features['catalyst_negative_terms'] = negative_terms
        ml_features['catalyst_negative_risk'] = bool(neg_count > 0)
        if neg_count > 0:
            ml_features['catalyst_negative_risk_reason'] = 'NEGATIVE_CATALYST_TERMS_DETECTED'
            if pos_count == 0:
                reason = 'NEGATIVE_TERMS_ONLY_CAPPED_CATALYST_UPLIFT'
        source = 'news_evidence_fallback' if adjusted and (abs(prev_k) < 1e-9 and abs(prev_p - 0.5) < 1e-9) else ('feature_store_plus_news_evidence' if adjusted else 'feature_store')
    ml_features['catalyst_score_input_source'] = source
    ml_features['catalyst_score_adjusted_from_news'] = bool(adjusted)
    ml_features['catalyst_score_adjustment_reason'] = reason
    ml_features['catalyst_score_before_news_adjustment'] = max(1, min(5, int(round(prev_p * 5))))
    return ml_features
def _build_catalyst_diagnostics(ml_features: Dict[str, Any], catalyst_meta: Dict[str, Any]) -> Dict[str, Any]:
    headline_count = int(ml_features.get('headline_count', 0) or 0)
    recent_count = int(ml_features.get('recent_headline_count', headline_count) or 0)
    latest_age = ml_features.get('latest_headline_age_minutes')
    latest_age = float(latest_age) if isinstance(latest_age, (int, float)) else None
    keywords = ml_features.get('keywords_hit') or []
    if not isinstance(keywords, list):
        keywords = []
    positive_terms = ml_features.get('positive_terms') or []
    negative_terms = ml_features.get('negative_terms') or []
    samples = ml_features.get('headline_samples') or ml_features.get('headlines') or []
    if not isinstance(samples, list):
        samples = []
    samples = [_truncate_headline(h) for h in samples[:3] if str(h or '').strip()]

    feature_store_hit = bool(ml_features)
    missing_reason = 'UNKNOWN'
    if not feature_store_hit:
        missing_reason = 'SOURCE_UNAVAILABLE'
    elif all(k not in ml_features for k in ('headline_count', 'recent_headline_count', 'latest_headline_age_minutes', 'keywords_hit')):
        missing_reason = 'FEATURE_STORE_MISSING_FIELDS_WITH_NEWS_FALLBACK' if catalyst_meta.get('news_lookup_status') in {'FOUND'} else 'FEATURE_STORE_MISSING_FIELDS'
    elif headline_count <= 0 and float(ml_features.get('p_success', 0.5) or 0.5) == 0.5:
        missing_reason = 'FEATURE_STORE_BASELINE_ONLY'
    lookup_status = str(ml_features.get('news_lookup_status') or '')
    qualifies = bool(ml_features.get('qualifies_as_news_catalyst'))
    if headline_count <= 0:
        missing_reason = 'NO_NEWS_FOUND'
    elif latest_age is not None and latest_age > 240:
        missing_reason = 'NEWS_TOO_OLD'
    elif not keywords:
        missing_reason = 'NO_KEYWORDS_HIT'
    elif qualifies and keywords:
        missing_reason = 'CATALYST_EVIDENCE_PRESENT'
    elif not qualifies:
        missing_reason = 'NO_KEYWORDS_HIT'

    if bool(ml_features.get('news_api_error')) or lookup_status in {'API_ERROR', 'INVALID_RESPONSE', 'FINNHUB_KEY_MISSING'}:
        missing_reason = 'SOURCE_UNAVAILABLE'
    if bool(ml_features.get('ai_validation_unavailable')):
        missing_reason = 'AI_VALIDATION_UNAVAILABLE'

    return {
        'catalyst_source': catalyst_meta.get('model') or 'unknown',
        'catalyst_feature_store_hit': feature_store_hit,
        'catalyst_feature_store_age_minutes': ml_features.get('feature_store_age_minutes'),
        'catalyst_headline_count': headline_count,
        'catalyst_recent_headline_count': recent_count,
        'catalyst_latest_headline_age_minutes': latest_age,
        'catalyst_keywords_hit': keywords,
        'catalyst_strength_reason': catalyst_meta.get('reason') or 'No catalyst strength reason provided.',
        'catalyst_missing_reason': missing_reason,
        'catalyst_positive_terms': positive_terms if isinstance(positive_terms, list) else [],
        'catalyst_negative_terms': negative_terms if isinstance(negative_terms, list) else [],
        'catalyst_is_fresh': bool(latest_age is not None and latest_age <= 240),
        'catalyst_confidence': catalyst_meta.get('confidence') or 'unavailable',
        'catalyst_headline_samples': samples,
    }


def _baseline_catalyst_reason(catalyst_score: int, cat_diag: Dict[str, Any]) -> Optional[str]:
    if catalyst_score != 2:
        return None
    miss = cat_diag.get('catalyst_missing_reason')
    if miss in {'NO_NEWS_FOUND', 'NEWS_TOO_OLD', 'NO_KEYWORDS_HIT'}:
        return 'BASELINE_ONLY_NO_NEWS' if miss == 'NO_NEWS_FOUND' else 'BASELINE_ONLY_WEAK_NEWS'
    if miss in {'AI_VALIDATION_UNAVAILABLE'}:
        return 'BASELINE_ONLY_NO_AI_CONFIRMATION'
    if miss in {'NEWS_API_ERROR'}:
        return 'SOURCE_UNAVAILABLE'
    if miss in {'FEATURE_STORE_MISSING_FIELDS', 'FEATURE_STORE_BASELINE_ONLY'}:
        return 'FEATURE_STORE_BASELINE_ONLY'
    if cat_diag.get('catalyst_is_fresh'):
        return 'FRESH_BUT_NOT_STRONG'
    return 'UNKNOWN_BASELINE_REASON'


_SOURCE_PRIORITY = {
    'news_catalyst': 0,
    'momentum_breakout': 1,
    'orb_primary': 2,
    'fallback_market_candidates': 3,
    'unknown': 4,
}


def _select_primary_source(sources: List[str]) -> str:
    if not sources:
        return 'unknown'
    deduped = [s for s in dict.fromkeys([str(x or 'unknown') for x in sources]).keys()]
    return sorted(deduped, key=lambda x: _SOURCE_PRIORITY.get(x, 99))[0]

def analyze_symbol(symbol: str, snapshot: Dict[str, Any], quote: Dict[str, Any], daily_bars: List[Dict[str, Any]], minute_bars: List[Dict[str, Any]], spy_change_pct: float, profile: Dict[str, Any], asset: Dict[str, Any], spy_minute_bars: List[Dict[str, Any]], sector_snapshots: Dict[str, Any], market_internals: Dict[str, Any], feed_used: Optional[str] = None, news_catalyst: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    daily_bar = snapshot.get('dailyBar', {})
    prev_daily = snapshot.get('prevDailyBar', {})
    minute_bar = snapshot.get('minuteBar', {})
    ask = safe_num(quote.get('ap'))
    bid = safe_num(quote.get('bp'))
    spread = max(0.0, ask - bid) if ask and bid else 0.0
    current_price = ask or safe_num(minute_bar.get('c')) or safe_num(daily_bar.get('c')) or safe_num(prev_daily.get('c'))
    prev_close = safe_num(prev_daily.get('c')) or safe_num(daily_bar.get('o')) or current_price
    day_volume = safe_num(daily_bar.get('v')) or safe_num(prev_daily.get('v'))
    price_change_pct = ((current_price - prev_close) / prev_close * 100.0) if prev_close > 0 else 0.0
    atr = calc_atr(daily_bars)
    premarket_gap_pct = price_change_pct
    required_premarket_notional = required_premarket_volume_for_gap(premarket_gap_pct)
    snapshot = dict(snapshot or {})
    if feed_used:
        snapshot['_feed_used'] = feed_used
    pmv_diag = calculate_premarket_dollar_volume(symbol, minute_bars, snapshot, required_premarket_dollar_volume=required_premarket_notional)
    premarket_notional = safe_num(pmv_diag.get('actual_premarket_dollar_volume'))
    volume_poc = calc_daily_volume_poc(minute_bars, 0.01 if current_price >= 1 else 0.0001)
    va_metrics = calc_value_area(filter_bars_for_today_session(minute_bars), safe_num, VA_PERCENT)
    vah = safe_num(va_metrics.get('vah'))
    red_candle_trap = detect_heavy_red_candle_trap(minute_bars)
    mtf_aligned = has_positive_mtf_vwap_trend(minute_bars)
    vixy_change = get_vix_change()

    ml_features = store.get_symbol_features(symbol)
    if news_catalyst:
        for key in ('headline_count', 'recent_headline_count', 'latest_headline_age_minutes', 'headline_samples', 'keywords_hit', 'positive_terms', 'negative_terms', 'news_lookup_status', 'qualifies_as_news_catalyst'):
            if key in news_catalyst:
                ml_features[key] = news_catalyst.get(key)
        if news_catalyst.get('news_lookup_status') in {'API_ERROR', 'INVALID_RESPONSE', 'FINNHUB_KEY_MISSING'}:
            ml_features['news_api_error'] = True
    ml_features = _apply_news_evidence_fallback_features(ml_features, news_catalyst)
    p_success = float(ml_features.get('p_success', 0.50) or 0.50)
    sentiment = float(ml_features.get('finbert_sentiment', 0.0) or 0.0)
    keyword_boost = float(ml_features.get('keyword_boost', 0.0) or 0.0)
    catalyst_score = max(1, min(5, int(round(p_success * 5))))
    catalyst_meta = {
        'used_ai': True,
        'model': 'FinBERT + XGBoost',
        'sentiment_score': sentiment,
        'p_success': p_success,
        'keyword_boost': keyword_boost,
        'headline_count': int(ml_features.get('headline_count', 0) or 0),
        'hard_pass': p_success < 0.20,
        'catalyst_category_weight': catalyst_score,
        'direction': 'bullish' if sentiment >= 0 else 'mixed',
        'confidence': 'medium',
        'reason': 'Loaded from pre-market feature store.',
    }
    catalyst_diag = _build_catalyst_diagnostics(ml_features, catalyst_meta)
    catalyst_diag['catalyst_score_input_source'] = ml_features.get('catalyst_score_input_source', 'feature_store')
    catalyst_diag['catalyst_score_adjusted_from_news'] = bool(ml_features.get('catalyst_score_adjusted_from_news'))
    catalyst_diag['catalyst_score_before_news_adjustment'] = ml_features.get('catalyst_score_before_news_adjustment')
    catalyst_diag['catalyst_score_after_news_adjustment'] = catalyst_score
    catalyst_diag['catalyst_score_adjustment_reason'] = ml_features.get('catalyst_score_adjustment_reason')
    catalyst_diag['catalyst_negative_risk'] = bool(ml_features.get('catalyst_negative_risk'))
    catalyst_diag['catalyst_negative_risk_reason'] = ml_features.get('catalyst_negative_risk_reason')
    catalyst_baseline_reason = _baseline_catalyst_reason(catalyst_score, catalyst_diag)
    if catalyst_score == 2 and news_catalyst and catalyst_diag.get('catalyst_headline_count', 0) > 0 and float(keyword_boost or 0.0) <= 0:
        catalyst_baseline_reason = 'FRESH_BUT_NOT_STRONG' if catalyst_diag.get('catalyst_is_fresh') else 'BASELINE_ONLY_WEAK_NEWS'
        catalyst_diag['catalyst_score_not_upgraded_reason'] = 'NEWS_EVIDENCE_NOT_CONNECTED_TO_MODEL'
    liquidity_score, liquidity_meta = score_float_liquidity(profile, asset, premarket_notional, day_volume, spread, atr, current_price)
    liquidity_failure_codes = []
    if liquidity_meta.get('wide_spread_block'):
        liquidity_failure_codes.append('WIDE_SPREAD')
    if pmv_diag.get('actual_premarket_dollar_volume') is None:
        liquidity_failure_codes.append('PREMARKET_VOLUME_UNAVAILABLE')
    elif premarket_notional < required_premarket_notional:
        liquidity_failure_codes.append('PREMARKET_DOLLAR_VOLUME_TOO_LIGHT')
    liquidity_meta.update({'max_allowed_spread_pct': MAX_SPREAD_PCT, 'required_premarket_dollar_volume': round(required_premarket_notional, 2), 'actual_premarket_dollar_volume': round(premarket_notional, 2) if premarket_notional else premarket_notional, 'liquidity_failure_codes': liquidity_failure_codes, 'liquidity_score_reason': f"score={liquidity_score}; spread={liquidity_meta.get('spread_pct')} vs max={MAX_SPREAD_PCT}; premarket=${round(premarket_notional,2) if premarket_notional is not None else 'n/a'}"})
    daily_score, daily_meta = score_daily_alignment(current_price, daily_bars)
    sector_symbol = choose_sector_etf(profile, symbol)
    sector_snapshot = sector_snapshots.get(sector_symbol, {})
    sector_prev = safe_num(sector_snapshot.get('prevDailyBar', {}).get('c')) or 1
    sector_curr = safe_num(sector_snapshot.get('dailyBar', {}).get('c')) or safe_num(sector_snapshot.get('minuteBar', {}).get('c')) or sector_prev
    sector_change_pct = ((sector_curr - sector_prev) / sector_prev * 100.0) if sector_prev > 0 else 0.0
    sector_score, sector_meta = score_sector_sympathy(symbol, price_change_pct, sector_symbol, sector_change_pct, catalyst_meta)
    or_stats = get_opening_range_stats(minute_bars)
    orb_meta = detect_orb(minute_bars, OPENING_RANGE_START_ET, OPENING_RANGE_END_ET)
    open_rs_score, open_rs_meta = score_relative_strength_open(minute_bars, spy_minute_bars)
    vwap_score, vwap_meta = score_vwap_hold_reclaim(minute_bars)
    session_bars = filter_bars_for_today_session(minute_bars)
    vwap_5m_slope = None
    if len(session_bars) >= 10:
        first = calc_vwap(session_bars[-10:-5])
        last = calc_vwap(session_bars[-5:])
        vwap_5m_slope = round(last - first, 6)
    vwap_now = safe_num(vwap_meta.get('vwap'))
    price_vs_vwap_pct = round(((current_price - vwap_now) / vwap_now) * 100.0, 4) if current_price and vwap_now else None
    vwap_reason = 'VWAP trend aligned.' if mtf_aligned else '5-minute VWAP trend is not aligned.'
    vwap_meta.update({'vwap_trend_aligned': bool(mtf_aligned), 'vwap_trend_reason': vwap_reason, 'vwap_5m_slope': vwap_5m_slope, 'price_vs_vwap_pct': price_vs_vwap_pct, 'vwap_reclaim_confirmed': bool(vwap_meta.get('reclaimed_vwap')), 'vwap_hold_bars_count': int(vwap_meta.get('holds_last5', 0) or 0)})
    pullback_score, pullback_meta = score_first_pullback_quality(minute_bars, or_stats)
    entry_score, entry_meta = score_entry_quality(current_price, daily_bars, minute_bars, or_stats, vwap_meta, pullback_meta)
    confirm_score, confirm_meta = score_opening_range_confirmation(current_price, or_stats, vwap_meta)

    rvol = indicators_calc_rvol(minute_bars, filter_bars_for_today_session, bar_dt_et, safe_num)
    trend_efficiency = indicators_calc_trend_efficiency(minute_bars, filter_bars_for_today_session, safe_num)
    halt_risk = calculate_halt_risk_probability(minute_bars)
    rel_strength_vs_spy = open_rs_meta.get('edge', 0.0)
    model_scores = build_model_scores(
        price_change_pct=premarket_gap_pct,
        rvol=rvol,
        float_shares=safe_num(liquidity_meta.get('float_shares')),
        catalyst_weight=int(catalyst_meta.get('catalyst_category_weight') or catalyst_score),
        spread_pct=safe_num(liquidity_meta.get('spread_pct')),
        trend_efficiency=trend_efficiency,
        current_price=current_price,
        vwap=safe_num(vwap_meta.get('vwap')),
        now_label=now_et().strftime('%H:%M'),
    )

    technical_score = (
        catalyst_score
        + liquidity_score
        + daily_score
        + sector_score
        + open_rs_score
        + vwap_score
        + pullback_score
        + entry_score
        + confirm_score
    )
    ai_score = max(0.0, (p_success - 0.40) * 40)
    total = int(round(technical_score + ai_score))
    buy_lower = entry_meta['entry_price']
    buy_upper = round(entry_meta['entry_price'] * (1 + MAX_ENTRY_EXTENSION_PCT), 2)
    p_success = catalyst_meta.get('p_success', 0.0)
    vix_spike_active = vixy_change >= VIX_CIRCUIT_BREAKER_PCT
    sizing = calculate_position_size(
        entry_price=entry_meta['entry_price'],
        stop_price=entry_meta['stop_price'],
        target_price=entry_meta['target_1'],
        p_success=p_success,
        vix_spike_active=vix_spike_active,
    )
    after_time_gate = buy_window_open()
    wait_state = not after_time_gate

    skip_reasons = []
    if catalyst_score < MIN_CATALYST_SCORE:
        skip_reasons.append('Catalyst not strong enough.')
    if premarket_gap_pct < MIN_PREMARKET_GAP_PCT:
        skip_reasons.append('Premarket gap is not strong enough for an A-grade setup.')
    if pmv_diag.get('actual_premarket_dollar_volume') is None:
        skip_reasons.append('Premarket dollar volume unavailable from current data feed.')
        if str(feed_used or '').lower() == 'iex':
            skip_reasons.append('PREMARKET_DATA_UNAVAILABLE_CURRENT_FEED')
    elif premarket_notional < required_premarket_notional:
        skip_reasons.append(f'Premarket dollar volume is too light for a {premarket_gap_pct:.1f}% gap (needs at least ${required_premarket_notional:,.0f}).')
    if sector_score < MIN_SECTOR_SYMPATHY_SCORE:
        skip_reasons.append('Sector sympathy is too weak.')
    if catalyst_meta.get('hard_pass'):
        skip_reasons.append('Gemini flagged the headlines as non-tradeable noise or risk.')
    if liquidity_meta.get('wide_spread_block'):
        skip_reasons.append('Spread is too wide.')
    if liquidity_meta.get('high_float_block'):
        skip_reasons.append(f"Float is too high ({liquidity_meta.get('float_shares', 0):,.0f} shares).")
    if volume_poc and current_price <= volume_poc:
        skip_reasons.append('Price is below the daily volume POC.')
    if vah and current_price <= vah:
        skip_reasons.append(f'Price (${current_price}) is not above Value Area High (${vah}).')
    if red_candle_trap.get('triggered'):
        skip_reasons.append('Hard skip: opening heavy red candle trap detected.')
    if not mtf_aligned:
        skip_reasons.append('5-minute VWAP trend is not aligned.')
    if entry_meta.get('extended'):
        skip_reasons.append('Price is extended above the entry zone.')
    if sizing['qty'] < 1:
        skip_reasons.append('Risk sizing says size is zero.')
    if wait_state:
        skip_reasons.append(f'WAIT until after {NO_BUY_BEFORE_ET} ET.')
    if after_time_gate and not or_stats.get('or_complete'):
        skip_reasons.append('Opening range is not complete.')
    if after_time_gate and not confirm_meta.get('breakout_confirmed'):
        skip_reasons.append('Opening-range breakout is not confirmed yet.')
    if after_time_gate and not vwap_meta.get('reclaimed_vwap'):
        skip_reasons.append('VWAP reclaim/hold is not strong enough.')
    if market_internals.get('longs_blocked'):
        skip_reasons.append(market_internals.get('reason') or 'Market internals are blocking long breakouts.')
    if vixy_change >= VIX_CIRCUIT_BREAKER_PCT:
        skip_reasons.append(f'VIX Volatility Spike: {vixy_change:.1f}% (Limit {VIX_CIRCUIT_BREAKER_PCT:g}%).')

    setup_diag = build_setup_grade_diagnostics(
        total=total,
        catalyst_score=catalyst_score,
        liquidity_score=liquidity_score,
        sector_score=sector_score,
        confirm_score=confirm_score,
        vwap_score=vwap_score,
        pullback_score=pullback_score,
        premarket_gap_pct=premarket_gap_pct,
        premarket_notional=premarket_notional,
    )
    setup_grade = setup_diag['setup_grade']
    setup_grade_reason = setup_diag['setup_grade_reason']
    decision = 'SKIP'

    # --- FIXED DECISION LOGIC ---
    after_time_gate = buy_window_open()

    # If the gate is still closed (it's before 09:45 ET), we MUST wait.
    if not after_time_gate:
        decision = 'WAIT'
    else:
        # Once the gate is open, we use the regime trade decision logic
        regime_decision = regime_trade_decision(model_scores, now_et(), safe_num(rel_strength_vs_spy))

        # Check if it meets the "BUY NOW" high-precision requirements
        is_high_precision = (
            not skip_reasons
            and setup_grade in {'A+', 'A'}
            and total >= MIN_SCORE_TO_EXECUTE
            and current_price >= buy_lower * 0.995
            and current_price <= buy_upper
            and regime_decision == 'BUY NOW'
            and sector_meta.get('is_leader_vs_sector', False)
            and not vix_spike_active
        )

        if is_high_precision:
            decision = 'BUY NOW'
        else:
            decision = 'WATCH' if setup_grade != 'NO TRADE' else 'SKIP'

    decision_reason = "High-precision conditions satisfied; decision is executable BUY NOW."
    if decision == "WAIT":
        decision_reason = f"Buy window closed before {NO_BUY_BEFORE_ET} ET."
    elif decision == "SKIP":
        if setup_grade == "NO TRADE":
            decision_reason = "Setup grade is NO TRADE, so decision is non-executable SKIP."
            skip_reasons.extend([r for r in setup_diag.get('failed_watch_requirements', []) if r not in skip_reasons])
            skip_reasons.extend([r for r in setup_diag.get('failed_a_requirements', []) if r not in skip_reasons])
        else:
            decision_reason = "Setup failed non-negotiable execution gates and was downgraded to SKIP."
    elif decision == "WATCH":
        decision_reason = "Setup is watchlist-eligible but did not meet executable BUY NOW gates."

    executable_decisions = {'BUY NOW', 'A+', 'A'}
    execution_eligibility_reason = "Decision is executable under current CENTRAL_SCANNER_EXECUTE_DECISIONS allowlist."
    if decision not in executable_decisions:
        execution_eligibility_reason = "Decision is non-executable under current CENTRAL_SCANNER_EXECUTE_DECISIONS allowlist."
    skip_reason_codes = []
    for reason in skip_reasons:
        code = normalize_skip_reason_code(reason)
        if code not in skip_reason_codes:
            skip_reason_codes.append(code)

    notes = []
    if or_stats.get('or_high'):
        notes.append(f"OR {OPENING_RANGE_START_ET}-{OPENING_RANGE_END_ET}: {or_stats['or_low']} to {or_stats['or_high']}")
    if vwap_meta.get('vwap'):
        notes.append(f"VWAP {vwap_meta['vwap']}")
    if open_rs_meta.get('edge') is not None:
        notes.append(f"Open RS vs SPY: {open_rs_meta.get('edge', 0)}%")

    # 1. Build the typed Sub-components
    component_scores = ComponentScores(
        catalyst=catalyst_score,
        liquidity=liquidity_score,
        daily_chart_alignment=daily_score,
        sector_sympathy=sector_score,
        open_relative_strength=open_rs_score,
        vwap_hold_reclaim=vwap_score,
        first_pullback=pullback_score,
        entry_quality=entry_score,
        opening_range_confirmation=confirm_score
    )

    watch_panel = WatchPanelDef(
        label=f"{now_et().strftime('%A')}: Watch {symbol}",
        buy_after=f'{NO_BUY_BEFORE_ET} ET',
        buy_range=[round(buy_lower, 2), round(buy_upper, 2)],
        max_shares=sizing['qty'],
        stop=round(entry_meta['stop_price'], 2),
        take_profit_range=[round(entry_meta['target_1'], 2), round(entry_meta['target_2'], 2)],
        max_dollar_loss=sizing['max_dollar_loss'],
        opening_range=[or_stats.get('or_low'), or_stats.get('or_high')],
        vwap=vwap_meta.get('vwap'),
        status=decision,
        setup_grade=setup_grade
    )

    # 2. Build the main typed Result Object
    analysis_result = SymbolAnalysisResult(
        symbol=symbol,
        score_total=total,
        decision=decision,
        current_price=round(current_price, 2),
        buy_lower=round(buy_lower, 2),
        buy_upper=buy_upper,
        entry_price=round(entry_meta['entry_price'], 2),
        stop_price=round(entry_meta['stop_price'], 2),
        target_1=round(entry_meta['target_1'], 2),
        target_2=round(entry_meta['target_2'], 2),
        qty=sizing['qty'],
        risk_per_share=entry_meta['risk_per_share'],
        max_dollar_loss=sizing['max_dollar_loss'],
        buying_power_used=sizing['buying_power_used'],
        rr_ratio_1=entry_meta['rr_ratio_1'],
        rr_ratio_2=entry_meta['rr_ratio_2'],
        score_models=ScoreTriplet(**model_scores).to_dict(),
        scores=component_scores,
        details={
            'catalyst': {**catalyst_meta, **catalyst_diag, 'catalyst_score': catalyst_score, 'catalyst_score_baseline_reason': catalyst_baseline_reason, 'catalyst_score_floor_applied': bool(catalyst_score == 2), 'catalyst_score_components': {'p_success': round(float(p_success), 4), 'sentiment_score': round(float(sentiment), 4), 'keyword_boost': round(float(keyword_boost), 4)}},
            'liquidity': liquidity_meta,
            'daily_chart_alignment': daily_meta,
            'sector_sympathy': sector_meta,
            'open_relative_strength': open_rs_meta,
            'vwap_hold_reclaim': vwap_meta,
            'first_pullback': pullback_meta,
            'entry_quality': entry_meta,
            'opening_range': or_stats,
            'scanner_now_et': or_stats.get('scanner_now_et'),
            'intraday_bar_count': or_stats.get('intraday_bar_count'),
            'today_session_bar_count': or_stats.get('today_session_bar_count'),
            'opening_range_bar_count': or_stats.get('opening_range_bar_count'),
            'latest_bar_timestamp_et': or_stats.get('latest_bar_timestamp_et'),
            'earliest_today_bar_timestamp_et': or_stats.get('earliest_today_bar_timestamp_et'),
            'opening_range_start_et': or_stats.get('opening_range_start_et'),
            'opening_range_end_et': or_stats.get('opening_range_end_et'),
            'opening_range_complete': or_stats.get('opening_range_complete'),
            'opening_range_complete_reason': or_stats.get('opening_range_complete_reason'),
            'or_high': or_stats.get('or_high'),
            'or_low': or_stats.get('or_low'),
            'current_price': or_stats.get('current_price'),
            'breakout_threshold_price': or_stats.get('breakout_threshold_price') or or_stats.get('breakout_price'),
            'breakout_confirmed': or_stats.get('breakout_confirmed'),
            'breakout_confirmed_reason': or_stats.get('breakout_confirmed_reason'),
            'bars_above_breakout': or_stats.get('bars_above_breakout'),
            'orb_setup': orb_meta,
            'opening_range_confirmation': confirm_meta,
            'price_change_pct': round(price_change_pct, 2),
            'premarket_gap_pct': round(premarket_gap_pct, 2),
            'spy_day_change_pct': round(spy_change_pct, 2),
            'spread': round(spread, 4),
            'spread_pct': round((spread / current_price) if current_price > 0 else 0.0, 4),
            'volume_profile': {'daily_poc': round(volume_poc, 4) if volume_poc else None, 'price_above_poc': bool(current_price > volume_poc) if volume_poc else None},
            'value_area': va_metrics,
            'market_internals': market_internals,
            'rvol': round(rvol, 2),
            'trend_efficiency': round(trend_efficiency, 3),
            'halt_risk': halt_risk,
            'relative_strength_vs_spy': round(safe_num(rel_strength_vs_spy), 2),
            'red_candle_trap': red_candle_trap,
            'mtf_vwap_aligned': mtf_aligned,
            'vix_circuit_breaker': vixy_change >= VIX_CIRCUIT_BREAKER_PCT,
            'vixy_change_pct_1h': round(vixy_change, 3),
            'feed_used': feed_used,
            'extended_hours_bars_available': bool(pmv_diag.get('premarket_data_available')),
            'premarket_volume_confidence': 'high' if pmv_diag.get('premarket_data_available') else ('low' if (minute_bars or []) else 'unavailable'),
            'premarket_dollar_volume': pmv_diag.get('actual_premarket_dollar_volume'),
            'required_premarket_dollar_volume': round(required_premarket_notional, 2),
            'premarket_dollar_volume_gap': pmv_diag.get('premarket_dollar_volume_gap'),
            'premarket_dollar_volume_passed': pmv_diag.get('premarket_dollar_volume_passed'),
            'premarket_bar_count': pmv_diag.get('premarket_bar_count'),
            'premarket_data_available': pmv_diag.get('premarket_data_available'),
            'premarket_data_unavailable_reason': pmv_diag.get('premarket_data_unavailable_reason'),
            'premarket_data_source': pmv_diag.get('premarket_data_source'),
            'skip_reasons': skip_reasons,
            'skip_reason_codes': skip_reason_codes,
            'skip_reason': skip_reasons[0] if skip_reasons else None,
            'decision_reason': decision_reason,
            'setup_grade_reason': setup_grade_reason,
            'execution_eligibility_reason': execution_eligibility_reason,
            'min_score_to_execute': MIN_SCORE_TO_EXECUTE,
            'threshold_comparisons': setup_diag['threshold_comparisons'],
            'failed_a_plus_requirements': setup_diag['failed_a_plus_requirements'],
            'failed_a_requirements': setup_diag['failed_a_requirements'],
            'failed_watch_requirements': setup_diag['failed_watch_requirements'],
            'nearest_grade': setup_diag['nearest_grade'],
            'nearest_grade_failed_requirements': setup_diag['nearest_grade_failed_requirements'],
            'sizing': sizing,
            'quick_notes': notes,
        },
        setup_grade=setup_grade,
        watch_panel=watch_panel,
        buy_window_open=after_time_gate,
        opening_range_complete=bool(or_stats.get('or_complete')),
        breakout_confirmed=bool(confirm_meta.get('breakout_confirmed'))
    )

    # 3. Return as a dict so it passes safely to Flask and SQLite
    return analysis_result.to_dict()



def update_dynamic_orb_state_from_market_data() -> Dict[str, Any]:
    try:
        rvol = 1.0
        atr_expansion = 1.0
        state = dynamic_orb.build_dynamic_orb_state(rvol, atr_expansion)
        market_state.set_market_state(market_state.DYNAMIC_ORB_STATE_NAME, state, ttl_seconds=43200)
        market_state.set_data_freshness("market_context")
        return state
    except Exception as exc:
        logger.warning("Dynamic ORB state update failed in scan: %s", exc)
        return dynamic_orb.build_dynamic_orb_state(1.0, 1.0)


INDIVIDUAL_BAR_RETRY_CAP = 25


def fill_missing_bars_individually(symbols: List[str], bars_map: Dict[str, List[Dict[str, Any]]], timeframe: str, start: datetime, end: datetime, limit: int, feed: str) -> Dict[str, Any]:
    attempted = 0
    success = 0
    failed_symbols: List[str] = []
    for symbol in symbols[:INDIVIDUAL_BAR_RETRY_CAP]:
        attempted += 1
        try:
            symbol_bars = get_bars([symbol], timeframe, start, end, limit, feed=feed).get(symbol, [])
            if symbol_bars:
                bars_map[symbol] = symbol_bars
                success += 1
            else:
                failed_symbols.append(symbol)
        except Exception:
            failed_symbols.append(symbol)
            logger.exception("Individual bar retry failed for %s timeframe=%s", symbol, timeframe)
    return {
        'individual_bar_retry_attempted_count': attempted,
        'individual_bar_retry_success_count': success,
        'individual_bar_retry_failed_symbols': failed_symbols,
    }

def run_scan(user: Optional[Any] = None) -> Dict[str, Any]:
    try:
        update_dynamic_orb_state_from_market_data()
    except Exception as exc:
        logger.warning("Dynamic ORB pre-scan update failed (continuing): %s", exc)
    feed = resolve_data_feed(user)
    orb_symbols = get_refined_universe(user=user)
    momentum_symbols, rejected_candidates = get_momentum_breakout_universe(user=user) if MOMENTUM_BREAKOUT_MODE_ENABLED else ([], [])
    source_candidate_counts = {
        'alpaca_movers': len(get_alpaca_movers(SCAN_CANDIDATE_LIMIT)),
        'premarket_leaders': len(get_premarket_leaders(SCAN_CANDIDATE_LIMIT)),
        'unusual_relvol': len(get_unusual_relvol(SCAN_CANDIDATE_LIMIT)),
        'news_catalyst': 0,
        'fallback_market_candidates': 0,
        'momentum_breakout': len(momentum_symbols),
    }
    symbols = list(dict.fromkeys(orb_symbols + momentum_symbols))
    candidate_sources_map: Dict[str, List[str]] = {}
    for sym in orb_symbols:
        candidate_sources_map.setdefault(sym, []).append('orb_primary')
    for sym in momentum_symbols:
        candidate_sources_map.setdefault(sym, []).append('momentum_breakout')
    news_catalyst_map = get_news_catalyst_map(symbols or get_market_candidates(SCAN_CANDIDATE_LIMIT))
    news_catalyst_symbols = [sym for sym, payload in news_catalyst_map.items() if bool(payload.get('qualifies_as_news_catalyst'))]
    news_catalyst_checked_symbols = list(news_catalyst_map.keys())
    news_catalyst_nonqualifying_symbols = [sym for sym in news_catalyst_checked_symbols if sym not in set(news_catalyst_symbols)]
    source_candidate_counts['news_catalyst'] = len(news_catalyst_symbols)
    news_only_added = 0
    for sym in news_catalyst_symbols:
        candidate_sources_map.setdefault(sym, []).append('news_catalyst')
        if sym not in symbols:
            symbols.append(sym)
            news_only_added += 1
    candidate_source_map = {sym: _select_primary_source(srcs) for sym, srcs in candidate_sources_map.items()}
    candidate_count_raw = len(orb_symbols + momentum_symbols)
    candidate_count_after_dedupe = len(symbols)
    fallback_used = False
    fallback_reason = None
    fallback_reason_detail = None
    fallback_candidates = []
    if candidate_count_after_dedupe < 10:
        fallback_candidates = [s for s in get_market_candidates(SCAN_CANDIDATE_LIMIT) if s != 'SPY']
        source_candidate_counts['fallback_market_candidates'] = len(fallback_candidates)
        fallback_used = True
        fallback_reason = 'CANDIDATE_UNIVERSE_TOO_SMALL'
        fallback_reason_detail = 'PRIMARY_CANDIDATES_BELOW_MINIMUM'
        symbols = list(dict.fromkeys(symbols + fallback_candidates))
        for sym in fallback_candidates:
            candidate_sources_map.setdefault(sym, []).append('fallback_market_candidates')
            candidate_source_map[sym] = _select_primary_source(candidate_sources_map.get(sym, []))
    if not symbols:
        raise ScanError('No symbols passed the refined universe gatekeeper.')
    snapshots = get_snapshots(symbols, feed=feed)
    quotes = get_latest_quotes(symbols, feed=feed)
    symbols = apply_user_symbol_filters(
        symbols,
        snapshots,
        quotes,
        user=user,
        candidate_source_map=candidate_source_map,
    )
    candidate_count_after_user_filters = len(symbols)
    symbols_removed_by_user_filters = [s for s in list(dict.fromkeys(orb_symbols + momentum_symbols + fallback_candidates)) if s not in symbols]
    if not symbols or (len(symbols) == 1 and symbols[0] == 'SPY'):
        raise ScanError('No symbols remained after applying your personalization and ESG filters.')
    candidate_count_before_asset_filter = len(symbols)
    asset_filter_rejections = []
    asset_metadata_degraded_allowed_symbols: List[str] = []
    asset_metadata_degraded_rejections: List[Dict[str, Any]] = []
    asset_metadata_by_symbol: Dict[str, Dict[str, Any]] = {}
    filtered_symbols = []
    asset_metadata_failure_count = 0
    asset_metadata_success_count = 0
    asset_metadata_endpoint_used = f'{config.ALPACA_ASSETS_BASE}/v2/assets/{{symbol}}'
    asset_metadata_failure_reason_counts: Dict[str, int] = {}
    asset_metadata_failure_samples: List[Dict[str, Any]] = []
    asset_metadata_all_failed = False
    auth_or_config_failures = {'HTTP_401', 'HTTP_403', 'HTTP_429', 'HTTP_5XX', 'REQUEST_EXCEPTION'}
    asset_metadata_global_failure = False
    asset_metadata_degraded_mode = False
    asset_lookup: Dict[str, Dict[str, Any]] = {}
    degraded_symbol_etf_blocklist = {'TSLL', 'TSLQ', 'TSLZ', 'TQQQ', 'SQQQ', 'SOXL', 'SOXS', 'TZA', 'TNA', 'SPXL', 'SPXS', 'UVXY', 'VIXY', 'BITO', 'BOIL', 'KOLD', 'NVDL', 'NVDX', 'NVDQ', 'NVD', 'LABU', 'LABD'}
    for symbol in symbols:
        asset, asset_diag = get_alpaca_asset_with_diagnostics(
            symbol,
            user=user,
            source=candidate_source_map.get(symbol, 'unknown'),
        )
        asset_lookup[symbol] = asset
        asset_metadata_endpoint_used = asset_diag.get('endpoint_used') or asset_metadata_endpoint_used
        if asset:
            asset_metadata_success_count += 1
        else:
            asset_metadata_failure_count += 1
            reason = asset_diag.get('failure_reason') or 'INVALID_RESPONSE'
            asset_metadata_failure_reason_counts[reason] = asset_metadata_failure_reason_counts.get(reason, 0) + 1
            if len(asset_metadata_failure_samples) < 20:
                asset_metadata_failure_samples.append(asset_diag)
    asset_metadata_all_failed = candidate_count_before_asset_filter > 0 and asset_metadata_success_count == 0
    asset_metadata_global_failure = asset_metadata_all_failed and bool(asset_metadata_failure_reason_counts) and all(
        key in auth_or_config_failures for key in asset_metadata_failure_reason_counts.keys()
    )
    asset_metadata_degraded_mode = asset_metadata_global_failure
    for symbol in symbols:
        asset = asset_lookup.get(symbol, {})
        profile = get_company_profile(symbol)
        classification = classify_asset(
            symbol, asset, profile,
            platform_flags={'biotech': BIOTECH_TRADING_ENABLED, 'etf': ETF_TRADING_ENABLED, 'leveraged_etf': LEVERAGED_ETF_TRADING_ENABLED, 'inverse_etf': INVERSE_ETF_TRADING_ENABLED, 'crypto_etf': CRYPTO_ETF_TRADING_ENABLED, 'options': OPTIONS_TRADING_ENABLED},
            user_flags={'biotech': bool(getattr(user, 'allow_biotech', True)), 'etf': bool(getattr(user, 'allow_etf_trading', True)), 'leveraged_etf': bool(getattr(user, 'allow_leveraged_etfs', False)), 'inverse_etf': bool(getattr(user, 'allow_inverse_etfs', False)), 'crypto_etf': bool(getattr(user, 'allow_crypto_etfs', True)), 'options': bool(getattr(user, 'allow_options_trading', False))}
        )
        symbol_upper = str(symbol or '').upper()
        profile_text = f"{(profile or {}).get('name', '')} {(profile or {}).get('description', '')}".lower()
        is_warrant_symbol = symbol_upper.endswith(('W', 'WS', 'WT', 'WSA', 'WSB', 'R', 'RT')) or symbol_upper in {'R', 'RT'}
        is_warrant_like = is_warrant_symbol or ('warrant' in str((asset or {}).get('name', '')).lower()) or ('warrant' in profile_text) or (' right' in profile_text)
        is_etf_or_leveraged_etf = classification.get('asset_type') in {'BROAD_ETF', 'CRYPTO_ETF', 'LEVERAGED_ETF', 'INVERSE_ETF'}
        has_asset_metadata = bool(asset)
        is_supported_equity_candidate = classification.get('asset_type') in {'COMMON_STOCK', 'LOW_FLOAT_MOMENTUM_STOCK'} and (bool(asset.get('tradable')) if has_asset_metadata else False)
        meta = {
            'asset_classification': classification.get('asset_type'),
            'tradable': bool(asset.get('tradable')),
            'exchange': asset.get('exchange'),
            'easy_to_borrow': asset.get('easy_to_borrow'),
            'asset_metadata_unavailable_degraded': bool(asset_metadata_degraded_mode and not has_asset_metadata),
            'is_warrant_like': is_warrant_like,
            'is_etf_or_leveraged_etf': is_etf_or_leveraged_etf,
            'is_supported_equity_candidate': is_supported_equity_candidate,
        }
        asset_metadata_by_symbol[symbol] = meta
        reason = None
        if has_asset_metadata:
            if not asset.get('tradable'):
                reason = 'NOT_TRADABLE'
            elif is_warrant_like:
                reason = 'WARRANT_OR_RIGHT'
            elif classification.get('asset_type') == 'LEVERAGED_ETF' and not LEVERAGED_ETF_TRADING_ENABLED:
                reason = 'LEVERAGED_ETF_BLOCKED_BY_SETTINGS'
            elif classification.get('asset_type') == 'INVERSE_ETF' and not INVERSE_ETF_TRADING_ENABLED:
                reason = 'ETF_BLOCKED_BY_SETTINGS'
            elif classification.get('asset_type') in {'BROAD_ETF', 'CRYPTO_ETF'} and not ETF_TRADING_ENABLED:
                reason = 'ETF_BLOCKED_BY_SETTINGS'
            elif classification.get('asset_type') not in {'COMMON_STOCK', 'LOW_FLOAT_MOMENTUM_STOCK', 'BROAD_ETF', 'CRYPTO_ETF', 'LEVERAGED_ETF', 'INVERSE_ETF'}:
                reason = 'UNSUPPORTED_ASSET_TYPE'
        elif asset_metadata_degraded_mode:
            if is_warrant_like:
                reason = 'WARRANT_OR_RIGHT'
            elif symbol_upper in degraded_symbol_etf_blocklist:
                if symbol_upper in {'SQQQ', 'SOXS', 'TZA', 'SPXS', 'TSLQ', 'TSLZ', 'NVDQ', 'LABD'} and not INVERSE_ETF_TRADING_ENABLED:
                    reason = 'INVERSE_ETF_BLOCKED_BY_SETTINGS'
                elif symbol_upper in {'BITO'} and not CRYPTO_ETF_TRADING_ENABLED:
                    reason = 'CRYPTO_ETF_BLOCKED_BY_SETTINGS'
                elif symbol_upper in {'TSLL', 'TQQQ', 'SOXL', 'TNA', 'SPXL', 'UVXY', 'VIXY', 'BOIL', 'KOLD', 'NVDL', 'NVDX', 'NVD', 'LABU'} and not LEVERAGED_ETF_TRADING_ENABLED:
                    reason = 'LEVERAGED_ETF_BLOCKED_BY_SETTINGS'
                elif not ETF_TRADING_ENABLED:
                    reason = 'ETF_BLOCKED_BY_SETTINGS'
            elif classification.get('asset_type') == 'LEVERAGED_ETF' and not LEVERAGED_ETF_TRADING_ENABLED:
                reason = 'LEVERAGED_ETF_BLOCKED_BY_SETTINGS'
            elif classification.get('asset_type') == 'INVERSE_ETF' and not INVERSE_ETF_TRADING_ENABLED:
                reason = 'ETF_BLOCKED_BY_SETTINGS'
            elif classification.get('asset_type') in {'BROAD_ETF', 'CRYPTO_ETF'} and not ETF_TRADING_ENABLED:
                reason = 'ETF_BLOCKED_BY_SETTINGS'
            if reason:
                asset_metadata_degraded_rejections.append({'symbol': symbol, 'reason': reason})
            else:
                asset_metadata_degraded_allowed_symbols.append(symbol)
                logger.info("Asset metadata degraded mode active; allowing symbol through metadata filter using conservative symbol/profile checks.", extra={'symbol': symbol})
        else:
            reason = 'MISSING_ASSET_METADATA'
        if reason:
            asset_filter_rejections.append({'symbol': symbol, 'reason': reason, **meta})
            continue
        filtered_symbols.append(symbol)
    symbols = filtered_symbols
    candidate_count_after_asset_filter = len(symbols)
    asset_filter_rejection_counts = {
        k: len([r for r in asset_filter_rejections if r.get('reason') == k]) for k in {r.get('reason') for r in asset_filter_rejections}
    }
    asset_metadata_degraded_rejection_counts = {
        k: len([r for r in asset_metadata_degraded_rejections if r.get('reason') == k]) for k in {r.get('reason') for r in asset_metadata_degraded_rejections}
    }
    asset_filter_removed_symbols = [r.get('symbol') for r in asset_filter_rejections]
    if not symbols or (len(symbols) == 1 and symbols[0] == 'SPY'):
        logger.error(
            "No symbols remained after asset quality filtering.",
            extra={
                'candidate_count_before_asset_filter': candidate_count_before_asset_filter,
                'candidate_count_after_asset_filter': candidate_count_after_asset_filter,
                'asset_filter_rejection_counts': asset_filter_rejection_counts,
                'asset_filter_rejection_samples': asset_filter_rejections[:20],
                'asset_filter_removed_symbols': asset_filter_removed_symbols[:100],
                'asset_filter_empty_reason': 'ALL_CANDIDATES_REJECTED_BY_ASSET_FILTER',
                'asset_metadata_failure_count': asset_metadata_failure_count,
                'asset_metadata_success_count': asset_metadata_success_count,
                'asset_metadata_endpoint_used': asset_metadata_endpoint_used,
                'asset_metadata_all_failed': asset_metadata_all_failed,
            'asset_metadata_requested_count': candidate_count_before_asset_filter,
            'asset_metadata_global_failure': asset_metadata_global_failure,
            'asset_metadata_degraded_mode': asset_metadata_degraded_mode,
            'asset_metadata_failure_reason_counts': asset_metadata_failure_reason_counts,
            'asset_metadata_failure_samples': asset_metadata_failure_samples,
            'asset_metadata_degraded_allowed_count': len(asset_metadata_degraded_allowed_symbols),
            'asset_metadata_degraded_allowed_symbols': asset_metadata_degraded_allowed_symbols[:100],
            'asset_metadata_degraded_rejection_counts': asset_metadata_degraded_rejection_counts,
            'asset_metadata_degraded_rejection_samples': asset_metadata_degraded_rejections[:20],
            },
        )
        raise ScanError("No symbols remained after asset quality filtering.")
    snapshots = get_snapshots(symbols, feed=feed)
    quotes = get_latest_quotes(symbols, feed=feed)
    sector_symbols = ['SPY', 'SMH', 'XLK', 'XLF', 'XLV', 'XLY', 'XLC', 'XLI', 'XLE', 'XLU', 'XLRE', 'XLB', 'XBI', 'KBE']
    sector_snapshots = get_snapshots([s for s in sector_symbols if s not in symbols], feed=feed)
    sector_snapshots.update({k: v for k, v in snapshots.items() if k in sector_symbols})
    end = now_utc()
    daily_start = end - timedelta(days=400)
    intraday_start = end - timedelta(days=3)
    daily_limit = 400
    intraday_limit = 1000
    daily_bars_map = get_bars(symbols, '1Day', daily_start, end, daily_limit, feed=feed)
    minute_bars_map = get_bars(symbols, '1Min', intraday_start, end, intraday_limit, feed=feed)
    missing_daily = [s for s in symbols if not daily_bars_map.get(s)]
    missing_minute = [s for s in symbols if not minute_bars_map.get(s)]
    symbols_with_market_data = [s for s in symbols if snapshots.get(s) or quotes.get(s)]
    retry_daily = fill_missing_bars_individually([s for s in missing_daily if s in symbols_with_market_data], daily_bars_map, '1Day', daily_start, end, daily_limit, feed)
    retry_minute = fill_missing_bars_individually([s for s in missing_minute if s in symbols_with_market_data], minute_bars_map, '1Min', intraday_start, end, intraday_limit, feed)
    missing_daily = [s for s in symbols if not daily_bars_map.get(s)]
    missing_minute = [s for s in symbols if not minute_bars_map.get(s)]

    spy_snap = snapshots.get('SPY', {})
    spy_prev = safe_num(spy_snap.get('prevDailyBar', {}).get('c')) or 1
    spy_curr = safe_num(spy_snap.get('dailyBar', {}).get('c')) or safe_num(spy_snap.get('minuteBar', {}).get('c')) or spy_prev
    spy_change_pct = ((spy_curr - spy_prev) / spy_prev * 100.0) if spy_prev > 0 else 0.0
    spy_minute_bars = minute_bars_map.get('SPY', [])
    market_internals = get_market_internals_bias(feed=feed)

    ranked = []
    analyzed_symbols = []
    logger.info("Starting scan loop for %s symbols", len(symbols))
    rejection_events = []
    for symbol in symbols:
        if symbol == 'SPY':
            rejection_events.append({'symbol': symbol, 'stage': 'context', 'reason': 'SPY_CONTEXT_ONLY', 'price': None, 'dollar_volume': None, 'spread_pct': None, 'source': 'context'})
            continue
        daily_bars = daily_bars_map.get(symbol, [])
        minute_bars = minute_bars_map.get(symbol, [])
        snapshot = dict(snapshots.get(symbol, {}) or {})
        source_list = candidate_sources_map.get(symbol, [candidate_source_map.get(symbol, 'unknown')])
        source = _select_primary_source(source_list)
        snapshot['_candidate_source'] = source
        quote = quotes.get(symbol, {})
        ask = safe_num(quote.get('ap'))
        minute_close = safe_num(snapshot.get('minuteBar', {}).get('c'))
        daily_close = safe_num(snapshot.get('dailyBar', {}).get('c'))
        current_price = ask or minute_close or daily_close

        _scanner_debug("Evaluating %s: Price=$%s, DailyBars=%s, MinBars=%s", symbol, current_price, len(daily_bars), len(minute_bars))

        # FIX 2: Allow up to $500.00
        if current_price and current_price >= 500.0:
            rejection_events.append({'symbol': symbol, 'stage': 'price_volume_filter', 'reason': 'PRICE_TOO_HIGH', 'price': current_price, 'dollar_volume': None, 'spread_pct': None, 'source': 'scan_loop'})
            _scanner_debug("SKIP: %s price too high.", symbol)
            continue
        if not quote:
            rejection_events.append({'symbol': symbol, 'stage': 'analysis_input', 'reason': 'MISSING_QUOTE', 'price': current_price, 'dollar_volume': None, 'spread_pct': None, 'source': 'scan_loop'})
            continue
        if not snapshot:
            rejection_events.append({'symbol': symbol, 'stage': 'analysis_input', 'reason': 'MISSING_SNAPSHOT', 'price': current_price, 'dollar_volume': None, 'spread_pct': None, 'source': 'scan_loop'})
            continue
        if not daily_bars and not minute_bars:
            reason = 'BAR_DATA_RETRY_FAILED' if symbol in (retry_daily['individual_bar_retry_failed_symbols'] + retry_minute['individual_bar_retry_failed_symbols']) else 'MISSING_DAILY_AND_MINUTE_BARS'
            rejection_events.append({'symbol': symbol, 'stage': 'analysis_input', 'reason': reason, 'price': current_price, 'dollar_volume': None, 'spread_pct': None, 'source': 'scan_loop'})
            continue
        if not daily_bars:
            rejection_events.append({'symbol': symbol, 'stage': 'analysis_input', 'reason': 'MISSING_DAILY_BARS', 'price': current_price, 'dollar_volume': None, 'spread_pct': None, 'source': 'scan_loop'})
            continue
        if not minute_bars:
            rejection_events.append({'symbol': symbol, 'stage': 'analysis_input', 'reason': 'MISSING_MINUTE_BARS', 'price': current_price, 'dollar_volume': None, 'spread_pct': None, 'source': 'scan_loop'})
            _scanner_debug("SKIP: %s missing Alpaca data.", symbol)
            continue

        # We removed the silent exception so we can see exact crashes
        try:
            profile = get_company_profile(symbol)
            asset = get_alpaca_asset(symbol)
            analysis = analyze_symbol(symbol, snapshot, quote, daily_bars, minute_bars, spy_change_pct, profile, asset, spy_minute_bars, sector_snapshots, market_internals, feed_used=feed, news_catalyst=news_catalyst_map.get(symbol))
            classification = classify_asset(
                symbol, asset, profile,
                platform_flags={'biotech': BIOTECH_TRADING_ENABLED, 'etf': ETF_TRADING_ENABLED, 'leveraged_etf': LEVERAGED_ETF_TRADING_ENABLED, 'inverse_etf': INVERSE_ETF_TRADING_ENABLED, 'crypto_etf': CRYPTO_ETF_TRADING_ENABLED, 'options': OPTIONS_TRADING_ENABLED},
                user_flags={'biotech': bool(getattr(user, 'allow_biotech', True)), 'etf': bool(getattr(user, 'allow_etf_trading', True)), 'leveraged_etf': bool(getattr(user, 'allow_leveraged_etfs', False)), 'inverse_etf': bool(getattr(user, 'allow_inverse_etfs', False)), 'crypto_etf': bool(getattr(user, 'allow_crypto_etfs', True)), 'options': bool(getattr(user, 'allow_options_trading', False))}
            )
            analysis.update({
                'source': source,
                'sources': list(dict.fromkeys(source_list)),
                'asset_type': classification.get('asset_type'),
                'asset_type_reason': classification.get('asset_type_reason'),
                'platform_allowed': classification.get('platform_allowed'),
                'user_allowed': classification.get('user_allowed'),
                'tradable_by_xeanvi': classification.get('tradable_by_xeanvi'),
                'rejection_reasons': classification.get('rejection_reasons') or [],
            })
            analysis.setdefault('details', {})['candidate_source'] = source
            analysis['details']['candidate_sources'] = list(dict.fromkeys(source_list))
            ranked.append(analysis)
            analyzed_symbols.append(symbol)
            _scanner_debug("SUCCESS: Analyzed %s", symbol)
        except Exception as e:
            rejection_events.append({'symbol': symbol, 'stage': 'analysis', 'reason': 'ANALYSIS_EXCEPTION', 'price': current_price, 'dollar_volume': None, 'spread_pct': None, 'source': 'scan_loop'})
            logger.exception("Crash while analyzing symbol=%s", symbol)
            continue

    logger.info("Scan loop finished. analyzed_symbols=%s", len(ranked))

    if not ranked:
        raise ScanError('No tradeable candidates were found from the current market data.')

    grade_rank = {'A+': 4, 'A': 3, 'WATCH': 2, 'NO TRADE': 1}
    ranked.sort(
        key=lambda x: (
            grade_rank.get(x.get('setup_grade'), 0),
            x['decision'] == 'BUY NOW',
            x['decision'] == 'WATCH FOR BREAKOUT',
            x['scores']['catalyst'],
            x['scores'].get('sector_sympathy', 0),
            x['score_total'],
            x['details']['open_relative_strength'].get('edge', -999),
            -x['details']['liquidity']['spread'],
        ),
        reverse=True,
    )
    best = ranked[0]
    executable_candidates = [r for r in ranked if r.get('decision') in {'BUY NOW', 'A+', 'A'}]
    watch_candidates = [r for r in ranked if r.get('decision') == 'WATCH']
    skip_candidates = [r for r in ranked if r.get('decision') == 'SKIP']
    best_pick_selection_reason = 'HIGHEST_RANKED_CANDIDATE'
    starvation_flags = []
    if asset_metadata_degraded_mode:
        starvation_flags.append('ASSET_METADATA_DEGRADED_MODE')
    if executable_candidates and best.get('decision') == 'SKIP':
        starvation_flags.append('BEST_PICK_IGNORED_EXECUTABLE_CANDIDATE')
        best_pick_selection_reason = 'POTENTIAL_RANKING_BUG_SKIP_OVER_EXECUTABLE'
    top_5 = []
    for r in ranked[:5]:
        d = r.get('details', {})
        actual_pmdv = d.get('premarket_dollar_volume')
        required_pmdv = d.get('required_premarket_dollar_volume')
        gap = (actual_pmdv - required_pmdv) if isinstance(actual_pmdv, (int, float)) and isinstance(required_pmdv, (int, float)) else None
        cat = d.get('catalyst') or {}
        top_5.append({'symbol': r.get('symbol'), 'source': r.get('source', 'unknown'), 'sources': r.get('sources') or [r.get('source', 'unknown')], 'decision': r.get('decision'), 'setup_grade': r.get('setup_grade'), 'score_total': r.get('score_total'), 'liquidity_score': (r.get('scores') or {}).get('liquidity'), 'liquidity_score_reason': (d.get('liquidity') or {}).get('liquidity_score_reason'), 'liquidity_failure_codes': (d.get('liquidity') or {}).get('liquidity_failure_codes') or [], 'catalyst_score': (r.get('scores') or {}).get('catalyst'), 'catalyst_source': cat.get('catalyst_source', 'unknown'), 'catalyst_strength_reason': cat.get('catalyst_strength_reason'), 'catalyst_score_baseline_reason': cat.get('catalyst_score_baseline_reason'), 'catalyst_missing_reason': cat.get('catalyst_missing_reason'), 'catalyst_confidence': cat.get('catalyst_confidence'), 'catalyst_headline_count': cat.get('catalyst_headline_count'), 'catalyst_latest_headline_age_minutes': cat.get('catalyst_latest_headline_age_minutes'), 'catalyst_keywords_hit': cat.get('catalyst_keywords_hit') or [], 'catalyst_score_components': cat.get('catalyst_score_components') or {}, 'catalyst_score_input_source': cat.get('catalyst_score_input_source'), 'catalyst_score_adjusted_from_news': cat.get('catalyst_score_adjusted_from_news'), 'catalyst_score_before_news_adjustment': cat.get('catalyst_score_before_news_adjustment'), 'catalyst_score_after_news_adjustment': cat.get('catalyst_score_after_news_adjustment'), 'catalyst_score_adjustment_reason': cat.get('catalyst_score_adjustment_reason'), 'catalyst_score_not_upgraded_reason': cat.get('catalyst_score_not_upgraded_reason'), 'catalyst_positive_terms': cat.get('catalyst_positive_terms') or [], 'catalyst_negative_terms': cat.get('catalyst_negative_terms') or [], 'catalyst_negative_risk': cat.get('catalyst_negative_risk'), 'catalyst_negative_risk_reason': cat.get('catalyst_negative_risk_reason'), 'open_relative_strength': (d.get('open_relative_strength') or {}).get('edge'), 'spread_pct': d.get('spread_pct'), 'actual_premarket_dollar_volume': actual_pmdv, 'required_premarket_dollar_volume': required_pmdv, 'premarket_dollar_volume_gap': gap, 'premarket_dollar_volume_passed': bool(actual_pmdv is not None and required_pmdv is not None and actual_pmdv >= required_pmdv), 'vwap_trend_aligned': (d.get('vwap_hold_reclaim') or {}).get('vwap_trend_aligned'), 'vwap_trend_reason': (d.get('vwap_hold_reclaim') or {}).get('vwap_trend_reason'), 'price_vs_vwap_pct': (d.get('vwap_hold_reclaim') or {}).get('price_vs_vwap_pct'), 'skip_reason_codes': d.get('skip_reason_codes') or []})
    premarket_unavailable_symbols = [r.get('symbol') for r in ranked if (r.get('details') or {}).get('premarket_data_available') is False]
    premarket_too_light_symbols = [r.get('symbol') for r in ranked if 'PREMARKET_DOLLAR_VOLUME_TOO_LIGHT' in ((r.get('details') or {}).get('skip_reason_codes') or [])]
    premarket_passed_count = len([r for r in ranked if bool((r.get('details') or {}).get('premarket_dollar_volume_passed'))])
    source_quality_summary: Dict[str, Any] = {}
    all_sources = sorted(set([s for srcs in candidate_sources_map.values() for s in srcs] + ['unknown']))
    for src in all_sources:
        analyzed_for_source = [r for r in ranked if src in (r.get('sources') or [r.get('source', 'unknown')])]
        if not analyzed_for_source and src not in source_candidate_counts:
            continue
        source_quality_summary[src] = {
            'raw_count': source_candidate_counts.get(src, 0),
            'analyzed_count': len(analyzed_for_source),
            'executable_count': len([r for r in analyzed_for_source if r.get('decision') in {'BUY NOW', 'A+', 'A'}]),
            'watch_count': len([r for r in analyzed_for_source if r.get('decision') == 'WATCH']),
            'skip_count': len([r for r in analyzed_for_source if r.get('decision') == 'SKIP']),
            'average_score_total': round(sum(float(r.get('score_total') or 0) for r in analyzed_for_source) / max(1, len(analyzed_for_source)), 3),
            'average_catalyst_score': round(sum(float((r.get('scores') or {}).get('catalyst') or 0) for r in analyzed_for_source) / max(1, len(analyzed_for_source)), 3),
            'average_liquidity_score': round(sum(float((r.get('scores') or {}).get('liquidity') or 0) for r in analyzed_for_source) / max(1, len(analyzed_for_source)), 3),
            'average_spread_pct': round(sum(float((r.get('details') or {}).get('spread_pct') or 0) for r in analyzed_for_source) / max(1, len(analyzed_for_source)), 5),
            'average_premarket_dollar_volume': round(sum(float((r.get('details') or {}).get('premarket_dollar_volume') or 0) for r in analyzed_for_source) / max(1, len(analyzed_for_source)), 2),
            'pass_rate_to_analysis': round(len(analyzed_for_source) / max(1, source_candidate_counts.get(src, 0)), 4),
            'avg_actual_premarket_dollar_volume': round(sum(float((r.get('details') or {}).get('premarket_dollar_volume') or 0) for r in analyzed_for_source) / max(1, len(analyzed_for_source)), 2),
            'avg_required_premarket_dollar_volume': round(sum(float((r.get('details') or {}).get('required_premarket_dollar_volume') or 0) for r in analyzed_for_source) / max(1, len(analyzed_for_source)), 2),
            'avg_spread_pct': round(sum(float((r.get('details') or {}).get('spread_pct') or 0) for r in analyzed_for_source) / max(1, len(analyzed_for_source)), 5),
            'executable_rate': round(len([r for r in analyzed_for_source if r.get('decision') in {'BUY NOW', 'A+', 'A'}]) / max(1, len(analyzed_for_source)), 4),
            'watch_rate': round(len([r for r in analyzed_for_source if r.get('decision') == 'WATCH']) / max(1, len(analyzed_for_source)), 4),
            'skip_rate': round(len([r for r in analyzed_for_source if r.get('decision') == 'SKIP']) / max(1, len(analyzed_for_source)), 4),
            'top_symbols': [r.get('symbol') for r in analyzed_for_source[:5]],
            'primary_skip_reason_codes': sorted([(code, len([r for r in analyzed_for_source if code in ((r.get('details') or {}).get('skip_reason_codes') or [])])) for code in {c for rr in analyzed_for_source for c in ((rr.get('details') or {}).get('skip_reason_codes') or [])}], key=lambda x: x[1], reverse=True)[:5],
            'rejection_counts_by_stage': {stage: len([e for e in rejection_events if e.get('stage') == stage and e.get('symbol') in [r.get('symbol') for r in analyzed_for_source]]) for stage in {e.get('stage') for e in rejection_events}},
            'dominant_failure_reason': (sorted([(code, len([r for r in analyzed_for_source if code in ((r.get('details') or {}).get('skip_reason_codes') or [])])) for code in {c for rr in analyzed_for_source for c in ((rr.get('details') or {}).get('skip_reason_codes') or [])}], key=lambda x: x[1], reverse=True)[:1] or [[None, 0]])[0][0],
        }
    catalyst_baseline_reason_counts = {}
    catalyst_missing_reason_counts = {}
    for r in ranked:
        cat = (r.get('details') or {}).get('catalyst', {})
        br = cat.get('catalyst_score_baseline_reason') or 'NONE'
        mr = cat.get('catalyst_missing_reason') or 'UNKNOWN'
        catalyst_baseline_reason_counts[br] = catalyst_baseline_reason_counts.get(br, 0) + 1
        catalyst_missing_reason_counts[mr] = catalyst_missing_reason_counts.get(mr, 0) + 1

    news_symbols = [r for r in ranked if int((((r.get('details') or {}).get('catalyst') or {}).get('catalyst_headline_count') or 0)) > 0]
    non_news_symbols = [r for r in ranked if r not in news_symbols]
    news_adjusted = [r for r in news_symbols if bool((((r.get('details') or {}).get('catalyst') or {}).get('catalyst_score_adjusted_from_news')))]
    positive_keyword_symbols = [r.get('symbol') for r in news_symbols if (((r.get('details') or {}).get('catalyst') or {}).get('catalyst_positive_terms'))]
    negative_keyword_symbols = [r.get('symbol') for r in news_symbols if (((r.get('details') or {}).get('catalyst') or {}).get('catalyst_negative_terms'))]
    still_baseline_after_news_symbols = [r.get('symbol') for r in news_symbols if int((r.get('scores') or {}).get('catalyst') or 0) == 2]
    latest_news_evidence_scoring_summary = {
        'qualified_news_symbols': [r.get('symbol') for r in news_symbols],
        'news_symbols_adjusted_count': len(news_adjusted),
        'news_symbols_not_adjusted_count': max(0, len(news_symbols) - len(news_adjusted)),
        'positive_keyword_symbols': positive_keyword_symbols,
        'negative_keyword_symbols': negative_keyword_symbols,
        'still_baseline_after_news_symbols': still_baseline_after_news_symbols,
        'avg_catalyst_score_for_news_symbols': round(sum(float((r.get('scores') or {}).get('catalyst') or 0) for r in news_symbols) / max(1, len(news_symbols)), 3),
        'avg_catalyst_score_for_non_news_symbols': round(sum(float((r.get('scores') or {}).get('catalyst') or 0) for r in non_news_symbols) / max(1, len(non_news_symbols)), 3),
    }
    latest_news_catalyst_score_blockers = [
        {
            'symbol': r.get('symbol'),
            'headline_count': ((r.get('details') or {}).get('catalyst') or {}).get('catalyst_headline_count'),
            'keywords_hit': ((r.get('details') or {}).get('catalyst') or {}).get('catalyst_keywords_hit') or [],
            'positive_terms': ((r.get('details') or {}).get('catalyst') or {}).get('catalyst_positive_terms') or [],
            'negative_terms': ((r.get('details') or {}).get('catalyst') or {}).get('catalyst_negative_terms') or [],
            'catalyst_score': (r.get('scores') or {}).get('catalyst'),
            'catalyst_score_not_upgraded_reason': ((r.get('details') or {}).get('catalyst') or {}).get('catalyst_score_not_upgraded_reason'),
        }
        for r in news_symbols[:20]
    ]
    chart_pack = get_stock_chart_pack(best['symbol'], user=user)
    valid_candidates = [r for r in ranked if r.get('setup_grade') in {'A+', 'A'}]
    market_call = 'NO TRADE TODAY'
    if valid_candidates:
        market_call = f"{valid_candidates[0]['setup_grade']} setup available"
    elif any(r.get('setup_grade') == 'WATCH' for r in ranked):
        market_call = 'WATCH ONLY'
    return {
        'generated_at': now_utc().isoformat(),
        'day_of_week': now_et().strftime('%A'),
        'market_bias_proxy': {'spy_change_pct': round(spy_change_pct, 2), 'market_internals': market_internals},
        'market_call': market_call,
        'best_pick': best,
        'watchlist': ranked[:(MOMENTUM_WATCHLIST_SIZE if MOMENTUM_BREAKOUT_MODE_ENABLED else WATCHLIST_SIZE)],
        'ranked': ranked[:10],
        'chart_pack': chart_pack,
        'rejected_candidates': rejected_candidates,
        'debug_summary': {'total_symbols_scanned': len(symbols), 'total_symbols_accepted': len(ranked), 'total_symbols_rejected': len(rejected_candidates)},
        'scan_diagnostics': {
            'candidate_count_raw': candidate_count_raw,
            'candidate_count_primary_raw': candidate_count_raw,
            'candidate_count_after_dedupe': candidate_count_after_dedupe,
            'candidate_count_primary_after_dedupe': candidate_count_after_dedupe,
            'candidate_count_after_user_filters': candidate_count_after_user_filters,
            'candidate_count_final_before_analysis': len(symbols),
            'candidate_count_after_price_volume_filters': len(analyzed_symbols),
            'candidate_symbols_sample': symbols[:20],
            'analyzed_symbols': analyzed_symbols,
            'watchlist_symbols': [x.get('symbol') for x in ranked[:WATCHLIST_SIZE]],
            'best_pick_symbol': best.get('symbol'),
            'best_pick_rank_method': 'grade->decision->catalyst->sector->total->rs->spread',
            'top_5_candidates_by_score': top_5,
            'feed_used': feed,
            'extended_hours_bars_available': bool(len(ranked) - len(premarket_unavailable_symbols)),
            'latest_premarket_data_unavailable_count': len(premarket_unavailable_symbols),
            'latest_premarket_volume_unavailable_symbols': premarket_unavailable_symbols[:20],
            'latest_premarket_volume_too_light_symbols': premarket_too_light_symbols[:20],
            'latest_catalyst_score_summary': {
                'symbols_checked': len(ranked),
                'average_catalyst_score': round(sum(float((r.get('scores') or {}).get('catalyst', 0) or 0) for r in ranked) / max(1, len(ranked)), 3),
                'score_counts': {str(i): len([r for r in ranked if int((r.get('scores') or {}).get('catalyst', 0) or 0) == i]) for i in range(1, 6)},
                'source_counts': {str((r.get('details') or {}).get('catalyst', {}).get('catalyst_source', 'unknown')): len([x for x in ranked if (x.get('details') or {}).get('catalyst', {}).get('catalyst_source', 'unknown') == (r.get('details') or {}).get('catalyst', {}).get('catalyst_source', 'unknown')]) for r in ranked[:5]},
                'missing_reason_counts': {str((r.get('details') or {}).get('catalyst', {}).get('catalyst_missing_reason', 'UNKNOWN')): len([x for x in ranked if (x.get('details') or {}).get('catalyst', {}).get('catalyst_missing_reason', 'UNKNOWN') == (r.get('details') or {}).get('catalyst', {}).get('catalyst_missing_reason', 'UNKNOWN')]) for r in ranked[:5]},
                'confidence_counts': {str((r.get('details') or {}).get('catalyst', {}).get('catalyst_confidence', 'unavailable')): len([x for x in ranked if (x.get('details') or {}).get('catalyst', {}).get('catalyst_confidence', 'unavailable') == (r.get('details') or {}).get('catalyst', {}).get('catalyst_confidence', 'unavailable')]) for r in ranked[:5]},
            },
            'latest_weak_catalyst_symbols': [r.get('symbol') for r in ranked if int((r.get('scores') or {}).get('catalyst', 0) or 0) <= 2][:20],
            'latest_vwap_alignment_summary': {
                'aligned_count': len([r for r in ranked if bool((r.get('details') or {}).get('vwap_hold_reclaim', {}).get('vwap_trend_aligned'))]),
                'not_aligned_count': len([r for r in ranked if not bool((r.get('details') or {}).get('vwap_hold_reclaim', {}).get('vwap_trend_aligned'))]),
                'common_reasons': [((r.get('details') or {}).get('vwap_hold_reclaim', {}).get('vwap_trend_reason') or 'unknown') for r in ranked[:5]],
            },
            'latest_liquidity_failure_summary': {
                'low_liquidity_count': len([r for r in ranked if int((r.get('scores') or {}).get('liquidity', 0) or 0) <= 2]),
                'wide_spread_count': len([r for r in ranked if 'WIDE_SPREAD' in ((r.get('details') or {}).get('liquidity', {}).get('liquidity_failure_codes') or [])]),
                'low_premarket_dollar_volume_count': len([r for r in ranked if 'PREMARKET_DOLLAR_VOLUME_TOO_LIGHT' in ((r.get('details') or {}).get('liquidity', {}).get('liquidity_failure_codes') or [])]),
                'unavailable_premarket_volume_count': len([r for r in ranked if 'PREMARKET_VOLUME_UNAVAILABLE' in ((r.get('details') or {}).get('liquidity', {}).get('liquidity_failure_codes') or [])]),
            },
            'latest_candidate_quality_summary': {
                'analyzed_count': len(ranked),
                'executable_count': len(executable_candidates),
                'watch_count': len(watch_candidates),
                'skip_count': len(skip_candidates),
                'primary_quality_blockers': sorted([(code, len([r for r in ranked if code in ((r.get('details') or {}).get('skip_reason_codes') or [])])) for code in {c for rr in ranked for c in ((rr.get('details') or {}).get('skip_reason_codes') or [])}], key=lambda x: x[1], reverse=True)[:10],
            },
            'latest_premarket_volume_summary': {
                'symbols_checked': len(ranked),
                'available_count': len(ranked) - len(premarket_unavailable_symbols),
                'unavailable_count': len(premarket_unavailable_symbols),
                'passed_count': premarket_passed_count,
                'failed_count': max(0, len(ranked) - len(premarket_unavailable_symbols) - premarket_passed_count),
                'feed_used': feed,
            },
            'executable_candidate_count': len(executable_candidates),
            'watch_candidate_count': len(watch_candidates),
            'skip_candidate_count': len(skip_candidates),
            'best_executable_candidate_symbol': executable_candidates[0]['symbol'] if executable_candidates else None,
            'best_watch_candidate_symbol': watch_candidates[0]['symbol'] if watch_candidates else None,
            'best_skip_candidate_symbol': skip_candidates[0]['symbol'] if skip_candidates else None,
            'best_pick_selection_reason': best_pick_selection_reason,
            'scanner_starvation_flags': starvation_flags,
            'source_candidate_counts': {src: len([sym for sym, srcs in candidate_sources_map.items() if src in srcs]) for src in sorted(set(list(source_candidate_counts.keys()) + [s for srcs in candidate_sources_map.values() for s in srcs]))},
            'orb_primary_raw_count': len(orb_symbols),
            'orb_primary_after_filters_count': len([s for s in symbols if 'orb_primary' in (candidate_sources_map.get(s) or [])]),
            'orb_primary_rejection_counts': {'NO_ORB_CANDIDATES': 1 if len(orb_symbols) == 0 else 0},
            'momentum_breakout_raw_count': len(momentum_symbols),
            'momentum_breakout_after_filters_count': len([s for s in symbols if 'momentum_breakout' in (candidate_sources_map.get(s) or [])]),
            'momentum_breakout_rejection_counts': {},
            'source_candidate_symbols_sample': {'fallback_market_candidates': fallback_candidates[:20], 'orb_primary': orb_symbols[:20], 'momentum_breakout': momentum_symbols[:20]},
            'news_catalyst_symbols_sample': news_catalyst_symbols[:20],
            'news_catalyst_checked_symbols_sample': news_catalyst_checked_symbols[:20],
            'news_catalyst_nonqualifying_symbols_sample': news_catalyst_nonqualifying_symbols[:20],
            'news_catalyst_checked_count': len(news_catalyst_checked_symbols),
            'news_catalyst_qualified_count': len(news_catalyst_symbols),
            'news_catalyst_no_news_count': len([p for p in news_catalyst_map.values() if p.get('news_lookup_status') == 'NO_NEWS_FOUND']),
            'news_catalyst_api_error_count': len([p for p in news_catalyst_map.values() if p.get('news_lookup_status') in {'API_ERROR', 'INVALID_RESPONSE', 'FINNHUB_KEY_MISSING'}]),
            'news_catalyst_evidence_sample': [
                {
                    'symbol': p.get('symbol'),
                    'headline_count': p.get('headline_count'),
                    'news_lookup_status': p.get('news_lookup_status'),
                    'qualifies_as_news_catalyst': p.get('qualifies_as_news_catalyst'),
                    'headline_samples': p.get('headline_samples') or [],
                    'keywords_hit': p.get('keywords_hit') or [],
                }
                for p in list(news_catalyst_map.values())[:10]
            ],
            'news_catalyst_raw_count': len(news_catalyst_symbols),
            'news_catalyst_added_to_universe_count': news_only_added,
            'news_catalyst_not_analyzed_symbols': [s for s in news_catalyst_symbols if s not in analyzed_symbols][:50],
            'news_catalyst_rejection_reasons': {r.get('reason', 'UNKNOWN'): len([x for x in rejection_events if x.get('symbol') in set(news_catalyst_symbols) and x.get('reason') == r.get('reason')]) for r in rejection_events if r.get('symbol') in set(news_catalyst_symbols)},
            'top_candidates_by_source': {src: [x.get('symbol') for x in ranked if src in (x.get('sources') or [x.get('source', 'unknown')])][:5] for src in sorted(set([s for srcs in candidate_sources_map.values() for s in srcs]))},
            'rejected_candidate_source_counts': {src: len([r for r in rejection_events if r.get('source') == src]) for src in sorted(set([r.get('source','unknown') for r in rejection_events]))},
            'final_analyzed_symbols_with_source': [{'symbol': x.get('symbol'), 'source': x.get('source', 'unknown'), 'sources': x.get('sources') or [x.get('source', 'unknown')]} for x in ranked],
            'best_pick_source': best.get('source', 'unknown'),
            'latest_candidate_source_quality_summary': source_quality_summary,
            'latest_catalyst_baseline_reason_counts': catalyst_baseline_reason_counts,
            'latest_catalyst_missing_reason_counts': catalyst_missing_reason_counts,
            'latest_news_evidence_scoring_summary': latest_news_evidence_scoring_summary,
            'latest_news_catalyst_score_blockers': latest_news_catalyst_score_blockers,
            'latest_catalyst_feature_store_hit_count': len([r for r in ranked if bool(((r.get('details') or {}).get('catalyst') or {}).get('catalyst_feature_store_hit'))]),
            'latest_catalyst_feature_store_missing_count': len([r for r in ranked if not bool(((r.get('details') or {}).get('catalyst') or {}).get('catalyst_feature_store_hit'))]),
            'candidate_rejection_counts': {k: len([r for r in rejection_events if r.get('stage') == k]) for k in {r.get('stage') for r in rejection_events}},
            'candidate_rejection_samples': rejection_events[:20],
            'gatekeeper_rejection_counts': {},
            'user_filter_rejection_counts': {'user_filters': len(symbols_removed_by_user_filters)},
            'symbols_removed_by_price_volume_filter': [r.get('symbol') for r in rejection_events if r.get('stage') == 'price_volume_filter'],
            'symbols_removed_by_strict_gatekeeper': [],
            'symbols_removed_by_user_filters': symbols_removed_by_user_filters,
            'final_candidate_count': len(symbols),
            'fallback_used': fallback_used,
            'fallback_reason': fallback_reason,
            'fallback_used_reason_detail': fallback_reason_detail,
            'primary_candidate_count_before_fallback': candidate_count_after_dedupe,
            'fallback_candidate_count': len(fallback_candidates),
            'candidate_count_after_fallback': len(symbols),
            'latest_top_5_raw_candidates_before_filters': [s for s in list(dict.fromkeys(orb_symbols + momentum_symbols))[:5]],
            'latest_top_5_rejected_candidates': [r.get('symbol') for r in rejection_events[:5]],
            'latest_final_analyzed_count': len(analyzed_symbols),
            'bar_data_requested_symbols_count': len(symbols),
            'daily_bars_returned_symbols_count': len([s for s in symbols if daily_bars_map.get(s)]),
            'minute_bars_returned_symbols_count': len([s for s in symbols if minute_bars_map.get(s)]),
            'missing_daily_bars_symbols': missing_daily[:50],
            'missing_minute_bars_symbols': missing_minute[:50],
            'missing_snapshot_symbols': [s for s in symbols if not snapshots.get(s)][:50],
            'missing_quote_symbols': [s for s in symbols if not quotes.get(s)][:50],
            'symbols_with_snapshot_but_no_bars': [s for s in symbols if snapshots.get(s) and (not daily_bars_map.get(s) or not minute_bars_map.get(s))][:50],
            'data_feed_used': feed,
            'bar_fetch_time_window': {'daily_start': daily_start.isoformat(), 'daily_end': end.isoformat(), 'intraday_start': intraday_start.isoformat(), 'intraday_end': end.isoformat()},
            'bar_fetch_limit_daily': daily_limit,
            'bar_fetch_limit_intraday': intraday_limit,
            'individual_bar_retry_attempted_count': retry_daily['individual_bar_retry_attempted_count'] + retry_minute['individual_bar_retry_attempted_count'],
            'individual_bar_retry_success_count': retry_daily['individual_bar_retry_success_count'] + retry_minute['individual_bar_retry_success_count'],
            'individual_bar_retry_failed_symbols': list(dict.fromkeys(retry_daily['individual_bar_retry_failed_symbols'] + retry_minute['individual_bar_retry_failed_symbols'])),
            'asset_filter_rejection_counts': asset_filter_rejection_counts,
            'asset_filter_rejection_samples': asset_filter_rejections[:20],
            'candidate_count_before_asset_filter': candidate_count_before_asset_filter,
            'candidate_count_after_asset_filter': candidate_count_after_asset_filter,
            'asset_filter_removed_symbols': asset_filter_removed_symbols,
            'asset_filter_empty_reason': None,
            'asset_metadata_failure_count': asset_metadata_failure_count,
            'asset_metadata_success_count': asset_metadata_success_count,
            'asset_metadata_endpoint_used': asset_metadata_endpoint_used,
            'asset_metadata_all_failed': asset_metadata_all_failed,
            'asset_metadata_requested_count': candidate_count_before_asset_filter,
            'asset_metadata_global_failure': asset_metadata_global_failure,
            'asset_metadata_degraded_mode': asset_metadata_degraded_mode,
            'asset_metadata_failure_reason_counts': asset_metadata_failure_reason_counts,
            'asset_metadata_failure_samples': asset_metadata_failure_samples,
            'asset_metadata_degraded_allowed_count': len(asset_metadata_degraded_allowed_symbols),
            'asset_metadata_degraded_allowed_symbols': asset_metadata_degraded_allowed_symbols[:100],
            'asset_metadata_degraded_rejection_counts': asset_metadata_degraded_rejection_counts,
            'asset_metadata_degraded_rejection_samples': asset_metadata_degraded_rejections[:20],
        },
        'momentum_mode_enabled': MOMENTUM_BREAKOUT_MODE_ENABLED,
        'data_feed_used': feed,
        'rules_applied': {
            'min_catalyst_score': MIN_CATALYST_SCORE,
            'no_buy_before_et': NO_BUY_BEFORE_ET,
            'opening_range_window_et': f'{OPENING_RANGE_START_ET}-{OPENING_RANGE_END_ET}',
            'max_spread_pct': MAX_SPREAD_PCT,
            'max_entry_extension_pct': MAX_ENTRY_EXTENSION_PCT,
            'current_bankroll': CURRENT_BANKROLL,
            'risk_pct_per_trade': 0.02,
            'dynamic_dollar_risk_limit': round(CURRENT_BANKROLL * 0.02, 2),
            'a_plus_score': A_PLUS_SCORE,
            'a_score': A_SCORE,
            'min_premarket_gap_pct': MIN_PREMARKET_GAP_PCT,
            'min_premarket_dollar_vol': MIN_PREMARKET_DOLLAR_VOL,
            'market_internals_block_enabled': MARKET_INTERNALS_BLOCK_ENABLED,
        },
    }




def debug_asset_metadata_lookup(
    symbols: List[str],
    user: Optional[Any] = None,
    source_map: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    source_map = source_map or {}
    rows = []
    for symbol in symbols:
        _, diag = get_alpaca_asset_with_diagnostics(symbol, user=user, source=source_map.get(symbol, 'unknown'))
        row = {k: diag.get(k) for k in ('symbol', 'endpoint_used', 'auth_source', 'ok', 'status_code', 'failure_reason', 'response_text_short')}
        rows.append(row)
        logger.info("asset_lookup symbol=%s endpoint=%s auth=%s ok=%s status=%s reason=%s response=%s", row['symbol'], row['endpoint_used'], row['auth_source'], row['ok'], row['status_code'], row['failure_reason'], row['response_text_short'])
    return rows

def get_momentum_breakout_universe(limit: Optional[int] = None, user: Optional[Any] = None) -> Tuple[List[str], List[Dict[str, Any]]]:
    limit = limit or MOMENTUM_SCAN_CANDIDATE_LIMIT
    candidates = list(dict.fromkeys(get_alpaca_movers(limit) + get_premarket_leaders(limit) + get_unusual_relvol(limit)))
    feed = resolve_data_feed(user)
    snapshots = get_snapshots(candidates, feed=feed)
    quotes = get_latest_quotes(candidates, feed=feed)
    valid, rejected = [], []
    user_allow_penny = bool(getattr(user, 'allow_penny_stocks', False)) or not bool(getattr(user, 'exclude_penny_stocks', True))
    allow_penny = MOMENTUM_ALLOW_PENNY_STOCKS and user_allow_penny
    for symbol in candidates:
        snap = snapshots.get(symbol, {})
        quote = quotes.get(symbol, {})
        price = safe_num(quote.get('ap')) or safe_num(snap.get('minuteBar', {}).get('c'))
        prev = safe_num(snap.get('prevDailyBar', {}).get('c'))
        try:
            asset = get_alpaca_asset(symbol)
        except Exception:
            asset = {}
        try:
            profile = get_company_profile(symbol)
        except Exception:
            profile = {}
        classification = classify_asset(
            symbol, asset, profile,
            platform_flags={'biotech': BIOTECH_TRADING_ENABLED, 'etf': ETF_TRADING_ENABLED, 'leveraged_etf': LEVERAGED_ETF_TRADING_ENABLED, 'inverse_etf': INVERSE_ETF_TRADING_ENABLED, 'crypto_etf': CRYPTO_ETF_TRADING_ENABLED, 'options': OPTIONS_TRADING_ENABLED},
            user_flags={'biotech': bool(getattr(user, 'allow_biotech', True)), 'etf': bool(getattr(user, 'allow_etf_trading', True)), 'leveraged_etf': bool(getattr(user, 'allow_leveraged_etfs', False)), 'inverse_etf': bool(getattr(user, 'allow_inverse_etfs', False)), 'crypto_etf': bool(getattr(user, 'allow_crypto_etfs', True)), 'options': bool(getattr(user, 'allow_options_trading', False))}
        )
        base = {k: classification.get(k) for k in ('asset_type', 'asset_type_reason', 'platform_allowed', 'user_allowed', 'tradable_by_xeanvi')}
        if not classification.get('tradable_by_xeanvi', False):
            rejected.append({'symbol': symbol, **base, 'rejection_reason': classification.get('rejection_reason'), 'rejection_reasons': classification.get('rejection_reasons', [])})
            continue
        if price <= 0 or prev <= 0:
            rejected.append({'symbol': symbol, **base, 'rejection_reason': 'missing_prev_close', 'rejection_reasons': ['missing_prev_close']})
            continue
        if price < 5.0 and not allow_penny:
            rejected.append({'symbol': symbol, **base, 'rejection_reason': 'penny_stock_excluded', 'rejection_reasons': ['penny_stock_excluded']})
            continue
        change = ((price - prev) / prev) * 100.0
        vol = safe_num(snap.get('dailyBar', {}).get('v')) * price
        if not (MOMENTUM_MIN_PRICE <= price <= MOMENTUM_MAX_PRICE):
            rejected.append({'symbol': symbol, **base, 'rejection_reason': 'price_out_of_range', 'rejection_reasons': ['price_out_of_range']})
            continue
        if change >= MOMENTUM_MIN_DAY_CHANGE_PCT and vol >= MOMENTUM_MIN_DOLLAR_VOLUME or change >= MOMENTUM_EXTREME_DAY_CHANGE_PCT:
            valid.append(symbol)
        else:
            rejected.append({'symbol': symbol, **base, 'rejection_reason': 'not_enough_momentum', 'rejection_reasons': ['not_enough_momentum']})
    return valid, rejected[:MOMENTUM_DEBUG_REJECTIONS_LIMIT]
SKIP_REASON_CODE_MAP = {
    'Catalyst not strong enough.': 'CATALYST_SCORE_BELOW_WATCH_THRESHOLD',
    'Premarket gap is not strong enough for an A-grade setup.': 'PREMARKET_GAP_BELOW_A_THRESHOLD',
    'Sector sympathy is too weak.': 'SECTOR_SYMPATHY_BELOW_THRESHOLD',
    'Gemini flagged the headlines as non-tradeable noise or risk.': 'CATALYST_HARD_PASS',
    'Spread is too wide.': 'SPREAD_TOO_WIDE',
    'Price is below the daily volume POC.': 'PRICE_NOT_ABOVE_DAILY_POC',
    'Hard skip: opening heavy red candle trap detected.': 'HEAVY_RED_CANDLE_TRAP',
    '5-minute VWAP trend is not aligned.': 'VWAP_TREND_NOT_ALIGNED',
    'Price is extended above the entry zone.': 'PRICE_EXTENDED_ABOVE_ENTRY_ZONE',
    'Risk sizing says size is zero.': 'QTY_BELOW_ONE',
    'Opening range is not complete.': 'OPENING_RANGE_NOT_COMPLETE',
    'Opening-range breakout is not confirmed yet.': 'OPENING_RANGE_BREAKOUT_NOT_CONFIRMED',
    'VWAP reclaim/hold is not strong enough.': 'VWAP_RECLAIM_NOT_STRONG_ENOUGH',
    'Premarket dollar volume unavailable from current data feed.': 'PREMARKET_DOLLAR_VOLUME_UNAVAILABLE',
}

def normalize_skip_reason_code(reason: str) -> str:
    text = str(reason or '').strip()
    if not text:
        return 'UNKNOWN'
    if text in SKIP_REASON_CODE_MAP:
        return SKIP_REASON_CODE_MAP[text]
    if text.startswith('Premarket dollar volume is too light'):
        return 'PREMARKET_DOLLAR_VOLUME_TOO_LIGHT'
    if text.startswith('Price ($') and 'Value Area High' in text:
        return 'PRICE_NOT_ABOVE_VALUE_AREA_HIGH'
    if text.startswith('Float is too high'):
        return 'FLOAT_TOO_HIGH'
    if text.startswith('WAIT until after'):
        return 'BUY_WINDOW_CLOSED'
    if text.startswith('VIX Volatility Spike'):
        return 'VIX_CIRCUIT_BREAKER'
    return text.upper().replace(' ', '_').replace('-', '_').replace('.', '')
