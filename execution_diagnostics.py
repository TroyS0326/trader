import os
from typing import Any, Dict, List, Optional

from scanner import buy_window_open


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def decision_allowlist() -> set[str]:
    raw = os.getenv("CENTRAL_SCANNER_EXECUTE_DECISIONS", "BUY NOW,A+,A")
    parsed = {item.strip().upper() for item in raw.split(",") if item.strip()}
    return parsed or {"BUY NOW", "A+", "A"}


def onboarding_complete(user: Any) -> bool:
    required_flags = (
        "onboarding_completed",
        "paper_bankroll_set",
        "playbook_reviewed",
        "transparency_reviewed",
        "broker_connection_started",
    )
    return all(bool(getattr(user, flag, False)) for flag in required_flags)


def extract_best_pick(scan_payload: Dict[str, Any]) -> Dict[str, Any]:
    return (scan_payload.get("best_pick") or scan_payload.get("best") or scan_payload.get("top_pick") or {})


def extract_order_fields(best_pick: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    symbol = best_pick.get("symbol")
    qty_raw = best_pick.get("qty")
    entry = best_pick.get("entry_price")
    stop = best_pick.get("stop_price")
    target_1 = best_pick.get("target_1", best_pick.get("target_1_price"))
    target_2 = best_pick.get("target_2", best_pick.get("target_2_price"))

    required_values = (symbol, qty_raw, entry, stop, target_1, target_2)
    if any(v in (None, "") for v in required_values):
        return None

    try:
        normalized = {
            "symbol": str(symbol).upper().strip(),
            "qty": int(float(qty_raw)),
            "entry_price": float(entry),
            "stop_price": float(stop),
            "target_1": float(target_1),
            "target_2": float(target_2),
        }
    except (TypeError, ValueError):
        return None

    return normalized


def evaluate_execution_readiness(user: Any, scan_payload: Dict[str, Any]) -> Dict[str, Any]:
    reasons: List[Dict[str, str]] = []

    def add(code: str, message: str):
        reasons.append({"code": code, "message": message})

    exec_enabled = env_bool("CENTRAL_SCANNER_EXECUTION_ENABLED", False)
    live_exec_enabled = env_bool("CENTRAL_SCANNER_LIVE_EXECUTION_ENABLED", False)
    require_onboarding = env_bool("CENTRAL_SCANNER_REQUIRE_COMPLETED_ONBOARDING", True)
    window_open = buy_window_open()
    trading_mode = str(getattr(user, "trading_mode", "paper") or "paper").strip().lower()

    best_pick = extract_best_pick(scan_payload)
    decision = str(best_pick.get("decision") or best_pick.get("setup_grade") or "").upper().strip()
    order_fields = extract_order_fields(best_pick)
    qty = (order_fields or {}).get("qty")

    if not exec_enabled:
        add("EXECUTION_DISABLED", "CENTRAL_SCANNER_EXECUTION_ENABLED is not enabled.")
    if str(getattr(user, "subscription_status", "free") or "free").strip().lower() != "pro":
        add("NON_PRO_USER", "User subscription is not PRO.")
    if not getattr(user, "alpaca_access_token", None):
        add("NO_ACTIVE_ALPACA_TOKEN", "No active Alpaca token for the selected trading mode.")
    if require_onboarding and not onboarding_complete(user):
        add("ONBOARDING_INCOMPLETE", "Required onboarding flags are incomplete.")
    if not window_open:
        add("BUY_WINDOW_CLOSED", "Current time is before NO_BUY_BEFORE_ET.")
    if decision not in decision_allowlist():
        add("DECISION_NOT_ELIGIBLE", "Decision is not in CENTRAL_SCANNER_EXECUTE_DECISIONS.")
    if trading_mode == "live" and not live_exec_enabled:
        add("LIVE_EXECUTION_DISABLED", "CENTRAL_SCANNER_LIVE_EXECUTION_ENABLED is not enabled for live mode.")
    if not order_fields:
        add("MISSING_ORDER_FIELDS", "Required order fields are missing or invalid.")
    elif order_fields["qty"] < 1:
        add("QTY_BELOW_1", "Order quantity is below 1 share.")

    return {
        "execution_ready": not reasons,
        "blocked_reasons": reasons,
        "decision": decision,
        "symbol": (order_fields or {}).get("symbol") or str(best_pick.get("symbol") or "").upper().strip(),
        "qty": qty,
        "trading_mode": trading_mode,
        "execution_enabled": exec_enabled,
        "live_execution_enabled": live_exec_enabled,
        "buy_window_open": window_open,
    }
