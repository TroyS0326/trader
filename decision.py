from __future__ import annotations

from datetime import datetime
from typing import Dict


def time_bucket(time_et: datetime) -> str:
    minutes = time_et.hour * 60 + time_et.minute
    if 9 * 60 + 30 <= minutes <= 10 * 60 + 30:
        return 'morning'
    if 11 * 60 <= minutes <= 14 * 60:
        return 'midday'
    if 15 * 60 <= minutes <= 16 * 60:
        return 'power_hour'
    return 'other'


def regime_trade_decision(model_scores: Dict[str, int], time_et: datetime, relative_strength_vs_spy: float) -> str:
    bucket = time_bucket(time_et)
    if bucket == 'morning':
        if model_scores['opportunity'] > 80 and model_scores['tradability'] > 60:
            return 'BUY NOW'
        return 'WATCH FOR BREAKOUT'
    if bucket == 'midday':
        if model_scores['opportunity'] > 95 and model_scores['entry_quality'] > 90:
            return 'BUY NOW'
        return 'WATCH FOR BREAKOUT'
    if bucket == 'power_hour':
        if relative_strength_vs_spy > 2.0 and model_scores['tradability'] > 55:
            return 'BUY NOW'
        return 'WATCH FOR BREAKOUT'
    return 'WATCH FOR BREAKOUT'
