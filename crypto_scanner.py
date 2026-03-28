from __future__ import annotations

from datetime import datetime, timedelta, timezone
from statistics import mean
from typing import Any, Dict, List

import requests

from config import ALPACA_API_KEY, ALPACA_API_SECRET, ALPACA_DATA_BASE, CRYPTO_SCAN_ENABLED, CRYPTO_SYMBOLS

TIMEOUT = 20


def _headers() -> Dict[str, str]:
    if not ALPACA_API_KEY or not ALPACA_API_SECRET:
        return {}
    return {
        'accept': 'application/json',
        'APCA-API-KEY-ID': ALPACA_API_KEY,
        'APCA-API-SECRET-KEY': ALPACA_API_SECRET,
    }


def _safe_num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _get_crypto_bars(symbol: str, start: datetime, end: datetime) -> List[Dict[str, Any]]:
    resp = requests.get(
        f'{ALPACA_DATA_BASE}/v1beta3/crypto/us/bars',
        params={
            'symbols': symbol,
            'timeframe': '1Min',
            'start': start.isoformat(),
            'end': end.isoformat(),
            'limit': 500,
            'sort': 'asc',
        },
        headers=_headers(),
        timeout=TIMEOUT,
    )
    if resp.status_code >= 400:
        return []
    data = resp.json()
    return (data.get('bars') or {}).get(symbol, [])


def _calc_vwap(bars: List[Dict[str, Any]]) -> float:
    pv = 0.0
    vol = 0.0
    for b in bars:
        typical = (_safe_num(b.get('h')) + _safe_num(b.get('l')) + _safe_num(b.get('c'))) / 3.0
        v = _safe_num(b.get('v'))
        pv += typical * v
        vol += v
    return (pv / vol) if vol else 0.0


def run_crypto_scan() -> List[Dict[str, Any]]:
    if not CRYPTO_SCAN_ENABLED:
        return []
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=6)
    ranked: List[Dict[str, Any]] = []
    for symbol in CRYPTO_SYMBOLS:
        bars = _get_crypto_bars(symbol, start, end)
        if len(bars) < 90:
            continue
        recent = bars[-60:]
        opening = recent[:30]
        current = _safe_num(recent[-1].get('c'))
        or_high = max(_safe_num(b.get('h')) for b in opening)
        or_low = min(_safe_num(b.get('l')) for b in opening)
        vwap = _calc_vwap(recent)
        volumes = [_safe_num(b.get('v')) for b in recent[-10:]]
        avg_vol = mean(volumes) if volumes else 0.0
        breakout = current > or_high and current >= vwap
        score = 1
        if breakout and avg_vol > 0:
            score = 5
        elif current >= vwap and current >= or_high * 0.997:
            score = 4
        elif current >= vwap:
            score = 3
        ranked.append(
            {
                'symbol': symbol,
                'score': score,
                'current_price': round(current, 6),
                'or_high': round(or_high, 6),
                'or_low': round(or_low, 6),
                'vwap': round(vwap, 6),
                'breakout': breakout,
            }
        )
    ranked.sort(key=lambda x: (x['score'], x['breakout'], x['current_price']), reverse=True)
    return ranked[:10]
