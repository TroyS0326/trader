from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

from config import TIMEZONE_LABEL


def safe_num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_hhmm(value: str) -> tuple[int, int]:
    hh, mm = [int(x) for x in value.split(':', 1)]
    return hh, mm


def _bar_dt_et(bar: Dict[str, Any]) -> datetime | None:
    ts = bar.get('t', '')
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace('Z', '+00:00')).astimezone(ZoneInfo(TIMEZONE_LABEL))
    except Exception:
        return None


def filter_bars_for_today_session(minute_bars: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not minute_bars:
        return []
    latest_dt = _bar_dt_et(minute_bars[-1])
    if not latest_dt:
        return []
    target_date = latest_dt.date()

    out: List[Dict[str, Any]] = []
    for bar in minute_bars:
        dt = _bar_dt_et(bar)
        if not dt or dt.date() != target_date:
            continue
        mins = dt.hour * 60 + dt.minute
        if 9 * 60 + 30 <= mins <= 16 * 60:
            out.append(bar)
    return out


def filter_bars_in_et_window(minute_bars: List[Dict[str, Any]], start_label: str, end_label: str) -> List[Dict[str, Any]]:
    start_h, start_m = _parse_hhmm(start_label)
    end_h, end_m = _parse_hhmm(end_label)
    start_min = start_h * 60 + start_m
    end_min = end_h * 60 + end_m

    if not minute_bars:
        return []
    latest_dt = _bar_dt_et(minute_bars[-1])
    if not latest_dt:
        return []
    target_date = latest_dt.date()

    out: List[Dict[str, Any]] = []
    for bar in minute_bars:
        dt = _bar_dt_et(bar)
        if not dt or dt.date() != target_date:
            continue
        mins = dt.hour * 60 + dt.minute
        if start_min <= mins < end_min:
            out.append(bar)
    return out


def calculate_user_kelly_fraction(user_id: int) -> float | None:
    """Return a conservative half-Kelly fraction for a user's resolved trades.

    Returns None when there are fewer than 10 resolved trades.
    Returns 0.0 when expected value is non-positive.
    """
    from models import Trade

    resolved = (
        Trade.query.filter(
            Trade.user_id == user_id,
            Trade.outcome.isnot(None),
        )
        .order_by(Trade.created_at.desc())
        .limit(200)
        .all()
    )

    if len(resolved) < 10:
        return None

    wins = [t for t in resolved if str(t.outcome).lower() in {'win', 'target_1', 'target_2', 'profit'}]
    losses = [t for t in resolved if str(t.outcome).lower() in {'loss', 'stop', 'stopped', 'stopped_out'}]

    total = len(wins) + len(losses)
    if total < 10:
        return None

    p_win = len(wins) / total
    p_loss = 1.0 - p_win

    # Use target-1 RR as a conservative proxy for average win multiple.
    avg_win_r = 0.0
    if wins:
        multiples = [float(t.rr_ratio_1) for t in wins if t.rr_ratio_1 is not None and t.rr_ratio_1 > 0]
        avg_win_r = sum(multiples) / len(multiples) if multiples else 1.0

    if avg_win_r <= 0:
        return 0.0

    kelly_full = p_win - (p_loss / avg_win_r)
    if kelly_full <= 0:
        return 0.0

    return max(0.0, 0.5 * kelly_full)
