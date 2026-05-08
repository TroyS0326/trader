from __future__ import annotations

from typing import Any, Dict, List, Tuple
import os

BEST_PICK_KEYS = ("best_pick", "best", "top_pick")


def decision_allowlist_from_env() -> set[str]:
    raw = os.getenv("CENTRAL_SCANNER_EXECUTE_DECISIONS", "BUY NOW,A+,A")
    parsed = {item.strip().upper() for item in raw.split(",") if item.strip()}
    return parsed or {"BUY NOW", "A+", "A"}


def _to_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    fv = _to_float(value)
    if fv is None:
        return None
    try:
        return int(fv)
    except (TypeError, ValueError):
        return None


def _get_best_pick(result: Dict[str, Any]) -> Tuple[Dict[str, Any], str | None, List[str]]:
    notes: List[str] = []
    for key in BEST_PICK_KEYS:
        candidate = result.get(key)
        if isinstance(candidate, dict):
            return candidate, key, notes

    watchlist = result.get("watchlist")
    if isinstance(watchlist, list) and watchlist and isinstance(watchlist[0], dict):
        notes.append("No best_pick/best/top_pick key; watchlist[0] present.")
        return watchlist[0], "watchlist[0]", notes

    return {}, None, notes


def validate_scan_payload_contract(result: Dict[str, Any]) -> Dict[str, Any]:
    best_pick, key_used, notes = _get_best_pick(result or {})
    has_best_pick = bool(best_pick)

    normalized = {
        "symbol": best_pick.get("symbol") or best_pick.get("ticker"),
        "decision": best_pick.get("decision") or best_pick.get("setup_grade") or best_pick.get("action"),
        "qty": best_pick.get("qty") if best_pick.get("qty") is not None else best_pick.get("shares"),
        "entry_price": best_pick.get("entry_price") if best_pick.get("entry_price") is not None else best_pick.get("entry"),
        "stop_price": best_pick.get("stop_price") if best_pick.get("stop_price") is not None else best_pick.get("stop"),
        "target_1": best_pick.get("target_1") if best_pick.get("target_1") is not None else best_pick.get("target_1_price"),
        "target_2": best_pick.get("target_2") if best_pick.get("target_2") is not None else best_pick.get("target_2_price"),
    }

    missing_order_fields = [k for k in ("symbol", "qty", "entry_price", "stop_price", "target_1", "target_2") if normalized.get(k) in (None, "")]

    decision_raw = str(normalized.get("decision") or "").upper().strip()
    qty_int = _to_int(normalized.get("qty"))
    qty_valid = bool(qty_int is not None and qty_int >= 1)

    decision_is_executable = decision_raw in decision_allowlist_from_env()
    if decision_raw and not decision_is_executable:
        notes.append(f"Decision {decision_raw!r} is not executable.")

    if not qty_valid:
        notes.append("qty missing/invalid or below 1.")

    normalized_order_fields = {
        "symbol": str(normalized.get("symbol") or "").upper().strip() or None,
        "qty": qty_int,
        "entry_price": _to_float(normalized.get("entry_price")),
        "stop_price": _to_float(normalized.get("stop_price")),
        "target_1": _to_float(normalized.get("target_1")),
        "target_2": _to_float(normalized.get("target_2")),
    }

    executable_payload_ready = has_best_pick and not missing_order_fields and decision_is_executable and qty_valid and all(
        normalized_order_fields[k] not in (None, "") for k in ("symbol", "qty", "entry_price", "stop_price", "target_1", "target_2")
    )

    return {
        "has_best_pick": has_best_pick,
        "best_pick_key_used": key_used,
        "missing_order_fields": missing_order_fields,
        "normalized_order_fields": normalized_order_fields,
        "decision": decision_raw,
        "decision_is_executable": decision_is_executable,
        "qty_valid": qty_valid,
        "payload_shape_notes": notes,
        "executable_payload_ready": executable_payload_ready,
    }
