import argparse
import json
from datetime import datetime, timezone, timedelta
from typing import Any

import config
import db
from app import app
from broker import BrokerError, get_order
from models import User

TERMINAL_BROKER_STATUSES = {"canceled", "expired", "rejected", "done_for_day"}


def _is_not_found_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "404" in text or "not found" in text


def reconcile_active_trade_orders(user_id: int | None = None, limit: int = 100) -> dict:
    summary = {
        "checked_count": 0,
        "updated_count": 0,
        "marked_stale_count": 0,
        "skipped_count": 0,
        "error_count": 0,
        "affected_orders": [],
        "affected_symbols": [],
    }
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=config.ORDER_RECONCILIATION_STALE_MINUTES)

    with app.app_context():
        trades = db.get_active_trades(limit=limit, user_id=user_id)
        for trade in trades:
            summary["checked_count"] += 1
            order_id = trade.get("order_id")
            if not order_id:
                summary["skipped_count"] += 1
                continue
            user = db.db.session.get(User, trade.get("user_id"))
            if not user:
                summary["skipped_count"] += 1
                continue
            try:
                order = get_order(order_id, token=getattr(user, "alpaca_access_token", None), user=user)
                status = (order.get("status") or "").strip().lower()
                updates: dict[str, Any] = {
                    "order_status": status or trade.get("order_status"),
                    "status": status or trade.get("status"),
                    "filled_avg_price": order.get("filled_avg_price"),
                    "filled_qty": order.get("filled_qty"),
                    "raw_json": {"reconciliation": {"latest_order": order}},
                }
                if status in TERMINAL_BROKER_STATUSES:
                    updates["outcome"] = status
                db.update_trade_status(order_id, updates)
                summary["updated_count"] += 1
                summary["affected_orders"].append(order_id)
                if trade.get("symbol"):
                    summary["affected_symbols"].append(trade.get("symbol"))
            except BrokerError as exc:
                created_at = trade.get("created_at")
                created_dt = None
                if isinstance(created_at, str):
                    try:
                        created_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                    except ValueError:
                        created_dt = None
                if _is_not_found_error(exc) and created_dt and created_dt <= cutoff:
                    db.mark_stale_active_trade(order_id, "broker_order_not_found", {"reconciliation": {"error": str(exc)}})
                    summary["marked_stale_count"] += 1
                    summary["affected_orders"].append(order_id)
                    if trade.get("symbol"):
                        summary["affected_symbols"].append(trade.get("symbol"))
                elif _is_not_found_error(exc):
                    summary["skipped_count"] += 1
                else:
                    summary["error_count"] += 1
                    summary["skipped_count"] += 1
            except Exception:
                summary["error_count"] += 1
                summary["skipped_count"] += 1

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
