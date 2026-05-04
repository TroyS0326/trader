from __future__ import annotations

from datetime import datetime
from typing import Dict

from config import LUNCH_BLOCK_END, LUNCH_BLOCK_START


def time_bucket(time_et: datetime) -> str:
    minutes = time_et.hour * 60 + time_et.minute
    if 9 * 60 + 30 <= minutes < 11 * 60:
        return 'morning'
    if 11 * 60 <= minutes < 15 * 60:
        return 'midday'
    if 15 * 60 <= minutes <= 16 * 60:
        return 'power_hour'
    return 'other'


def is_lunch_block(time_et: datetime) -> bool:
    sh, sm = map(int, LUNCH_BLOCK_START.split(':'))
    eh, em = map(int, LUNCH_BLOCK_END.split(':'))
    start = time_et.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end = time_et.replace(hour=eh, minute=em, second=0, microsecond=0)
    return start <= time_et <= end


def regime_trade_decision(model_scores: Dict[str, int], time_et: datetime, relative_strength_vs_spy: float) -> str:
    if is_lunch_block(time_et):
        return 'WATCH'
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


def momentum_trade_decision(model_scores: Dict[str, int], time_et: datetime, relative_strength_vs_spy: float, momentum_meta: Dict[str, float]) -> str:
    _ = model_scores, relative_strength_vs_spy
    if momentum_meta.get('data_stale'):
        return 'DATA STALE'
    if momentum_meta.get('below_stop'):
        return 'SETUP BROKEN: BELOW STOP'
    if momentum_meta.get('vwap_failure'):
        return 'SETUP BROKEN: VWAP FAILURE'
    if momentum_meta.get('buy_window_closed'):
        return 'NO TRADE'
    if momentum_meta.get('too_extended') and not momentum_meta.get('pullback_reclaim'):
        return 'WATCH FOR PULLBACK'
    if momentum_meta.get('day_change_pct', 0) >= momentum_meta.get('min_day_change_pct', 40) and momentum_meta.get('rvol', 0) >= momentum_meta.get('min_rvol', 3) and momentum_meta.get('above_vwap') and momentum_meta.get('spread_ok'):
        return 'BUY NOW'
    if momentum_meta.get('breakout_ready'):
        return 'WATCH FOR BREAKOUT'
    return 'NO TRADE'
