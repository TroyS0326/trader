from __future__ import annotations

from datetime import datetime
from typing import Dict

import config


def time_bucket(time_et: datetime) -> str:
    minutes = time_et.hour * 60 + time_et.minute
    if 9 * 60 + 30 <= minutes <= 10 * 60 + 30:
        return 'morning'
    if 10 * 60 + 31 <= minutes <= 14 * 60:
        return 'midday'
    if 14 * 60 + 1 <= minutes <= 16 * 60:
        return 'power_hour'
    return 'other'


def is_lunch_block(time_et: datetime) -> bool:
    sh, sm = map(int, config.LUNCH_BLOCK_START.split(':'))
    eh, em = map(int, config.LUNCH_BLOCK_END.split(':'))
    start = time_et.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end = time_et.replace(hour=eh, minute=em, second=0, microsecond=0)
    return start <= time_et <= end


def regime_trade_decision(model_scores: Dict[str, int], time_et: datetime, relative_strength_vs_spy: float) -> str:
    aggressive_enabled = config.AGGRESSIVE_INTRADAY_ENABLED
    if is_lunch_block(time_et) and (not aggressive_enabled or not config.AGGRESSIVE_ALLOW_LUNCH_TRADING):
        return 'WATCH'
    bucket = time_bucket(time_et)
    opportunity = model_scores.get('opportunity', 0)
    tradability = model_scores.get('tradability', 0)
    entry_quality = model_scores.get('entry_quality', 0)

    if bucket == 'morning':
        if aggressive_enabled:
            if (
                opportunity >= config.AGGRESSIVE_MORNING_MIN_OPPORTUNITY
                and tradability >= config.AGGRESSIVE_MORNING_MIN_TRADABILITY
                and entry_quality >= config.AGGRESSIVE_MORNING_MIN_ENTRY_QUALITY
            ):
                return 'BUY NOW'
        elif opportunity > 80 and tradability > 60:
            return 'BUY NOW'
        return 'WATCH FOR BREAKOUT'
    if bucket == 'midday':
        if aggressive_enabled:
            if (
                opportunity >= config.AGGRESSIVE_MIDDAY_MIN_OPPORTUNITY
                and tradability >= config.AGGRESSIVE_MIDDAY_MIN_TRADABILITY
                and entry_quality >= config.AGGRESSIVE_MIDDAY_MIN_ENTRY_QUALITY
            ):
                return 'BUY NOW'
        elif opportunity > 95 and entry_quality > 90:
            return 'BUY NOW'
        return 'WATCH FOR BREAKOUT'
    if bucket == 'power_hour':
        if aggressive_enabled:
            if (
                relative_strength_vs_spy >= config.AGGRESSIVE_POWER_HOUR_MIN_RS
                and tradability >= config.AGGRESSIVE_POWER_HOUR_MIN_TRADABILITY
            ):
                return 'BUY NOW'
        elif relative_strength_vs_spy > 2.0 and tradability > 55:
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
    if config.AGGRESSIVE_INTRADAY_ENABLED:
        if (
            momentum_meta.get('pullback_reclaim')
            and momentum_meta.get('above_vwap')
            and momentum_meta.get('spread_ok')
            and momentum_meta.get('rvol', 0) >= config.AGGRESSIVE_MOMENTUM_PULLBACK_MIN_RVOL
        ):
            return 'BUY NOW'
        if (
            momentum_meta.get('day_change_pct', 0) >= config.AGGRESSIVE_MOMENTUM_MIN_DAY_CHANGE_PCT
            and momentum_meta.get('rvol', 0) >= config.AGGRESSIVE_MOMENTUM_MIN_RVOL
            and momentum_meta.get('above_vwap')
            and momentum_meta.get('spread_ok')
        ):
            return 'BUY NOW'
    if momentum_meta.get('day_change_pct', 0) >= momentum_meta.get('min_day_change_pct', 40) and momentum_meta.get('rvol', 0) >= momentum_meta.get('min_rvol', 3) and momentum_meta.get('above_vwap') and momentum_meta.get('spread_ok'):
        return 'BUY NOW'
    if momentum_meta.get('breakout_ready'):
        return 'WATCH FOR BREAKOUT'
    return 'NO TRADE'
