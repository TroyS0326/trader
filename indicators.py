from __future__ import annotations

from statistics import mean
from typing import Any, Callable, Dict, List


def calc_spread_pct(bid: float, ask: float, price: float) -> float:
    if price <= 0 or bid <= 0 or ask <= 0 or ask < bid:
        return 0.0
    return (ask - bid) / price


def calc_vwap(minute_bars: List[Dict[str, Any]], safe_num: Callable[[Any, float], float]) -> float:
    total_pv = 0.0
    total_v = 0.0
    for bar in minute_bars:
        typical = (safe_num(bar.get('h')) + safe_num(bar.get('l')) + safe_num(bar.get('c'))) / 3.0
        vol = safe_num(bar.get('v'))
        total_pv += typical * vol
        total_v += vol
    return total_pv / total_v if total_v > 0 else 0.0


def calc_rvol(
    minute_bars: List[Dict[str, Any]],
    filter_session_bars: Callable[[List[Dict[str, Any]]], List[Dict[str, Any]]],
    bar_dt_et: Callable[[Dict[str, Any]], Any],
    safe_num: Callable[[Any, float], float],
    lookback_days: int = 3,
) -> float:
    if not minute_bars:
        return 0.0
    session = filter_session_bars(minute_bars)
    current_volume = sum(safe_num(b.get('v')) for b in session)
    if current_volume <= 0:
        return 0.0
    latest_dt = bar_dt_et(minute_bars[-1])
    if not latest_dt:
        return 0.0
    cutoff = latest_dt.hour * 60 + latest_dt.minute
    volumes_by_day: Dict[Any, float] = {}
    for bar in minute_bars:
        dt = bar_dt_et(bar)
        if not dt or dt.date() == latest_dt.date():
            continue
        mins = dt.hour * 60 + dt.minute
        if 9 * 60 + 30 <= mins <= cutoff:
            volumes_by_day[dt.date()] = volumes_by_day.get(dt.date(), 0.0) + safe_num(bar.get('v'))
    history = list(volumes_by_day.values())[-lookback_days:]
    baseline = mean(history) if history else 0.0
    return (current_volume / baseline) if baseline > 0 else 0.0


def calc_trend_efficiency(
    minute_bars: List[Dict[str, Any]],
    filter_session_bars: Callable[[List[Dict[str, Any]]], List[Dict[str, Any]]],
    safe_num: Callable[[Any, float], float],
    window: int = 30,
) -> float:
    session = filter_session_bars(minute_bars)
    closes = [safe_num(b.get('c')) for b in session[-window:] if safe_num(b.get('c')) > 0]
    if len(closes) < 3:
        return 0.0
    net_move = abs(closes[-1] - closes[0])
    path_len = sum(abs(closes[i] - closes[i - 1]) for i in range(1, len(closes)))
    return (net_move / path_len) if path_len > 0 else 0.0
