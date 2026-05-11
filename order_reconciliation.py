import argparse
import json
from datetime import datetime, timezone, timedelta
from typing import Any

import config
import db
from app import app
from broker import (
    BrokerError,
    extract_open_sell_order_coverage,
    get_open_orders,
    get_open_position,
    get_order,
    parse_broker_error_json,
    place_emergency_exit_order,
)
from models import User

TERMINAL_BROKER_STATUSES = {"canceled", "expired", "rejected", "done_for_day"}
PENDING_REPAIR_STATUSES = {"new", "accepted", "pending_new", "partially_filled"}
FAILED_REPAIR_STATUSES = {"canceled", "expired", "rejected", "done_for_day"}


def _is_not_found_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "404" in text or "not found" in text


def _position_qty(position: Any) -> int:
    try:
        return int(float((position or {}).get("qty") or 0))
    except Exception:
        return 0


def _extract_emergency(raw: Any) -> tuple[str | None, str | None]:
    if not isinstance(raw, dict):
        return None, None
    order = raw.get("emergency_exit_order") if isinstance(raw.get("emergency_exit_order"), dict) else {}
    eid = order.get("id") or raw.get("emergency_exit_order_id")
    status = (raw.get("emergency_exit_status") or order.get("status") or "").strip().lower() or None
    return eid, status


def reconcile_active_trade_orders(user_id: int | None = None, limit: int = 100) -> dict:
    summary = {"checked_count": 0, "updated_count": 0, "marked_stale_count": 0, "skipped_count": 0, "error_count": 0,
               "position_checked_count": 0, "no_position_count": 0, "unprotected_position_count": 0,
               "grouped_position_count": 0, "emergency_exit_skipped_existing_count": 0,
               "emergency_exit_filled_count": 0, "emergency_exit_failed_count": 0,
               "emergency_exit_retry_blocked_count": 0, "emergency_exit_submitted_count": 0,
               "emergency_exit_error_count": 0, "emergency_exit_existing_held_count": 0,
               "existing_protective_order_count": 0, "partial_existing_coverage_count": 0,
               "emergency_exit_uncovered_qty_submitted_count": 0, "affected_orders": [], "affected_symbols": []}
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=config.ORDER_RECONCILIATION_STALE_MINUTES)

    with app.app_context():
        trades = db.get_active_trades(limit=limit, user_id=user_id)
        grouped: dict[tuple[int, str], list[dict]] = {}
        for trade in trades:
            summary["checked_count"] += 1
            order_id = trade.get("order_id")
            if not order_id:
                summary["skipped_count"] += 1
                continue
            key = (trade.get("user_id"), (trade.get("symbol") or "").upper())
            grouped.setdefault(key, []).append(trade)
            user = db.db.session.get(User, trade.get("user_id"))
            if not user:
                summary["skipped_count"] += 1
                continue
            try:
                order = get_order(order_id, token=getattr(user, "alpaca_access_token", None), user=user)
                status = (order.get("status") or "").strip().lower()
                filled_qty = db.numeric_filled_qty(order.get("filled_qty"))
                updates: dict[str, Any] = {
                    "order_status": status or trade.get("order_status"),
                    "status": "filled" if status == "filled" else "partially_filled" if filled_qty > 0 and status in TERMINAL_BROKER_STATUSES else status or trade.get("status"),
                    "filled_avg_price": order.get("filled_avg_price"), "filled_qty": order.get("filled_qty"),
                    "raw_json": {"reconciliation": {"latest_order": order}},
                }
                if status in TERMINAL_BROKER_STATUSES and filled_qty <= 0:
                    updates["outcome"] = status
                db.update_trade_status(order_id, updates)
                summary["updated_count"] += 1
                summary["affected_orders"].append(order_id)
                if trade.get("symbol"):
                    summary["affected_symbols"].append(trade.get("symbol"))
            except BrokerError as exc:
                created_dt = None
                if isinstance(trade.get("created_at"), str):
                    try:
                        created_dt = datetime.fromisoformat(trade["created_at"].replace("Z", "+00:00"))
                    except ValueError:
                        pass
                if _is_not_found_error(exc) and created_dt and created_dt <= cutoff:
                    db.mark_stale_active_trade(order_id, "broker_order_not_found", {"reconciliation": {"error": str(exc)}})
                    summary["marked_stale_count"] += 1
                elif _is_not_found_error(exc):
                    summary["skipped_count"] += 1
                else:
                    summary["error_count"] += 1; summary["skipped_count"] += 1
            except Exception:
                summary["error_count"] += 1; summary["skipped_count"] += 1

        for (uid, symbol), rows in grouped.items():
            summary["grouped_position_count"] += 1
            user = db.db.session.get(User, uid)
            if not user:
                summary["skipped_count"] += 1
                continue
            summary["position_checked_count"] += 1
            try:
                position = get_open_position(symbol, user=user, token=getattr(user, "alpaca_access_token", None))
            except Exception:
                summary["error_count"] += 1
                continue
            pos_qty = _position_qty(position)
            if position is None or pos_qty <= 0:
                db.update_trades_for_user_symbol(user_id=uid, symbol=symbol, updates={"status": "closed", "order_status": "closed", "outcome": "closed"}, raw_patch={"reconciliation": {"reason": "no_position_found"}}, notes_append="no_position_found")
                summary["no_position_count"] += 1
                continue

            any_unprotected = False
            existing_id = None
            existing_status = None
            for t in rows:
                raw = t.get("raw_json") or {}
                if isinstance(raw, str):
                    try: raw = json.loads(raw)
                    except Exception: raw = {}
                bundle = raw.get("order_bundle", {}) if isinstance(raw, dict) else {}
                has_managed = bool(bundle.get("target_1_order_id") or bundle.get("runner_stop_order_id"))
                if not has_managed:
                    any_unprotected = True
                eid, est = _extract_emergency(raw)
                if eid or est:
                    existing_id = existing_id or eid
                    existing_status = existing_status or est

            if not any_unprotected:
                continue
            summary["unprotected_position_count"] += 1

            def _close_group_from_exit(order: dict):
                fill_px = order.get("filled_avg_price")
                db.update_trades_for_user_symbol(user_id=uid, symbol=symbol, updates={"status": "closed", "order_status": "closed", "outcome": "closed", "exit_price": fill_px}, raw_patch={"emergency_exit_order": order, "emergency_exit_status": "filled", "emergency_exit_filled_at": order.get("filled_at") or datetime.now(timezone.utc).isoformat(), "reconciliation": {"reason": "existing_emergency_exit_filled"}}, notes_append="emergency_exit_filled")

            if existing_id or (existing_status in PENDING_REPAIR_STATUSES):
                summary["emergency_exit_skipped_existing_count"] += 1
                if existing_id:
                    try:
                        existing_order = get_order(existing_id, token=getattr(user, "alpaca_access_token", None), user=user)
                        estatus = (existing_order.get("status") or "").lower()
                        if estatus == "filled":
                            _close_group_from_exit(existing_order); summary["emergency_exit_filled_count"] += 1
                            continue
                        elif estatus in FAILED_REPAIR_STATUSES:
                            summary["emergency_exit_failed_count"] += 1
                            db.update_trades_for_user_symbol(user_id=uid, symbol=symbol, updates={}, raw_patch={"emergency_exit_order": existing_order, "emergency_exit_status": estatus}, notes_append=f"emergency_exit_failed:{estatus}")
                            if not config.EMERGENCY_EXIT_RETRY_FAILED_ENABLED:
                                summary["emergency_exit_retry_blocked_count"] += 1
                                continue
                            existing_id = None
                        else:
                            db.update_trades_for_user_symbol(user_id=uid, symbol=symbol, updates={}, raw_patch={"emergency_exit_order": existing_order, "emergency_exit_status": estatus}, notes_append="emergency_exit_existing_active")
                            continue
                    except Exception:
                        continue
                else:
                    continue

            mode = getattr(user, "trading_mode", "paper")
            allowed = config.UNPROTECTED_POSITION_REPAIR_ENABLED if mode != "live" else config.UNPROTECTED_POSITION_REPAIR_LIVE_ENABLED
            if not allowed:
                continue
            open_orders = []
            try:
                open_orders = get_open_orders(symbol=symbol, user=user, token=getattr(user, "alpaca_access_token", None))
            except BrokerError:
                summary["error_count"] += 1
                continue
            coverage = extract_open_sell_order_coverage(open_orders, symbol)
            held_qty = int(coverage.get("held_qty") or 0)
            if coverage.get("order_ids"):
                summary["existing_protective_order_count"] += len(coverage["order_ids"])
            if held_qty >= pos_qty and held_qty > 0:
                db.update_trades_for_user_symbol(
                    user_id=uid, symbol=symbol, updates={},
                    raw_patch={
                        "existing_protective_order_ids": coverage.get("order_ids", []),
                        "existing_protective_orders": coverage.get("orders", []),
                        "emergency_exit_status": "held_by_existing_orders",
                        "held_for_orders_qty": held_qty,
                        "broker_position_qty": pos_qty,
                        "reconciliation": {"reason": "position_fully_held_by_existing_sell_orders"},
                    },
                    notes_append="emergency_exit_existing_held_orders",
                )
                summary["emergency_exit_skipped_existing_count"] += 1
                summary["emergency_exit_existing_held_count"] += 1
                continue
            submit_qty = pos_qty
            if 0 < held_qty < pos_qty:
                submit_qty = pos_qty - held_qty
                summary["partial_existing_coverage_count"] += 1
            try:
                emergency = place_emergency_exit_order(symbol, submit_qty, user, reason="reconciliation_unprotected_position", reference_order_id=rows[0].get("order_id"))
                db.update_trades_for_user_symbol(user_id=uid, symbol=symbol, updates={}, raw_patch={"emergency_exit_order": emergency, "emergency_exit_order_id": emergency.get("id"), "emergency_exit_status": emergency.get("status"), "existing_protective_order_ids": coverage.get("order_ids", [])}, notes_append="emergency_exit_submitted:reconciliation_unprotected_position")
                summary["emergency_exit_submitted_count"] += 1
                summary["emergency_exit_uncovered_qty_submitted_count"] += submit_qty
            except BrokerError as exc:
                message = str(exc)
                if "403" in message:
                    parsed = parse_broker_error_json(exc)
                    try:
                        available_qty = float(parsed.get("available"))
                        held_for_orders = float(parsed.get("held_for_orders"))
                        existing_qty = float(parsed.get("existing_qty"))
                    except Exception:
                        available_qty = held_for_orders = existing_qty = None
                    related_orders = parsed.get("related_orders") if isinstance(parsed.get("related_orders"), list) else []
                    if available_qty == 0 and held_for_orders is not None and existing_qty is not None and held_for_orders >= existing_qty and related_orders:
                        db.update_trades_for_user_symbol(user_id=uid, symbol=symbol, updates={}, raw_patch={
                            "emergency_exit_status": "held_by_existing_orders",
                            "existing_protective_order_ids": related_orders,
                            "held_for_orders_qty": held_for_orders,
                            "broker_position_qty": existing_qty,
                            "broker_available_qty": available_qty,
                            "emergency_exit_403_payload": parsed,
                            "reconciliation": {"reason": "broker_reports_position_held_for_orders"},
                        }, notes_append="emergency_exit_existing_held_orders")
                        summary["emergency_exit_existing_held_count"] += 1
                        summary["emergency_exit_skipped_existing_count"] += 1
                        continue
                    summary["emergency_exit_error_count"] += 1
                    try:
                        refetched = get_open_position(symbol, user=user, token=getattr(user, "alpaca_access_token", None))
                    except Exception:
                        refetched = position
                    if refetched is None or _position_qty(refetched) <= 0:
                        db.update_trades_for_user_symbol(user_id=uid, symbol=symbol, updates={"status": "closed", "order_status": "closed", "outcome": "closed"}, raw_patch={"reconciliation": {"reason": "no_position_found"}}, notes_append="no_position_found")
                    else:
                        db.update_trades_for_user_symbol(user_id=uid, symbol=symbol, updates={}, raw_patch={"emergency_exit_error": message, "broker_error_status_or_text": message, "unprotected_position_detected": True}, notes_append="emergency_exit_error")
                else:
                    summary["emergency_exit_error_count"] += 1
                    db.update_trades_for_user_symbol(user_id=uid, symbol=symbol, updates={}, raw_patch={"emergency_exit_error": message}, notes_append="emergency_exit_failed:reconciliation_unprotected_position")
            except Exception as exc:
                summary["emergency_exit_error_count"] += 1
                db.update_trades_for_user_symbol(user_id=uid, symbol=symbol, updates={}, raw_patch={"emergency_exit_error": str(exc)}, notes_append="emergency_exit_failed:reconciliation_unprotected_position")

    summary["affected_orders"] = sorted(set(summary["affected_orders"]))
    summary["affected_symbols"] = sorted(set(summary["affected_symbols"]))
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-id", type=int, default=None)
    parser.add_argument("--limit", type=int, default=config.ORDER_RECONCILIATION_ACTIVE_LIMIT)
    args = parser.parse_args()
    print(json.dumps(reconcile_active_trade_orders(user_id=args.user_id, limit=args.limit), sort_keys=True))


if __name__ == "__main__":
    main()
