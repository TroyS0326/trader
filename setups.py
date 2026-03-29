from __future__ import annotations

from typing import Any, Callable, Dict, List


def detect_orb(
    minute_bars: List[Dict[str, Any]],
    filter_bars_in_et_window: Callable[[List[Dict[str, Any]], str, str], List[Dict[str, Any]]],
    filter_bars_for_today_session: Callable[[List[Dict[str, Any]]], List[Dict[str, Any]]],
    safe_num: Callable[[Any, float], float],
    opening_start_et: str,
    opening_end_et: str,
) -> Dict[str, Any]:
    session = filter_bars_for_today_session(minute_bars)
    opening = filter_bars_in_et_window(session, opening_start_et, opening_end_et)
    if not opening:
        return {'has_orb': False, 'or_high': None, 'or_low': None, 'breakout_attempts': 0}
    or_high = max(safe_num(b.get('h')) for b in opening)
    or_low = min(safe_num(b.get('l')) for b in opening)
    attempts = count_breakout_attempts(session, or_high, safe_num)
    return {
        'has_orb': True,
        'or_high': round(or_high, 4),
        'or_low': round(or_low, 4),
        'breakout_attempts': attempts,
    }


def count_breakout_attempts(session_bars: List[Dict[str, Any]], breakout_level: float, safe_num: Callable[[Any, float], float]) -> int:
    if breakout_level <= 0:
        return 0
    attempts = 0
    was_below = True
    for bar in session_bars:
        close = safe_num(bar.get('c'))
        if was_below and close >= breakout_level:
            attempts += 1
            was_below = False
        elif close < breakout_level:
            was_below = True
    return attempts
