import inspect
import logging
from datetime import datetime
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
from scanner import run_scan
from daily_report import run_daily_reports
from execution_diagnostics import evaluate_execution_readiness
import json
from db import get_recent_scans

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



def _dispatch_execution_if_allowed(user: Any, scan_payload: Dict[str, Any]) -> None:
    diag = evaluate_execution_readiness(user, scan_payload)
    base_ctx = {
        "user_id": getattr(user, "id", None),
        "scan_id": scan_payload.get("scan_id"),
        "trading_mode": diag.get("trading_mode"),
        "symbol": diag.get("symbol"),
        "decision": diag.get("decision"),
        "qty": diag.get("qty"),
    }

    if not diag.get("execution_ready"):
        for reason in diag.get("active_mode_blocked_reasons", diag.get("blocked_reasons", [])):
            logger.info("Execution skipped. reason=%s ctx=%s", reason.get("code"), base_ctx)
        return

    from tasks import execute_user_trade_task
    best_pick = (scan_payload.get("best_pick") or scan_payload.get("best") or scan_payload.get("top_pick") or {})
    target_1 = best_pick.get("target_1", best_pick.get("target_1_price"))
    target_2 = best_pick.get("target_2", best_pick.get("target_2_price"))

    order_fields = diag.get("order_fields") or {}
    execute_user_trade_task.delay(
        user.id,
        scan_payload.get("scan_id"),
        order_fields.get("symbol", diag["symbol"]),
        order_fields.get("qty", diag["qty"]),
        float(order_fields.get("entry_price", best_pick.get("entry_price"))),
        float(order_fields.get("stop_price", best_pick.get("stop_price"))),
        float(order_fields.get("target_1", target_1)),
        float(order_fields.get("target_2", target_2)),
    )
    logger.warning("Execution task dispatched. ctx=%s", base_ctx)


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

                result["user_id"] = user.id
                result["report_user_id"] = user.id
                result["trading_mode"] = getattr(user, "trading_mode", "paper")
                result["subscription_status"] = getattr(user, "subscription_status", "free")

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




def run_daily_paper_report_job() -> None:
    if not config.DAILY_REPORT_EMAIL_ENABLED:
        logger.info('Daily report email disabled via DAILY_REPORT_EMAIL_ENABLED=0')
        return
    report_date = datetime.now(ZoneInfo(config.TIMEZONE_LABEL)).date()
    if config.DAILY_REPORT_SKIP_WEEKENDS and report_date.weekday() >= 5:
        logger.info('Daily report skipped weekend for %s', report_date.isoformat())
        return
    with app.app_context():
        run_daily_reports(report_date, send=True, send_all=False, dry_run=config.DAILY_REPORT_DRY_RUN)

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
    scheduler.add_job(
        run_daily_paper_report_job,
        CronTrigger(day_of_week="mon-fri", hour=17, minute=0, timezone=tz),
        id="daily_paper_report_email",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )

    return scheduler


def diagnose_execution_readiness() -> None:
    with app.app_context():
        for user in _eligible_users():
            latest_payload = None
            try:
                raw = redis_client.get(f"latest_scan:{user.id}")
                if raw:
                    latest_payload = json.loads(raw)
            except Exception:
                latest_payload = None
            if latest_payload is None:
                scans = get_recent_scans() or []
                for scan in scans:
                    if int(scan.get("user_id") or 0) == int(user.id):
                        latest_payload = scan
                        break
            if latest_payload is None:
                latest_payload = {}

            diag = evaluate_execution_readiness(user, latest_payload)
            paper_reasons = [r["code"] for r in diag.get("paper_blocked_reasons", [])]
            live_reasons = [r["code"] for r in diag.get("live_blocked_reasons", [])]
            active_reasons = [r["code"] for r in diag.get("active_mode_blocked_reasons", [])]
            if not latest_payload:
                paper_reasons.append("NO_RECENT_SCAN")
                live_reasons.append("NO_RECENT_SCAN")
                active_reasons.append("NO_RECENT_SCAN")
            logger.info(
                "Execution readiness user_id=%s active_mode=%s paper_ready=%s live_ready=%s active_ready=%s paper_reason_codes=%s live_reason_codes=%s active_reason_codes=%s symbol=%s decision=%s qty=%s scan_id=%s",
                user.id,
                diag.get("active_mode", diag.get("trading_mode")),
                diag.get("paper_execution_ready"),
                diag.get("live_execution_ready"),
                diag["execution_ready"],
                paper_reasons,
                live_reasons,
                active_reasons,
                diag.get("symbol"),
                diag.get("decision"),
                diag.get("qty"),
                latest_payload.get("scan_id"),
            )


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
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument('--diagnose', action='store_true', help='Print execution readiness diagnostics without dispatching trades')
    args = parser.parse_args()
    if args.diagnose:
        diagnose_execution_readiness()
    else:
        main()
