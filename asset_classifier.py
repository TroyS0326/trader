from __future__ import annotations

import re
from typing import Any, Dict, Optional

BROAD_ETF_SYMBOLS = {"SPY", "QQQ", "IWM", "DIA"}
CRYPTO_KEYWORDS = ("bitcoin", "btc", "ether", "eth", "crypto", "blockchain")
LEVERAGED_HINTS = ("2x", "3x", "ultra", "leveraged", "bull")
INVERSE_HINTS = ("inverse", "short", "bear", "-1x", "-2x", "-3x")

OPTION_SYMBOL_PATTERN = re.compile(r"^[A-Z]{1,6}\d{6}[CP]\d{8}$")


def _text(asset: Dict[str, Any], profile: Dict[str, Any]) -> str:
    return " ".join(str(v or "") for v in [asset.get("name"), profile.get("name"), profile.get("finnhubIndustry")]).lower()


def classify_asset(symbol: str, asset: Optional[Dict[str, Any]], profile: Optional[Dict[str, Any]], *, platform_flags: Dict[str, bool], user_flags: Dict[str, bool]) -> Dict[str, Any]:
    symbol = (symbol or "").upper().strip()
    asset = asset or {}
    profile = profile or {}
    text = _text(asset, profile)

    if OPTION_SYMBOL_PATTERN.match(symbol) or " option " in text:
        asset_type = "OPTION"
        reason = "option_like_symbol"
    elif symbol in BROAD_ETF_SYMBOLS:
        asset_type = "BROAD_ETF"
        reason = "known_broad_etf"
    elif any(k in text for k in CRYPTO_KEYWORDS):
        asset_type = "CRYPTO_ETF" if asset.get("class") == "us_equity" else "UNKNOWN"
        reason = "crypto_keyword_match"
    elif asset.get("exchange") and ("etf" in text or asset.get("tradable") is True and symbol.endswith("Q")):
        if any(k in text for k in INVERSE_HINTS):
            asset_type = "INVERSE_ETF"
            reason = "inverse_keyword_match"
        elif any(k in text for k in LEVERAGED_HINTS):
            asset_type = "LEVERAGED_ETF"
            reason = "leveraged_keyword_match"
        else:
            asset_type = "BROAD_ETF"
            reason = "etf_metadata_or_name"
    elif asset.get("class") == "us_equity" or (symbol.isalpha() and 1 <= len(symbol) <= 5):
        asset_type = "LOW_FLOAT_MOMENTUM_STOCK" if "biotech" in text else "COMMON_STOCK"
        reason = "equity_default" if asset.get("class") == "us_equity" else "symbol_equity_fallback"
    else:
        asset_type = "UNKNOWN"
        reason = "insufficient_metadata"

    platform_allowed = {
        "COMMON_STOCK": True,
        "LOW_FLOAT_MOMENTUM_STOCK": platform_flags.get("biotech", True),
        "BROAD_ETF": platform_flags.get("etf", True),
        "CRYPTO_ETF": platform_flags.get("crypto_etf", True),
        "LEVERAGED_ETF": platform_flags.get("leveraged_etf", False),
        "INVERSE_ETF": platform_flags.get("inverse_etf", False),
        "OPTION": platform_flags.get("options", False),
        "UNKNOWN": False,
    }.get(asset_type, False)
    user_allowed = {
        "COMMON_STOCK": True,
        "LOW_FLOAT_MOMENTUM_STOCK": user_flags.get("biotech", True),
        "BROAD_ETF": user_flags.get("etf", True),
        "CRYPTO_ETF": user_flags.get("crypto_etf", True),
        "LEVERAGED_ETF": user_flags.get("leveraged_etf", False),
        "INVERSE_ETF": user_flags.get("inverse_etf", False),
        "OPTION": user_flags.get("options", False),
        "UNKNOWN": False,
    }.get(asset_type, False)
    tradable = platform_allowed and user_allowed and asset_type not in {"OPTION", "UNKNOWN"}
    rejection_reason = None
    if asset_type == "OPTION":
        rejection_reason = "options_not_supported_yet"
    elif not tradable:
        rejection_reason = "not_tradeable_by_xeanvi"

    return {
        "asset_type": asset_type,
        "asset_type_reason": reason,
        "platform_allowed": platform_allowed,
        "user_allowed": user_allowed,
        "tradable_by_xeanvi": tradable,
        "rejection_reason": rejection_reason,
        "rejection_reasons": [rejection_reason] if rejection_reason else [],
    }
