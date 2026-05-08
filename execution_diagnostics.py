import os
from typing import Any, Dict, List, Optional



def buy_window_open() -> bool:
    from scanner import buy_window_open as _buy_window_open
    return _buy_window_open()


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
    return not onboarding_missing_codes(user, str(getattr(user, "trading_mode", "paper") or "paper").strip().lower(), True)


def user_has_alpaca_paper_connection(user: Any) -> bool:
    return bool(getattr(user, "alpaca_paper_account_id", None) or getattr(user, "alpaca_paper_access_token", None))


def user_has_alpaca_live_connection(user: Any) -> bool:
    return bool(getattr(user, "alpaca_live_account_id", None) or getattr(user, "alpaca_live_access_token", None))


def has_paper_token(user: Any) -> bool:
    return bool(
        getattr(user, "alpaca_paper_access_token", None)
        or (str(getattr(user, "trading_mode", "paper") or "paper").strip().lower() == "paper" and getattr(user, "alpaca_access_token", None))
    )


def has_live_token(user: Any) -> bool:
    return bool(
        getattr(user, "alpaca_live_access_token", None)
        or (str(getattr(user, "trading_mode", "paper") or "paper").strip().lower() == "live" and getattr(user, "alpaca_access_token", None))
    )


def onboarding_missing_codes(user: Any, trading_mode: str, require_onboarding_completed: bool) -> List[str]:
    codes: List[str] = []
    if not user_has_alpaca_paper_connection(user):
        codes.append("PAPER_NOT_CONNECTED")
    if trading_mode == "live" and not user_has_alpaca_live_connection(user):
        codes.append("LIVE_NOT_CONNECTED")
    if not bool(getattr(user, "paper_bankroll_set", False)):
        codes.append("PAPER_BANKROLL_NOT_SET")
    if (getattr(user, "paper_bankroll", 0) or 0) <= 0:
        codes.append("PAPER_BANKROLL_ZERO")
    if require_onboarding_completed and not bool(getattr(user, "onboarding_completed", False)):
        codes.append("ONBOARDING_NOT_COMPLETED")
    if hasattr(user, "playbook_reviewed") and not bool(getattr(user, "playbook_reviewed", False)):
        codes.append("PLAYBOOK_NOT_REVIEWED")
    if hasattr(user, "transparency_reviewed") and not bool(getattr(user, "transparency_reviewed", False)):
        codes.append("TRANSPARENCY_NOT_REVIEWED")
    if hasattr(user, "broker_connection_started") and not bool(getattr(user, "broker_connection_started", False)):
        codes.append("BROKER_CONNECTION_NOT_STARTED")
    return codes


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


def _evaluate_paper_setup_readiness(user: Any) -> Dict[str, Any]:
    reasons: List[Dict[str, str]] = []

    def add(code: str, message: str):
        reasons.append({"code": code, "message": message})

    has_paper_mode_token = has_paper_token(user)
    if not user_has_alpaca_paper_connection(user):
        add("PAPER_NOT_CONNECTED", "Missing requirement: PAPER_NOT_CONNECTED")
    if not has_paper_mode_token:
        add("NO_ACTIVE_ALPACA_TOKEN", "No active Alpaca token for paper mode.")
    if not bool(getattr(user, "paper_bankroll_set", False)):
        add("PAPER_BANKROLL_NOT_SET", "Missing requirement: PAPER_BANKROLL_NOT_SET")
    if (getattr(user, "paper_bankroll", 0) or 0) <= 0:
        add("PAPER_BANKROLL_ZERO", "Missing requirement: PAPER_BANKROLL_ZERO")
    if hasattr(user, "playbook_reviewed") and not bool(getattr(user, "playbook_reviewed", False)):
        add("PLAYBOOK_NOT_REVIEWED", "Missing requirement: PLAYBOOK_NOT_REVIEWED")
    if hasattr(user, "transparency_reviewed") and not bool(getattr(user, "transparency_reviewed", False)):
        add("TRANSPARENCY_NOT_REVIEWED", "Missing requirement: TRANSPARENCY_NOT_REVIEWED")
    if hasattr(user, "broker_connection_started") and not bool(getattr(user, "broker_connection_started", False)):
        add("BROKER_CONNECTION_NOT_STARTED", "Missing requirement: BROKER_CONNECTION_NOT_STARTED")

    return {"paper_setup_ready": not reasons, "paper_setup_blocked_reasons": reasons}


def _evaluate_live_onboarding_readiness(user: Any) -> Dict[str, Any]:
    reasons: List[Dict[str, str]] = []

    def add(code: str, message: str):
        reasons.append({"code": code, "message": message})

    require_onboarding = env_bool("CENTRAL_SCANNER_REQUIRE_COMPLETED_ONBOARDING", True)
    has_live_mode_token = has_live_token(user)
    if not user_has_alpaca_live_connection(user):
        add("LIVE_NOT_CONNECTED", "Missing requirement: LIVE_NOT_CONNECTED")
    if not has_live_mode_token:
        add("NO_ACTIVE_ALPACA_TOKEN", "No active Alpaca token for live mode.")
    if require_onboarding and not bool(getattr(user, "onboarding_completed", False)):
        add("LIVE_ONBOARDING_NOT_COMPLETED", "Missing requirement: LIVE_ONBOARDING_NOT_COMPLETED")

    return {"live_onboarding_ready": not reasons, "live_onboarding_blocked_reasons": reasons}


def _evaluate_mode_readiness(user: Any, scan_payload: Dict[str, Any], mode: str) -> Dict[str, Any]:
    reasons: List[Dict[str, str]] = []

    def add(code: str, message: str):
        reasons.append({"code": code, "message": message})

    exec_enabled = env_bool("CENTRAL_SCANNER_EXECUTION_ENABLED", False)
    live_exec_enabled = env_bool("CENTRAL_SCANNER_LIVE_EXECUTION_ENABLED", False)
    require_onboarding = env_bool("CENTRAL_SCANNER_REQUIRE_COMPLETED_ONBOARDING", True)
    window_open = buy_window_open()
    trading_mode = mode

    best_pick = extract_best_pick(scan_payload)
    decision = str(best_pick.get("decision") or best_pick.get("setup_grade") or "").upper().strip()
    order_fields = extract_order_fields(best_pick)
    qty = (order_fields or {}).get("qty")

    if not exec_enabled:
        add("EXECUTION_DISABLED", "CENTRAL_SCANNER_EXECUTION_ENABLED is not enabled.")
    if str(getattr(user, "subscription_status", "free") or "free").strip().lower() != "pro":
        add("NON_PRO_USER", "User subscription is not PRO.")
    mode_has_token = has_paper_token(user) if trading_mode == "paper" else has_live_token(user)
    if not mode_has_token:
        add("NO_ACTIVE_ALPACA_TOKEN", "No active Alpaca token for the selected trading mode.")

    if trading_mode == "paper":
        if not user_has_alpaca_paper_connection(user):
            add("PAPER_NOT_CONNECTED", "Missing requirement: PAPER_NOT_CONNECTED")
        if not bool(getattr(user, "paper_bankroll_set", False)):
            add("PAPER_BANKROLL_NOT_SET", "Missing requirement: PAPER_BANKROLL_NOT_SET")
        if (getattr(user, "paper_bankroll", 0) or 0) <= 0:
            add("PAPER_BANKROLL_ZERO", "Missing requirement: PAPER_BANKROLL_ZERO")
        if hasattr(user, "playbook_reviewed") and not bool(getattr(user, "playbook_reviewed", False)):
            add("PLAYBOOK_NOT_REVIEWED", "Missing requirement: PLAYBOOK_NOT_REVIEWED")
        if hasattr(user, "transparency_reviewed") and not bool(getattr(user, "transparency_reviewed", False)):
            add("TRANSPARENCY_NOT_REVIEWED", "Missing requirement: TRANSPARENCY_NOT_REVIEWED")
        if hasattr(user, "broker_connection_started") and not bool(getattr(user, "broker_connection_started", False)):
            add("BROKER_CONNECTION_NOT_STARTED", "Missing requirement: BROKER_CONNECTION_NOT_STARTED")
        if any(r["code"] in {"PAPER_NOT_CONNECTED", "PAPER_BANKROLL_NOT_SET", "PAPER_BANKROLL_ZERO"} for r in reasons):
            add("PAPER_SETUP_INCOMPLETE", "Paper execution setup is incomplete.")
    elif trading_mode == "live":
        if not user_has_alpaca_live_connection(user):
            add("LIVE_NOT_CONNECTED", "Missing requirement: LIVE_NOT_CONNECTED")
        if require_onboarding and not bool(getattr(user, "onboarding_completed", False)):
            add("LIVE_ONBOARDING_NOT_COMPLETED", "Missing requirement: LIVE_ONBOARDING_NOT_COMPLETED")

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
        "order_fields": order_fields,
        "trading_mode": trading_mode,
        "execution_enabled": exec_enabled,
        "live_execution_enabled": live_exec_enabled,
        "buy_window_open": window_open,
    }


def evaluate_execution_readiness(user: Any, scan_payload: Dict[str, Any]) -> Dict[str, Any]:
    active_mode = str(getattr(user, "trading_mode", "paper") or "paper").strip().lower()
    paper_diag = _evaluate_mode_readiness(user, scan_payload, "paper")
    live_diag = _evaluate_mode_readiness(user, scan_payload, "live")
    active_diag = paper_diag if active_mode == "paper" else live_diag
    paper_setup_diag = _evaluate_paper_setup_readiness(user)
    live_onboarding_diag = _evaluate_live_onboarding_readiness(user)
    return {
        **active_diag,
        "active_mode": active_mode,
        "paper_execution_ready": paper_diag["execution_ready"],
        "live_execution_ready": live_diag["execution_ready"],
        "paper_blocked_reasons": paper_diag["blocked_reasons"],
        "live_blocked_reasons": live_diag["blocked_reasons"],
        "active_mode_blocked_reasons": active_diag["blocked_reasons"],
        "paper_setup_ready": paper_setup_diag["paper_setup_ready"],
        "paper_setup_blocked_reasons": paper_setup_diag["paper_setup_blocked_reasons"],
        "live_onboarding_ready": live_onboarding_diag["live_onboarding_ready"],
        "live_onboarding_blocked_reasons": live_onboarding_diag["live_onboarding_blocked_reasons"],
    }
