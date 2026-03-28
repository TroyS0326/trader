from __future__ import annotations

from datetime import datetime, timedelta, timezone
from statistics import mean
from typing import Any, Dict, List, Tuple
from zoneinfo import ZoneInfo

import requests

from ai_catalyst import classify_news_with_gemini
from config import (
    ALPACA_API_KEY,
    ALPACA_API_SECRET,
    ALPACA_DATA_BASE,
    ALPACA_FEED,
    DEFAULT_RISK_CAPITAL,
    FINNHUB_API_KEY,
    MAX_BUY_SHARES,
    MAX_DOLLAR_LOSS_PER_TRADE,
    MAX_ENTRY_EXTENSION_PCT,
    MAX_SPREAD_PCT,
    MARKET_INTERNALS_ADD_SYMBOL,
    MARKET_INTERNALS_BLOCK_ENABLED,
    MARKET_INTERNALS_TICK_SYMBOL,
    MIN_CATALYST_SCORE,
    MIN_PREMARKET_DOLLAR_VOL,
    MIN_PREMARKET_GAP_PCT,
    MIN_SECTOR_SYMPATHY_SCORE,
    A_PLUS_SCORE,
    A_SCORE,
    MIN_SCORE_TO_EXECUTE,
    NO_BUY_BEFORE_ET,
    OPENING_RANGE_END_ET,
    OPENING_RANGE_START_ET,
    OR_BREAKOUT_BUFFER_PCT,
    PULLBACK_MAX_RETRACE_PCT,
    SCAN_CANDIDATE_LIMIT,
    TIMEZONE_LABEL,
    WATCHLIST_SIZE,
)

TIMEOUT = 20
MAX_FLOAT_SHARES = 50_000_000
HIGH_GAP_THRESHOLD_PCT = 20.0
HIGH_GAP_MIN_PREMARKET_DOLLAR_VOL = 5_000_000
VETERAN_BLACKLIST = {
    'NVD', 'NVDL', 'NVDX', 'NVDQ', 'TQQQ', 'SQQQ', 'QLD', 'QID', 'SOXL', 'SOXS',
    'UPRO', 'SPXU', 'SPXL', 'SPXS', 'UVXY', 'VIXY', 'SVIX', 'BOIL', 'KOLD', 'UCO',
    'SCO', 'YINN', 'YANG', 'JNUG', 'JDST', 'FAS', 'FAZ'
}


class ScanError(Exception):
    pass


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
    resp = requests.get(url, params=params or {}, headers=headers or {}, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def safe_num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


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


def get_snapshots(symbols: List[str]) -> Dict[str, Any]:
    data = _get_json(
        f'{ALPACA_DATA_BASE}/v2/stocks/snapshots',
        params={'symbols': ','.join(symbols), 'feed': ALPACA_FEED},
        headers=_alpaca_headers(),
    )
    return data.get('snapshots', data)


def get_latest_quotes(symbols: List[str]) -> Dict[str, Any]:
    data = _get_json(
        f'{ALPACA_DATA_BASE}/v2/stocks/quotes/latest',
        params={'symbols': ','.join(symbols), 'feed': ALPACA_FEED},
        headers=_alpaca_headers(),
    )
    return data.get('quotes', data)


def get_bars(symbols: List[str], timeframe: str, start: datetime, end: datetime, limit: int) -> Dict[str, List[Dict[str, Any]]]:
    data = _get_json(
        f'{ALPACA_DATA_BASE}/v2/stocks/bars',
        params={
            'symbols': ','.join(symbols),
            'timeframe': timeframe,
            'start': start.isoformat(),
            'end': end.isoformat(),
            'limit': limit,
            'adjustment': 'split',
            'feed': ALPACA_FEED,
        },
        headers=_alpaca_headers(),
    )
    return data.get('bars', {})


def get_company_news(symbol: str, lookback_days: int = 3) -> List[Dict[str, Any]]:
    if not FINNHUB_API_KEY:
        return []
    today = datetime.utcnow().date()
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


def get_alpaca_asset(symbol: str) -> Dict[str, Any]:
    try:
        payload = _get_json(f'{ALPACA_DATA_BASE}/v2/assets/{symbol}', headers=_alpaca_headers())
        return payload if isinstance(payload, dict) else {}
    except requests.RequestException:
        return {}


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


def filter_bars_for_today_session(minute_bars: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    today = now_et().date()
    out: List[Dict[str, Any]] = []
    for bar in minute_bars:
        dt = bar_dt_et(bar)
        if not dt or dt.date() != today:
            continue
        mins = dt.hour * 60 + dt.minute
        if 9 * 60 + 30 <= mins <= 16 * 60:
            out.append(bar)
    return out


def filter_bars_in_et_window(minute_bars: List[Dict[str, Any]], start_label: str, end_label: str) -> List[Dict[str, Any]]:
    start_h, start_m = parse_hhmm(start_label)
    end_h, end_m = parse_hhmm(end_label)
    start_min = start_h * 60 + start_m
    end_min = end_h * 60 + end_m
    out: List[Dict[str, Any]] = []
    for bar in minute_bars:
        dt = bar_dt_et(bar)
        if not dt or dt.date() != now_et().date():
            continue
        mins = dt.hour * 60 + dt.minute
        if start_min <= mins < end_min:
            out.append(bar)
    return out


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


def get_stock_chart_pack(symbol: str) -> Dict[str, Any]:
    end = now_utc()
    daily_start = end - timedelta(days=260)
    intraday_start = end - timedelta(days=3)
    daily = get_bars([symbol], '1Day', daily_start, end, 260).get(symbol, [])
    intraday = get_bars([symbol], '1Min', intraday_start, end, 1000).get(symbol, [])
    return {'symbol': symbol, 'daily': to_chart_bars(daily[-220:]), 'intraday': to_chart_bars(intraday[-390:])}


def score_float_liquidity(profile: Dict[str, Any], asset: Dict[str, Any], premarket_notional: float, day_volume: float, spread: float, atr: float, current_price: float) -> Tuple[int, Dict[str, Any]]:
    shares_out = safe_num(profile.get('shareOutstanding')) * 1_000_000
    float_shares = extract_float_shares(profile, asset)
    high_float_block = bool(float_shares and float_shares > MAX_FLOAT_SHARES)
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
    headlines = get_company_news(symbol)
    ai = classify_news_with_gemini(symbol, headlines)
    if ai.get('used_ai'):
        score = int(ai.get('score') or 1)
        if ai.get('direction') == 'bearish':
            score = max(1, score - 1)
        if ai.get('hard_pass'):
            score = 1
        return score, {
            'used_ai': True,
            'headline_count': len(headlines),
            'catalyst_type': ai.get('catalyst_type', 'unknown'),
            'direction': ai.get('direction', 'unknown'),
            'confidence': ai.get('confidence', 'low'),
            'hard_pass': ai.get('hard_pass', False),
            'reason': ai.get('reason', ''),
            'headlines': headlines[:8],
        }

    score = 1
    if len(headlines) >= 6 and abs(price_change_pct) >= 8:
        score = 4
    elif len(headlines) >= 3 and abs(price_change_pct) >= 4:
        score = 3
    elif len(headlines) >= 1 or abs(price_change_pct) >= 5:
        score = 2
    return score, {
        'used_ai': False,
        'headline_count': len(headlines),
        'catalyst_type': 'unknown',
        'direction': 'unknown',
        'confidence': 'low',
        'hard_pass': False,
        'reason': ai.get('reason') or 'Fallback scoring used because Gemini was unavailable.',
        'headlines': headlines[:8],
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
    score = 1
    if bullish and sector_change_pct > 0 and edge >= 4:
        score = 5
    elif bullish and sector_change_pct >= -0.2 and edge >= 2.5:
        score = 4
    elif edge >= 1.0:
        score = 3
    elif edge >= 0:
        score = 2
    return score, {
        'sector_symbol': sector_symbol,
        'sector_change_pct': round(sector_change_pct, 2),
        'edge_vs_sector_pct': round(edge, 2),
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


def get_opening_range_stats(minute_bars: List[Dict[str, Any]]) -> Dict[str, Any]:
    session_bars = filter_bars_for_today_session(minute_bars)
    or_bars = filter_bars_in_et_window(session_bars, OPENING_RANGE_START_ET, OPENING_RANGE_END_ET)
    now_bar_count = len(session_bars)
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
        or_complete = buy_window_open() and len(or_bars) >= 20
        breakout_confirmed = or_complete and bars_above_breakout >= 2 and current_price >= breakout_price
        return {
            'session_bars': now_bar_count,
            'or_complete': or_complete,
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


def get_market_internals_bias() -> Dict[str, Any]:
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
        bars = get_bars([MARKET_INTERNALS_TICK_SYMBOL, MARKET_INTERNALS_ADD_SYMBOL], '1Min', start, end, 60)
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
    stop_anchor = max(safe_num(or_stats.get('or_low')), safe_num(vwap_meta.get('vwap')) * 0.995, coil_low)
    stop = min(stop_anchor, entry - max(0.05, atr * 0.35))
    stop = max(stop, recent_low)
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


def calculate_position_size(entry_price: float, stop_price: float) -> Dict[str, Any]:
    risk_per_share = max(0.01, round(entry_price - stop_price, 2))
    capital_qty = int(DEFAULT_RISK_CAPITAL // max(0.01, entry_price))
    risk_qty = int(MAX_DOLLAR_LOSS_PER_TRADE // risk_per_share)
    qty = max(0, min(MAX_BUY_SHARES, capital_qty, risk_qty))
    return {
        'qty': qty,
        'capital_qty': capital_qty,
        'risk_qty': risk_qty,
        'max_dollar_loss': round(qty * risk_per_share, 2),
        'buying_power_used': round(qty * entry_price, 2),
    }


def analyze_symbol(symbol: str, snapshot: Dict[str, Any], quote: Dict[str, Any], daily_bars: List[Dict[str, Any]], minute_bars: List[Dict[str, Any]], spy_change_pct: float, profile: Dict[str, Any], asset: Dict[str, Any], spy_minute_bars: List[Dict[str, Any]], sector_snapshots: Dict[str, Any], market_internals: Dict[str, Any]) -> Dict[str, Any]:
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
    premarket_notional = premarket_dollar_volume(minute_bars)
    premarket_gap_pct = price_change_pct
    required_premarket_notional = required_premarket_volume_for_gap(premarket_gap_pct)
    volume_poc = calc_daily_volume_poc(minute_bars, 0.01 if current_price >= 1 else 0.0001)
    red_candle_trap = detect_heavy_red_candle_trap(minute_bars)

    catalyst_score, catalyst_meta = score_catalyst(symbol, price_change_pct)
    liquidity_score, liquidity_meta = score_float_liquidity(profile, asset, premarket_notional, day_volume, spread, atr, current_price)
    daily_score, daily_meta = score_daily_alignment(current_price, daily_bars)
    sector_symbol = choose_sector_etf(profile, symbol)
    sector_snapshot = sector_snapshots.get(sector_symbol, {})
    sector_prev = safe_num(sector_snapshot.get('prevDailyBar', {}).get('c')) or 1
    sector_curr = safe_num(sector_snapshot.get('dailyBar', {}).get('c')) or safe_num(sector_snapshot.get('minuteBar', {}).get('c')) or sector_prev
    sector_change_pct = ((sector_curr - sector_prev) / sector_prev * 100.0) if sector_prev > 0 else 0.0
    sector_score, sector_meta = score_sector_sympathy(symbol, price_change_pct, sector_symbol, sector_change_pct, catalyst_meta)
    or_stats = get_opening_range_stats(minute_bars)
    open_rs_score, open_rs_meta = score_relative_strength_open(minute_bars, spy_minute_bars)
    vwap_score, vwap_meta = score_vwap_hold_reclaim(minute_bars)
    pullback_score, pullback_meta = score_first_pullback_quality(minute_bars, or_stats)
    entry_score, entry_meta = score_entry_quality(current_price, daily_bars, minute_bars, or_stats, vwap_meta, pullback_meta)
    confirm_score, confirm_meta = score_opening_range_confirmation(current_price, or_stats, vwap_meta)

    total = catalyst_score + liquidity_score + daily_score + sector_score + open_rs_score + vwap_score + pullback_score + entry_score + confirm_score
    buy_lower = entry_meta['entry_price']
    buy_upper = round(entry_meta['entry_price'] * (1 + MAX_ENTRY_EXTENSION_PCT), 2)
    sizing = calculate_position_size(entry_meta['entry_price'], entry_meta['stop_price'])
    after_time_gate = buy_window_open()
    wait_state = not after_time_gate

    skip_reasons = []
    if catalyst_score < MIN_CATALYST_SCORE:
        skip_reasons.append('Catalyst not strong enough.')
    if premarket_gap_pct < MIN_PREMARKET_GAP_PCT:
        skip_reasons.append('Premarket gap is not strong enough for an A-grade setup.')
    if premarket_notional < required_premarket_notional:
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
    if red_candle_trap.get('triggered'):
        skip_reasons.append('Hard skip: opening heavy red candle trap detected.')
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

    setup_grade = classify_setup_grade(total, catalyst_score, liquidity_score, sector_score, confirm_score, vwap_score, pullback_score, premarket_gap_pct, premarket_notional)
    in_buy_zone = current_price >= buy_lower * 0.995 and current_price <= buy_upper
    decision = 'SKIP'
    if wait_state and setup_grade in {'A+', 'A', 'WATCH'}:
        decision = 'WAIT'
    elif setup_grade in {'A+', 'A'} and not skip_reasons and total >= MIN_SCORE_TO_EXECUTE and in_buy_zone:
        decision = 'BUY NOW'
    elif setup_grade in {'A+', 'A', 'WATCH'}:
        decision = 'WATCH FOR BREAKOUT'

    notes = []
    if or_stats.get('or_high'):
        notes.append(f"OR {OPENING_RANGE_START_ET}-{OPENING_RANGE_END_ET}: {or_stats['or_low']} to {or_stats['or_high']}")
    if vwap_meta.get('vwap'):
        notes.append(f"VWAP {vwap_meta['vwap']}")
    if open_rs_meta.get('edge') is not None:
        notes.append(f"Open RS vs SPY: {open_rs_meta.get('edge', 0)}%")

    return {
        'symbol': symbol,
        'score_total': total,
        'decision': decision,
        'current_price': round(current_price, 2),
        'buy_lower': round(buy_lower, 2),
        'buy_upper': buy_upper,
        'entry_price': round(entry_meta['entry_price'], 2),
        'stop_price': round(entry_meta['stop_price'], 2),
        'target_1': round(entry_meta['target_1'], 2),
        'target_2': round(entry_meta['target_2'], 2),
        'qty': sizing['qty'],
        'risk_per_share': entry_meta['risk_per_share'],
        'max_dollar_loss': sizing['max_dollar_loss'],
        'buying_power_used': sizing['buying_power_used'],
        'rr_ratio_1': entry_meta['rr_ratio_1'],
        'rr_ratio_2': entry_meta['rr_ratio_2'],
        'scores': {
            'catalyst': catalyst_score,
            'liquidity': liquidity_score,
            'daily_chart_alignment': daily_score,
            'sector_sympathy': sector_score,
            'open_relative_strength': open_rs_score,
            'vwap_hold_reclaim': vwap_score,
            'first_pullback': pullback_score,
            'entry_quality': entry_score,
            'opening_range_confirmation': confirm_score,
        },
        'details': {
            'catalyst': catalyst_meta,
            'liquidity': liquidity_meta,
            'daily_chart_alignment': daily_meta,
            'sector_sympathy': sector_meta,
            'open_relative_strength': open_rs_meta,
            'vwap_hold_reclaim': vwap_meta,
            'first_pullback': pullback_meta,
            'entry_quality': entry_meta,
            'opening_range': or_stats,
            'opening_range_confirmation': confirm_meta,
            'price_change_pct': round(price_change_pct, 2),
            'premarket_gap_pct': round(premarket_gap_pct, 2),
            'spy_day_change_pct': round(spy_change_pct, 2),
            'spread': round(spread, 4),
            'spread_pct': round((spread / current_price) if current_price > 0 else 0.0, 4),
            'volume_profile': {'daily_poc': round(volume_poc, 4) if volume_poc else None, 'price_above_poc': bool(current_price > volume_poc) if volume_poc else None},
            'market_internals': market_internals,
            'red_candle_trap': red_candle_trap,
            'required_premarket_dollar_volume': round(required_premarket_notional, 2),
            'skip_reasons': skip_reasons,
            'sizing': sizing,
            'quick_notes': notes,
        },
        'setup_grade': setup_grade,
        'watch_panel': {
            'label': f"{now_et().strftime('%A')}: Watch {symbol}",
            'buy_after': f'{NO_BUY_BEFORE_ET} ET',
            'buy_range': [round(buy_lower, 2), round(buy_upper, 2)],
            'max_shares': sizing['qty'],
            'stop': round(entry_meta['stop_price'], 2),
            'take_profit_range': [round(entry_meta['target_1'], 2), round(entry_meta['target_2'], 2)],
            'max_dollar_loss': sizing['max_dollar_loss'],
            'opening_range': [or_stats.get('or_low'), or_stats.get('or_high')],
            'vwap': vwap_meta.get('vwap'),
            'status': decision,
            'setup_grade': setup_grade,
        },
        'buy_window_open': after_time_gate,
        'opening_range_complete': bool(or_stats.get('or_complete')),
        'breakout_confirmed': bool(confirm_meta.get('breakout_confirmed')),
    }


def run_scan() -> Dict[str, Any]:
    raw_symbols = get_market_candidates(100)
    snapshots = get_snapshots(raw_symbols)

    cheap_symbols = []
    for sym in raw_symbols:
        if sym == 'SPY':
            continue
        snap = snapshots.get(sym, {})
        daily_c = safe_num(snap.get('dailyBar', {}).get('c'))
        prev_c = safe_num(snap.get('prevDailyBar', {}).get('c'))
        price = daily_c or prev_c
        if price > 0 and price < 5.0:
            cheap_symbols.append(sym)

    symbols = cheap_symbols[:SCAN_CANDIDATE_LIMIT]
    if 'SPY' not in symbols:
        symbols.append('SPY')

    quotes = get_latest_quotes(symbols)
    sector_symbols = ['SPY', 'SMH', 'XLK', 'XLF', 'XLV', 'XLY', 'XLC', 'XLI', 'XLE', 'XLU', 'XLRE', 'XLB', 'XBI', 'KBE']
    sector_snapshots = get_snapshots([s for s in sector_symbols if s not in symbols])
    sector_snapshots.update({k: v for k, v in snapshots.items() if k in sector_symbols})
    end = now_utc()
    daily_bars_map = get_bars(symbols, '1Day', end - timedelta(days=400), end, 400)
    minute_bars_map = get_bars(symbols, '1Min', end - timedelta(days=3), end, 1000)

    spy_snap = snapshots.get('SPY', {})
    spy_prev = safe_num(spy_snap.get('prevDailyBar', {}).get('c')) or 1
    spy_curr = safe_num(spy_snap.get('dailyBar', {}).get('c')) or safe_num(spy_snap.get('minuteBar', {}).get('c')) or spy_prev
    spy_change_pct = ((spy_curr - spy_prev) / spy_prev * 100.0) if spy_prev > 0 else 0.0
    spy_minute_bars = minute_bars_map.get('SPY', [])
    market_internals = get_market_internals_bias()

    ranked = []
    for symbol in symbols:
        if symbol == 'SPY':
            continue
        daily_bars = daily_bars_map.get(symbol, [])
        minute_bars = minute_bars_map.get(symbol, [])
        snapshot = snapshots.get(symbol, {})
        quote = quotes.get(symbol, {})
        ask = safe_num(quote.get('ap'))
        minute_close = safe_num(snapshot.get('minuteBar', {}).get('c'))
        daily_close = safe_num(snapshot.get('dailyBar', {}).get('c'))
        current_price = ask or minute_close or daily_close
        if current_price and current_price >= 5.0:
            continue
        if not snapshot or not daily_bars or not minute_bars:
            continue
        try:
            profile = get_company_profile(symbol)
            asset = get_alpaca_asset(symbol)
            ranked.append(analyze_symbol(symbol, snapshot, quote, daily_bars, minute_bars, spy_change_pct, profile, asset, spy_minute_bars, sector_snapshots, market_internals))
        except Exception:
            continue

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
    chart_pack = get_stock_chart_pack(best['symbol'])
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
        'watchlist': ranked[:WATCHLIST_SIZE],
        'ranked': ranked[:10],
        'chart_pack': chart_pack,
        'rules_applied': {
            'min_catalyst_score': MIN_CATALYST_SCORE,
            'no_buy_before_et': NO_BUY_BEFORE_ET,
            'opening_range_window_et': f'{OPENING_RANGE_START_ET}-{OPENING_RANGE_END_ET}',
            'max_spread_pct': MAX_SPREAD_PCT,
            'max_entry_extension_pct': MAX_ENTRY_EXTENSION_PCT,
            'max_dollar_loss_per_trade': MAX_DOLLAR_LOSS_PER_TRADE,
            'a_plus_score': A_PLUS_SCORE,
            'a_score': A_SCORE,
            'min_premarket_gap_pct': MIN_PREMARKET_GAP_PCT,
            'min_premarket_dollar_vol': MIN_PREMARKET_DOLLAR_VOL,
            'market_internals_block_enabled': MARKET_INTERNALS_BLOCK_ENABLED,
        },
    }
