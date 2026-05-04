from __future__ import annotations

import logging
from typing import Any, Dict

import config
import market_state

logger = logging.getLogger(__name__)


def classify_orb_state(rvol: float, atr_expansion: float) -> Dict[str, str]:
    if (
        rvol >= config.DYNAMIC_ORB_RVOL_EXTREME_THRESHOLD
        or atr_expansion >= config.DYNAMIC_ORB_ATR_EXPANSION_EXTREME_THRESHOLD
    ):
        return {
            "mode": "extreme_volatility",
            "start_time_et": config.DYNAMIC_ORB_EXTREME_START_ET,
            "preferred_setup": "vwap_reclaim_or_first_pullback",
            "reason": "Extreme pre-market volatility detected. Aggressive ORB routing should be delayed and setup preference should shift toward VWAP reclaim or first pullback.",
        }
    if (
        rvol >= config.DYNAMIC_ORB_RVOL_DELAY_THRESHOLD
        or atr_expansion >= config.DYNAMIC_ORB_ATR_EXPANSION_DELAY_THRESHOLD
    ):
        return {
            "mode": "delayed",
            "start_time_et": config.DYNAMIC_ORB_DELAYED_START_ET,
            "preferred_setup": "vwap_reclaim",
            "reason": "Elevated pre-market volatility detected. ORB routing should be delayed to reduce opening whipsaw risk.",
        }
    return {
        "mode": "normal",
        "start_time_et": config.DYNAMIC_ORB_NORMAL_START_ET,
        "preferred_setup": "orb",
        "reason": "Normal pre-market volatility. Standard ORB timing state is active.",
    }


def build_dynamic_orb_state(rvol: float, atr_expansion: float) -> Dict[str, Any]:
    classification = classify_orb_state(float(rvol), float(atr_expansion))
    return {
        **classification,
        "rvol": float(rvol),
        "atr_expansion": float(atr_expansion),
        "generated_at_utc_ts": market_state.now_utc_ts(),
    }


def get_latest_dynamic_orb_state() -> Dict[str, Any]:
    try:
        state = market_state.get_market_state(market_state.DYNAMIC_ORB_STATE_NAME, default=None)
        if isinstance(state, dict) and state:
            return state
    except Exception as exc:
        logger.warning("Failed to get latest dynamic ORB state: %s", exc)
    return build_dynamic_orb_state(1.0, 1.0)


def get_dynamic_orb_start_time_et() -> str:
    return str(get_latest_dynamic_orb_state().get("start_time_et") or config.DYNAMIC_ORB_NORMAL_START_ET)


def get_dynamic_orb_preferred_setup() -> str:
    return str(get_latest_dynamic_orb_state().get("preferred_setup") or "orb")
