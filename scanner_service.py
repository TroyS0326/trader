import inspect
import logging
from datetime import datetime, timedelta
import os
import signal
import time
from typing import Any, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import config
import db
from app import app, redis_client
from db import insert_scan
from execution_guard import approve_scan_for_user
from models import User
from scanner import run_scan, recheck_active_watch_candidates
from daily_report import run_daily_reports
from execution_diagnostics import evaluate_execution_readiness
from scan_contract import validate_scan_payload_contract
from order_reconciliation import reconcile_active_trade_orders
import json
from db import get_recent_scans
from scanner_effectiveness import build_scanner_effectiveness_report

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
    accepts_kwargs = any(
        param.kind == inspect.Parameter.VAR_KEYWORD
        for param in sig.parameters.values()
    )
    if "user" in sig.parameters or accepts_kwargs:
        return run_scan(user=user)
    return run_scan()


def run_shared_market_scan() -> Dict[str, Any]:
    """
    Run the expensive market-wide scanner exactly once per central scan cycle.

    IMPORTANT:
    Do not call this function inside a per-user loop. User-specific adaptations
    must happen in personalize_scan_for_user().
    """
    logger.info("Shared market scan started")
    result = _run_scan_for_user(user=None)
    logger.info("Shared market scan finished")
    return result






def _aggressive_promote_decision_allowlist() -> set[str]:
    raw = getattr(config, "AGGRESSIVE_PROMOTE_SCAN_DECISIONS", "SKIP,WATCH,WATCH FOR BREAKOUT")
    parsed = {item.strip().upper() for item in str(raw).split(",") if item.strip()}
    return parsed or {"SKIP", "WATCH", "WATCH FOR BREAKOUT"}


def _flatten_reason_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        flattened: list[str] = []
        for v in value.values():
            flattened.extend(_flatten_reason_values(v))
        return flattened
    if isinstance(value, (list, tuple, set)):
        flattened: list[str] = []
        for item in value:
            flattened.extend(_flatten_reason_values(item))
        return flattened
    return [str(value)]


def _maybe_promote_aggressive_intraday_pick(user: Any, result: Dict[str, Any]) -> Dict[str, Any]:
    if not (config.AGGRESSIVE_INTRADAY_ENABLED and config.AGGRESSIVE_PROMOTE_SCAN_DECISIONS_ENABLED):
        return result
    if str(getattr(user, "subscription_status", "")).lower() != "pro":
        return result
    trading_mode = str(getattr(user, "trading_mode", "paper")).lower()
    if trading_mode == "live" and not config.AGGRESSIVE_PROMOTION_LIVE_ENABLED:
        return result
    if trading_mode not in {"paper", "live"}:
        return result

    best_pick = result.get("best_pick")
    if not isinstance(best_pick, dict):
        return result

    symbol = str(best_pick.get("symbol") or "").strip().upper()
    old_decision = str(best_pick.get("decision") or "").strip()

    hard_terms = {"stale", "below_stop", "vwap_failure", "setup_broken", "spread_too_wide", "insufficient_liquidity", "price_out_of_range", "dilution", "not_tradeable", "buy_window_closed", "duplicate_active_trade"}

    def blocked(reason: str) -> Dict[str, Any]:
        logger.info("Aggressive intraday promotion skipped user_id=%s symbol=%s reason=%s", getattr(user, "id", None), symbol, reason)
        return result

    decision_u = old_decision.upper()
    if decision_u in {"DATA STALE", "NO TRADE"} or decision_u.startswith("SETUP BROKEN"):
        return blocked(f"decision={decision_u}")

    for source in (best_pick, best_pick.get("details") or {}, best_pick.get("momentum_meta") or {}):
        if not isinstance(source, dict):
            continue
        for flag in ("data_stale", "below_stop", "vwap_failure", "buy_window_closed", "setup_broken"):
            if bool(source.get(flag)):
                return blocked(flag)
    if str(best_pick.get("live_signal") or "").strip().upper().startswith("SETUP BROKEN"):
        return blocked("live_signal_setup_broken")
    if bool(best_pick.get("active_trade_blocked")):
        return blocked("active_trade_blocked")

    too_extended = bool(best_pick.get("too_extended"))
    pullback_reclaim = bool(best_pick.get("pullback_reclaim"))
    if too_extended and not pullback_reclaim:
        return blocked("too_extended_without_pullback_reclaim")

    for key in ("skip_reason_codes", "rejection_reason", "rejection_reasons", "missing_buy_confirmations", "missing_buy_confirmations_json"):
        for text in _flatten_reason_values(best_pick.get(key)):
            lowered = text.lower()
            if any(term in lowered for term in hard_terms):
                return blocked(f"reason_field:{key}")

    contract_diag = validate_scan_payload_contract({"best_pick": best_pick})
    if contract_diag.get("missing_order_fields"):
        return result

    try:
        qty = int(best_pick.get("qty"))
        entry = float(best_pick.get("entry_price"))
        stop = float(best_pick.get("stop_price"))
        target_1 = float(best_pick.get("target_1"))
        target_2 = float(best_pick.get("target_2"))
    except (TypeError, ValueError):
        return result

    if not symbol or qty < 1 or entry <= 0 or stop <= 0 or target_1 <= entry or target_2 < target_1 or stop >= entry:
        return result

    risk_per_share = entry - stop
    if risk_per_share <= 0:
        return result
    risk_pct = risk_per_share / entry
    max_risk_pct = float(config.AGGRESSIVE_PROMOTION_MAX_RISK_PER_SHARE_PCT)
    tightened_stop = None
    if risk_pct > max_risk_pct:
        if not bool(getattr(config, "AGGRESSIVE_TIGHTEN_PROMOTION_PLAN_ENABLED", False)):
            return result
        if trading_mode == "live" and not bool(getattr(config, "AGGRESSIVE_TIGHTEN_PROMOTION_LIVE_ENABLED", False)):
            return blocked("tightening_live_disabled")

        tightened_stop = round(entry * (1 - max_risk_pct), 2)
        if tightened_stop <= 0:
            return blocked("tightened_stop_non_positive")
        if tightened_stop >= entry:
            return blocked("tightened_stop_gte_entry")
        if tightened_stop <= stop:
            return blocked("tightened_stop_not_above_original")

        tightened_risk_per_share = entry - tightened_stop
        if tightened_risk_per_share <= 0:
            return blocked("tightened_risk_non_positive")
        rr_to_target_1 = (target_1 - entry) / tightened_risk_per_share
        if rr_to_target_1 < float(getattr(config, "AGGRESSIVE_TIGHTEN_PROMOTION_MIN_RR_TARGET_1", 1.20)):
            return blocked("tightened_rr_below_min")

    allowed = _aggressive_promote_decision_allowlist()
    decision_candidates = {str(best_pick.get(k) or "").strip().upper() for k in ("decision", "action", "setup_grade")}
    if not any(c in allowed for c in decision_candidates if c):
        return result

    best_pick["decision"] = "BUY NOW"
    best_pick["aggressive_intraday_promoted_from_decision"] = old_decision
    best_pick["aggressive_intraday_promoted"] = True
    if tightened_stop is not None:
        tightened_risk_per_share = entry - tightened_stop
        rr_to_target_1 = (target_1 - entry) / tightened_risk_per_share
        best_pick["aggressive_intraday_original_stop_price"] = stop
        best_pick["stop_price"] = tightened_stop
        best_pick["aggressive_intraday_stop_tightened"] = True
        best_pick["aggressive_intraday_tightened_risk_pct"] = tightened_risk_per_share / entry
        best_pick["aggressive_intraday_tightened_rr_to_target_1"] = rr_to_target_1
        best_pick["aggressive_intraday_promotion_reason"] = "Aggressive intraday promotion: oversized stop tightened to capped risk."
    else:
        best_pick["aggressive_intraday_promotion_reason"] = "Aggressive intraday promotion: complete numeric order contract passed."
    result["best_pick"] = best_pick

    for item in result.get("watchlist") or []:
        if isinstance(item, dict) and str(item.get("symbol") or "").strip().upper() == symbol:
            item["decision"] = "BUY NOW"
            item["aggressive_intraday_promoted_from_decision"] = old_decision
            item["aggressive_intraday_promoted"] = True
            item["aggressive_intraday_promotion_reason"] = best_pick["aggressive_intraday_promotion_reason"]
            if tightened_stop is not None:
                item["aggressive_intraday_original_stop_price"] = stop
                item["stop_price"] = tightened_stop
                item["aggressive_intraday_stop_tightened"] = True
                item["aggressive_intraday_tightened_risk_pct"] = best_pick.get("aggressive_intraday_tightened_risk_pct")
                item["aggressive_intraday_tightened_rr_to_target_1"] = best_pick.get("aggressive_intraday_tightened_rr_to_target_1")

    logger.warning("Aggressive intraday pick promoted user_id=%s symbol=%s from_decision=%s entry=%s stop=%s target_1=%s target_2=%s qty=%s risk_per_share_pct=%.4f", getattr(user, "id", None), symbol, old_decision, entry, best_pick.get("stop_price"), target_1, target_2, qty, risk_pct)
    return result

def _pick_is_allowed_for_user(user: Any, pick: Dict[str, Any]) -> bool:
    symbol = str((pick or {}).get("symbol") or "").upper().strip()
    if not symbol:
        return False

    if bool(getattr(user, "exclude_penny_stocks", True)):
        price = pick.get("entry_price", pick.get("current_price", pick.get("price", 0)))
        try:
            if float(price) > 0 and float(price) < 5.0:
                return False
        except (TypeError, ValueError):
            pass

    if bool(getattr(user, "exclude_biotech", False)):
        industry = str(pick.get("industry") or pick.get("sector") or "").lower()
        if any(k in industry for k in ("biotech", "biotechnology", "pharmaceutical")):
            return False

    return True


def _candidate_symbol(candidate: Any) -> str:
    if not isinstance(candidate, dict):
        return ""
    return str(candidate.get("symbol") or "").strip().upper()


def _active_trade_for_user_symbol(user: Any, symbol: str) -> Dict[str, Any] | None:
    if not symbol:
        return None
    try:
        return db.get_active_trade_for_user_symbol(getattr(user, "id", None), symbol)
    except Exception:
        logger.warning("Active trade duplicate check failed user_id=%s symbol=%s", getattr(user, "id", None), symbol, exc_info=True)
        return None

def _resize_best_pick_for_user(user: Any, best_pick: Dict[str, Any]) -> Dict[str, Any]:
    """
    P1: Replaces the shared-scan qty (sized against global CURRENT_BANKROLL)
    with a qty calculated from the user's actual live bankroll.

    This is the authoritative per-user sizing for automated execution.
    The shared scan qty is only a market-discovery signal — it must be
    re-stamped here before the payload is approved and dispatched.

    Safe-fails to the original shared qty on any error so a bankroll
    lookup failure never silently zeroes out a valid setup.
    """
    try:
        from analyze_performance import calculate_user_kelly_fraction

        entry_price = float(best_pick.get("entry_price") or 0.0)
        stop_price  = float(best_pick.get("stop_price")  or 0.0)

        if entry_price <= 0 or stop_price <= 0 or entry_price <= stop_price:
            return best_pick

        risk_per_share = max(0.01, entry_price - stop_price)

        # Prefer active_bankroll (mode-aware) then fall back to legacy bankroll
        user_bankroll = float(
            getattr(user, "active_bankroll", None)
            or getattr(user, "bankroll", 0.0)
        )
        if user_bankroll <= 0:
            return best_pick

        # Kelly fraction from trade history, fall back to user risk_pct setting
        kelly_fraction = calculate_user_kelly_fraction(getattr(user, "id", None))
        if kelly_fraction is None:
            user_risk_pct = float(getattr(user, "risk_pct", 1.0))
            dollar_risk = user_bankroll * (user_risk_pct / 100.0)
        elif kelly_fraction == 0:
            # Negative expected value — zero the position, let gate block it
            best_pick = dict(best_pick)
            best_pick["qty"] = 0
            best_pick["qty_source"] = "kelly_zero_ev"
            return best_pick
        else:
            dollar_risk = user_bankroll * kelly_fraction

        # Hard cap: 7% of bankroll OR config absolute ceiling, whichever is lower
        max_dollar_risk = min(
            dollar_risk,
            user_bankroll * getattr(config, "PER_TRADE_LOSS_CAP_PCT", 0.07),
            getattr(config, "MAX_DOLLAR_LOSS_PER_TRADE", 5.0),
        )

        user_qty = int(max_dollar_risk // risk_per_share)
        user_qty = max(0, min(getattr(config, "MAX_BUY_SHARES", 999), user_qty))

        best_pick = dict(best_pick)  # shallow copy — never mutate shared scan
        best_pick["qty"]        = user_qty
        best_pick["qty_source"] = "user_bankroll_kelly"
        best_pick["qty_bankroll_used"] = round(user_bankroll, 2)
        best_pick["qty_dollar_risk"]   = round(max_dollar_risk, 2)

        logger.debug(
            "personalize qty user_id=%s symbol=%s bankroll=$%s kelly=%s qty=%s",
            getattr(user, "id", None),
            best_pick.get("symbol"),
            round(user_bankroll, 2),
            kelly_fraction,
            user_qty,
        )
        return best_pick

    except Exception:
        logger.exception(
            "_resize_best_pick_for_user failed for user_id=%s — using shared scan qty",
            getattr(user, "id", None),
        )
        return best_pick

def personalize_scan_for_user(user: Any, shared_scan: Dict[str, Any]) -> Dict[str, Any]:
    """Build a user-specific scan payload from a shared market scan result."""
    result = dict(shared_scan or {})
    result["user_id"] = user.id
    result["report_user_id"] = user.id
    result["trading_mode"] = getattr(user, "trading_mode", "paper")
    result["subscription_status"] = getattr(user, "subscription_status", "free")
    result["scan_source"] = "central_scanner"
    result["scan_attribution_version"] = 1

    # Preserve user-level exclusion safety without re-running the expensive market scan.
    candidate_pool: List[Dict[str, Any]] = []
    best_pick = result.get("best_pick") or {}
    if isinstance(best_pick, dict):
        candidate_pool.append(best_pick)
    candidate_pool.extend(item for item in (result.get("watchlist") or []) if isinstance(item, dict))

    candidates: List[Dict[str, Any]] = []
    seen_symbols: set[str] = set()
    for item in candidate_pool:
        symbol = _candidate_symbol(item)
        if not symbol or symbol in seen_symbols or not _pick_is_allowed_for_user(user, item):
            continue
        seen_symbols.add(symbol)
        candidates.append(item)

    if not candidates:
        result["watchlist"] = []
        result["best_pick"] = {}
    elif not config.ACTIVE_SYMBOL_ROTATION_ENABLED:
        result["watchlist"] = candidates
        result["best_pick"] = candidates[0]
    else:
        non_active_candidates: List[Dict[str, Any]] = []
        active_blocked_candidates: List[Dict[str, Any]] = []
        blocked_symbols: List[str] = []
        for item in candidates:
            symbol = _candidate_symbol(item)
            if _active_trade_for_user_symbol(user, symbol):
                active_blocked_candidates.append(item)
                blocked_symbols.append(symbol)
            else:
                non_active_candidates.append(item)

        result["active_symbol_rotation_blocked_symbols"] = blocked_symbols
        original_best_symbol = _candidate_symbol(candidates[0]) if candidates else ""

        if non_active_candidates:
            result["best_pick"] = non_active_candidates[0]
            result["watchlist"] = non_active_candidates + active_blocked_candidates
            result["active_symbol_rotation_applied"] = bool(
                original_best_symbol and blocked_symbols and original_best_symbol in blocked_symbols and _candidate_symbol(result["best_pick"]) != original_best_symbol
            )
        else:
            blocked_pick = dict(active_blocked_candidates[0])
            blocked_pick["decision"] = "SKIP"
            blocked_pick["active_trade_blocked"] = True
            blocked_pick["active_trade_block_reason"] = "duplicate_active_trade"
            reasons = _flatten_reason_values(blocked_pick.get("skip_reason_codes"))
            if "duplicate_active_trade" not in reasons:
                reasons.append("duplicate_active_trade")
            blocked_pick["skip_reason_codes"] = reasons
            if "aggressive_intraday_promoted" in blocked_pick:
                blocked_pick["aggressive_intraday_promoted"] = False
            result["best_pick"] = blocked_pick
            result["watchlist"] = active_blocked_candidates
            result["active_symbol_rotation_all_candidates_blocked"] = True

# P1: re-stamp qty from user's live bankroll before approving the payload.
    # The shared scan sized against global CURRENT_BANKROLL — wrong for multi-user.
    best_pick = result.get("best_pick")
    if isinstance(best_pick, dict) and best_pick.get("symbol"):
        result["best_pick"] = _resize_best_pick_for_user(user, best_pick)

    result = _maybe_promote_aggressive_intraday_pick(user, result)
    return result


def fan_out_scan_to_users(shared_scan: Dict[str, Any], users: Iterable[Any]) -> None:
    users = list(users)
    logger.info("Fan-out starting for eligible users count=%s", len(users))
    for user in users:
        try:
            result = personalize_scan_for_user(user, shared_scan)
            contract_diag = validate_scan_payload_contract(result if isinstance(result, dict) else {})
            logger.info("Scan contract user_id=%s has_best_pick=%s key=%s executable_ready=%s missing=%s decision=%s qty_valid=%s notes=%s", getattr(user, "id", None), contract_diag.get("has_best_pick"), contract_diag.get("best_pick_key_used"), contract_diag.get("executable_payload_ready"), contract_diag.get("missing_order_fields"), contract_diag.get("decision"), contract_diag.get("qty_valid"), contract_diag.get("payload_shape_notes"))
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
    try:
        active_trade = db.get_active_trade_for_user_symbol(user.id, str(diag.get("symbol") or "").strip().upper())
        if active_trade:
            logger.info(
                "Execution skipped. reason=DUPLICATE_ACTIVE_TRADE user_id=%s scan_id=%s symbol=%s existing_trade_id=%s existing_order_id=%s",
                getattr(user, "id", None),
                scan_payload.get("scan_id"),
                str(diag.get("symbol") or "").strip().upper(),
                active_trade.get("id"),
                active_trade.get("order_id"),
            )
            return
    except Exception:
        logger.warning("Pre-dispatch duplicate active trade check failed user_id=%s scan_id=%s symbol=%s", getattr(user, "id", None), scan_payload.get("scan_id"), str(diag.get("symbol") or "").strip().upper(), exc_info=True)

    try:
        normalized_symbol = str(diag.get("symbol") or "").strip().upper()
        latest_trade = db.latest_trade_for_user_symbol(user.id, normalized_symbol)
        if latest_trade:
            created_at = latest_trade.get("created_at")
            created_dt = datetime.fromisoformat(str(created_at).replace("Z", "+00:00")) if created_at else None
            if created_dt is not None:
                now_utc = datetime.now(ZoneInfo("UTC"))
                if created_dt.tzinfo is None:
                    created_dt = created_dt.replace(tzinfo=ZoneInfo("UTC"))
                if created_dt > (now_utc - timedelta(minutes=config.EXECUTION_SYMBOL_COOLDOWN_MINUTES)):
                    logger.info("Execution skipped. reason=SYMBOL_COOLDOWN ctx=%s", base_ctx)
                    return
        count_today = db.count_trades_for_user_symbol_today(user.id, normalized_symbol)
        if count_today >= config.EXECUTION_MAX_SAME_SYMBOL_TRADES_PER_DAY:
            logger.info("Execution skipped. reason=SAME_SYMBOL_DAILY_LIMIT ctx=%s", base_ctx)
            return
    except Exception:
        logger.warning("Pre-dispatch symbol churn check failed user_id=%s scan_id=%s symbol=%s", getattr(user, "id", None), scan_payload.get("scan_id"), str(diag.get("symbol") or "").strip().upper(), exc_info=True)

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

        shared_scan = run_shared_market_scan()
        if not isinstance(shared_scan, dict):
            logger.warning("Shared scan returned invalid payload type=%s; skipping cycle before fan-out", type(shared_scan).__name__)
            return
        try:
            summary = reconcile_active_trade_orders(limit=max(1, config.ORDER_RECONCILIATION_ACTIVE_LIMIT))
            logger.info("Order reconciliation summary=%s", summary)
        except Exception:
            logger.exception("Order reconciliation failed before fan-out")
        fan_out_scan_to_users(shared_scan, users)
        if config.WATCH_RECHECK_ENABLED:
            try:
                logger.info("Watch recheck summary=%s", recheck_active_watch_candidates(limit=max(1, config.WATCH_RECHECK_LIMIT)))
            except Exception:
                logger.exception("Watch recheck failed after fan-out")


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
            contract_diag = diag.get("scan_contract") or validate_scan_payload_contract(latest_payload if isinstance(latest_payload, dict) else {})
            logger.info(
                "Execution readiness user_id=%s active_mode=%s paper_ready=%s live_ready=%s active_ready=%s paper_reason_codes=%s live_reason_codes=%s active_reason_codes=%s symbol=%s decision=%s qty=%s scan_id=%s contract=%s",
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
                contract_diag,
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
    parser.add_argument('--effectiveness-report', action='store_true', help='Print scanner effectiveness report without dispatching trades')
    parser.add_argument('--limit', type=int, default=50, help='Limit for effectiveness report scans')
    args = parser.parse_args()
    if args.diagnose:
        diagnose_execution_readiness()
    elif args.effectiveness_report:
        with app.app_context():
            print(json.dumps(build_scanner_effectiveness_report(limit=max(1, args.limit)), indent=2, default=str))
    else:
        main()
