import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from db import insert_trade_audit_log


APPROVED_SCAN_TTL_SECONDS = 60 * 60 * 8  # 8 hours / trading day


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_symbol(symbol: Any) -> str:
    return str(symbol or "").upper().strip()


def _extract_allowed_symbols(scan_payload: Dict[str, Any]) -> List[str]:
    """
    Builds the list of symbols the bot is allowed to trade from the dashboard scan.
    This includes the best_pick plus any watchlist symbols returned by the scan.
    """
    symbols = set()

    best_pick = scan_payload.get("best_pick") or {}
    best_symbol = _normalize_symbol(best_pick.get("symbol"))
    if best_symbol:
        symbols.add(best_symbol)

    for item in scan_payload.get("watchlist") or []:
        symbol = _normalize_symbol(item.get("symbol"))
        if symbol:
            symbols.add(symbol)

    return sorted(symbols)


def approved_scan_key(user_id: int) -> str:
    return f"approved_scan:{user_id}"


def latest_scan_key(user_id: int) -> str:
    return f"latest_scan:{user_id}"


def approve_scan_for_user(redis_client, user, scan_payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Called after /api/scan succeeds.
    This scan becomes the user's approved auto-trading plan.
    """
    scan_id = scan_payload.get("scan_id")
    allowed_symbols = _extract_allowed_symbols(scan_payload)

    approval = {
        "user_id": int(user.id),
        "scan_id": int(scan_id) if scan_id is not None else None,
        "approved_at": _now_iso(),
        "trading_mode_at_scan": getattr(user, "trading_mode", "paper"),
        "subscription_status_at_scan": getattr(user, "subscription_status", "free"),
        "allowed_symbols": allowed_symbols,
        "best_symbol": _normalize_symbol((scan_payload.get("best_pick") or {}).get("symbol")),
    }

    redis_client.setex(
        approved_scan_key(user.id),
        APPROVED_SCAN_TTL_SECONDS,
        json.dumps(approval),
    )

    redis_client.setex(
        latest_scan_key(user.id),
        APPROVED_SCAN_TTL_SECONDS,
        json.dumps(scan_payload),
    )

    return approval


def validate_execution_against_approved_scan(
    redis_client,
    user,
    symbol: str,
    scan_id: Any,
) -> Dict[str, Any]:
    """
    Hard live-money gate.

    Paper mode is allowed through.
    Live mode must match the user's latest approved scan.
    """
    symbol = _normalize_symbol(symbol)
    trading_mode = getattr(user, "trading_mode", "paper")
    subscription_status = getattr(user, "subscription_status", "free")

    if trading_mode != "live":
        return {
            "ok": True,
            "mode": trading_mode,
            "reason": "PAPER_OR_NON_LIVE_MODE",
        }

    if subscription_status != "pro":
        return {
            "ok": False,
            "status": 403,
            "error": "LIVE_BLOCKED: User is not PRO.",
        }

    if not getattr(user, "alpaca_live_access_token", None):
        return {
            "ok": False,
            "status": 403,
            "error": "LIVE_BLOCKED: No Alpaca account connected.",
        }

    if not scan_id:
        return {
            "ok": False,
            "status": 400,
            "error": "LIVE_BLOCKED: Missing scan_id. Run a fresh dashboard scan first.",
        }

    raw = redis_client.get(approved_scan_key(user.id))
    if not raw:
        return {
            "ok": False,
            "status": 400,
            "error": "LIVE_BLOCKED: No approved scan found for this user. Click Scan again.",
        }

    try:
        approval = json.loads(raw)
    except json.JSONDecodeError:
        return {
            "ok": False,
            "status": 500,
            "error": "LIVE_BLOCKED: Approved scan cache is corrupted. Click Scan again.",
        }

    try:
        incoming_scan_id = int(scan_id)
        approved_scan_id = int(approval.get("scan_id"))
    except (TypeError, ValueError):
        return {
            "ok": False,
            "status": 400,
            "error": "LIVE_BLOCKED: Invalid scan_id.",
        }

    if incoming_scan_id != approved_scan_id:
        return {
            "ok": False,
            "status": 400,
            "error": (
                f"LIVE_BLOCKED: Scan mismatch. "
                f"Incoming scan_id={incoming_scan_id}, approved scan_id={approved_scan_id}."
            ),
        }

    allowed_symbols = approval.get("allowed_symbols") or []
    if symbol not in allowed_symbols:
        return {
            "ok": False,
            "status": 400,
            "error": (
                f"LIVE_BLOCKED: Symbol {symbol} is not in the approved scan. "
                f"Allowed symbols: {', '.join(allowed_symbols) or 'none'}."
            ),
        }

    return {
        "ok": True,
        "mode": "live",
        "reason": "LIVE_APPROVED_BY_SCAN",
        "approval": approval,
    }


def audit_trade_log(
    logger: logging.Logger,
    user,
    symbol: str,
    scan_id: Any,
    qty: Any,
    entry_price: Any,
    stop_price: Any,
    target_1: Any,
    target_2: Any,
    order_result: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Stores every trade attempt in two places:

    1. Server logs with TRADE_AUDIT
    2. SQLite table trade_audit_logs
    """
    order_result = order_result or {}

    payload = {
        "created_at": _now_iso(),
        "user_id": getattr(user, "id", None),
        "email": getattr(user, "email", None),
        "trading_mode": getattr(user, "trading_mode", None),
        "subscription_status": getattr(user, "subscription_status", None),
        "symbol": _normalize_symbol(symbol),
        "scan_id": scan_id,
        "qty": qty,
        "entry_price": entry_price,
        "stop_price": stop_price,
        "target_1": target_1,
        "target_2": target_2,
        "order_id": order_result.get("id"),
        "order_status": order_result.get("status"),
        "raw_json": {
            "order_result": order_result,
        },
    }

    try:
        audit_id = insert_trade_audit_log(payload)
    except Exception as exc:
        audit_id = None
        logger.exception("TRADE_AUDIT_DB_INSERT_FAILED error=%s payload=%s", exc, payload)

    logger.warning(
        "TRADE_AUDIT audit_id=%s user_id=%s email=%s mode=%s pro_status=%s "
        "symbol=%s scan_id=%s qty=%s entry=%s stop=%s target_1=%s target_2=%s "
        "order_id=%s order_status=%s",
        audit_id,
        payload["user_id"],
        payload["email"],
        payload["trading_mode"],
        payload["subscription_status"],
        payload["symbol"],
        payload["scan_id"],
        payload["qty"],
        payload["entry_price"],
        payload["stop_price"],
        payload["target_1"],
        payload["target_2"],
        payload["order_id"],
        payload["order_status"],
    )
