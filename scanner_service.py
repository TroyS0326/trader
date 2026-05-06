import inspect
import logging
import os
import signal
import time
from typing import Any, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import config
from app import app, redis_client
from db import insert_scan
from execution_guard import approve_scan_for_user
from models import User
from scanner import buy_window_open, run_scan

logger = logging.getLogger("scanner_service")
logging.basicConfig(
    level=getattr(logging, os.getenv("CENTRAL_SCANNER_LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)

_SHOULD_RUN = True


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.warning("Invalid int env %s=%r. Using default=%s", name, raw, default)
        return default


def _decision_allowlist() -> set[str]:
    raw = os.getenv("CENTRAL_SCANNER_EXECUTE_DECISIONS", "BUY NOW,A+,A")
    parsed = {item.strip().upper() for item in raw.split(",") if item.strip()}
    return parsed or {"BUY NOW", "A+", "A"}


def _run_scan_for_user(user: Any) -> Dict[str, Any]:
    sig = inspect.signature(run_scan)
    if "user" in sig.parameters:
        return run_scan(user=user)
    return run_scan()


def _onboarding_complete(user: Any) -> bool:
    required_flags = (
        "onboarding_completed",
        "paper_bankroll_set",
        "playbook_reviewed",
        "transparency_reviewed",
        "broker_connection_started",
    )
    return all(bool(getattr(user, flag, False)) for flag in required_flags)


def _extract_order_fields(best_pick: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    required = ("symbol", "qty", "entry_price", "stop_price", "target_1", "target_2")
    if any(best_pick.get(k) in (None, "") for k in required):
        return None
    try:
        return {
            "symbol": str(best_pick.get("symbol", "")).upper().strip(),
            "qty": int(float(best_pick.get("qty"))),
            "entry_price": float(best_pick.get("entry_price")),
            "stop_price": float(best_pick.get("stop_price")),
            "target_1": float(best_pick.get("target_1")),
            "target_2": float(best_pick.get("target_2")),
        }
    except (TypeError, ValueError):
        logger.warning("Execution skipped: invalid numeric order field payload=%r", best_pick)
        return None


def _dispatch_execution_if_allowed(user: Any, scan_payload: Dict[str, Any]) -> None:
    if not _env_bool("CENTRAL_SCANNER_EXECUTION_ENABLED", False):
        logger.info("Execution disabled globally. user_id=%s", user.id)
        return
    if str(getattr(user, "subscription_status", "free") or "free").strip().lower() != "pro":
        logger.info("Execution skipped non-pro user. user_id=%s", user.id)
        return
    if not getattr(user, "alpaca_access_token", None):
        logger.info("Execution skipped no active Alpaca token. user_id=%s", user.id)
        return
    if _env_bool("CENTRAL_SCANNER_REQUIRE_COMPLETED_ONBOARDING", True) and not _onboarding_complete(user):
        logger.info("Execution skipped onboarding incomplete. user_id=%s", user.id)
        return
    if not buy_window_open():
        logger.info("Execution skipped: buy window closed. user_id=%s", user.id)
        return

    best_pick = scan_payload.get("best_pick") or {}
    decision = str(best_pick.get("decision") or best_pick.get("setup_grade") or "").upper().strip()
    if decision not in _decision_allowlist():
        logger.info("Execution skipped decision not eligible. user_id=%s decision=%s", user.id, decision)
        return

    if str(getattr(user, "trading_mode", "paper") or "paper").strip().lower() == "live" and not _env_bool("CENTRAL_SCANNER_LIVE_EXECUTION_ENABLED", False):
        logger.warning("Execution skipped: live mode disabled globally. user_id=%s", user.id)
        return

    order_fields = _extract_order_fields(best_pick)
    if not order_fields:
        logger.info("Execution skipped: missing order fields. user_id=%s", user.id)
        return

    from tasks import execute_user_trade_task

    execute_user_trade_task.delay(
        user.id,
        scan_payload.get("scan_id"),
        order_fields["symbol"],
        order_fields["qty"],
        order_fields["entry_price"],
        order_fields["stop_price"],
        order_fields["target_1"],
        order_fields["target_2"],
    )
    logger.warning("Execution task dispatched. user_id=%s symbol=%s", user.id, order_fields["symbol"])


def _eligible_users() -> Iterable[Any]:
    limit = max(1, _env_int("CENTRAL_SCANNER_USER_LIMIT", 250))
    query = User.query.order_by(User.id.asc()).limit(limit)
    return query.all()


def run_central_scan_cycle(cycle_name: str) -> None:
    logger.info("Starting central scan cycle: %s", cycle_name)
    with app.app_context():
        users = list(_eligible_users())
        logger.info("Eligible users loaded count=%s", len(users))

        for user in users:
            try:
                result = _run_scan_for_user(user)
                if not isinstance(result, dict):
                    logger.warning("Scan returned non-dict. user_id=%s", user.id)
                    continue

                scan_id = insert_scan(result)
                result["scan_id"] = scan_id
                approve_scan_for_user(redis_client, user, result)
                logger.info("Scan approved for user. user_id=%s scan_id=%s", user.id, scan_id)

                _dispatch_execution_if_allowed(user, result)
            except Exception:
                logger.exception("Central scanner cycle user failure. user_id=%s", getattr(user, "id", None))


def _handle_shutdown(signum, _frame):
    global _SHOULD_RUN
    logger.warning("Received signal=%s, shutting down scanner service", signum)
    _SHOULD_RUN = False


def _build_scheduler() -> BackgroundScheduler:
    tz = ZoneInfo(config.TIMEZONE_LABEL)
    scheduler = BackgroundScheduler(timezone=tz)

    scheduler.add_job(
        run_central_scan_cycle,
        CronTrigger(day_of_week="mon-fri", hour=8, minute="0,15,30,45", timezone=tz),
        kwargs={"cycle_name": "premarket_refresh"},
        id="premarket_refresh",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    scheduler.add_job(
        run_central_scan_cycle,
        CronTrigger(day_of_week="mon-fri", hour=9, minute="45,50,55", timezone=tz),
        kwargs={"cycle_name": "morning_funnel"},
        id="morning_funnel",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    scheduler.add_job(
        run_central_scan_cycle,
        CronTrigger(day_of_week="mon-fri", hour="10-15", minute="*/5", timezone=tz),
        kwargs={"cycle_name": "market_funnel"},
        id="market_funnel",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )

    return scheduler


def main() -> None:
    if not _env_bool("CENTRAL_SCANNER_ENABLED", True):
        logger.warning("CENTRAL_SCANNER_ENABLED=0. Exiting.")
        return

    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGTERM, _handle_shutdown)

    scheduler = _build_scheduler()
    scheduler.start()
    logger.warning("Central scanner service started. timezone=%s", config.TIMEZONE_LABEL)

    try:
        while _SHOULD_RUN:
            time.sleep(1)
    finally:
        scheduler.shutdown(wait=False)
        logger.warning("Central scanner service stopped.")


if __name__ == "__main__":
    main()
