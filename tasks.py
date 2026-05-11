import redis
import logging
import requests
from datetime import datetime, timedelta, timezone
from celery import Celery
from celery.schedules import crontab
from flask import Flask

from models import db, MarketRegime, User
from broker import place_managed_entry_order
from execution_guard import validate_execution_against_approved_scan, audit_trade_log
from ai_catalyst import batch_process_premarket
from scanner import get_refined_universe
from analyze_performance import calculate_user_kelly_fraction
import config
import db as db_ops
from db_safety import assert_existing_production_database_has_users, assert_not_empty_production_database, validate_runtime_database_safety
from sentry_setup import init_sentry
from time_utils import utc_now_aware, utc_now_naive

init_sentry("xeanvi-worker")

celery_app = Celery(
    'veteran_engine',
    broker=config.REDIS_URL,
    backend=config.REDIS_URL,
)
redis_client = redis.Redis.from_url(config.REDIS_URL, decode_responses=True)
celery_app.conf.timezone = 'UTC'
celery_app.conf.beat_schedule = {
    'update-market-regime-every-5-minutes': {
        'task': 'tasks.update_market_regime_task',
        'schedule': crontab(minute='*/5'),
    },
    'send-admin-daily-digest': {
        'task': 'tasks.send_admin_daily_digest_task',
        'schedule': crontab(minute=0, hour=22),
    },
}

_db_app = Flask(__name__)
_db_app.config['SQLALCHEMY_DATABASE_URI'] = config.SQLALCHEMY_DATABASE_URI
_db_app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
_db_app.config['SQLALCHEMY_ENGINE_OPTIONS'] = config.SQLALCHEMY_ENGINE_OPTIONS
db.init_app(_db_app)

with _db_app.app_context():
    validate_runtime_database_safety(_db_app)
    assert_existing_production_database_has_users(db)
    assert_not_empty_production_database(db)

ALPACA_HEADERS = {
    'APCA-API-KEY-ID': config.ALPACA_API_KEY,
    'APCA-API-SECRET-KEY': config.ALPACA_API_SECRET,
}
CHOP_RANGE_THRESHOLD_PCT = 0.006  # 0.6% daily range on SPY ~= tight/choppy market


def _parse_utc_datetime(value):
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            return None
    else:
        return None

    try:
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def _safe_log_exception(message, *args):
    """Log exceptions without allowing logger failures to affect task flow."""
    try:
        celery_app.log.get_default_logger().exception(message, *args)
        return
    except Exception:
        pass

    try:
        logging.getLogger(__name__).exception(message, *args)
    except Exception:
        pass


def _persist_submitted_trade_safely(*, user, user_id, scan_id, symbol, qty, entry_price, stop_price, target_1_price, target_2_price, order, order_id, order_status):
    try:
        existing_trade = db_ops.get_trade_by_order_id(order_id)
        if existing_trade:
            return False

        risk_per_share = None
        try:
            risk_per_share = float(entry_price) - float(stop_price)
        except (TypeError, ValueError):
            pass

        trade_payload = {
            "user_id": user.id,
            "scan_id": scan_id,
            "symbol": symbol,
            "side": "buy",
            "decision": "BUY NOW",
            "status": order_status,
            "order_status": order_status,
            "order_id": order_id,
            "entry_price": entry_price,
            "stop_price": stop_price,
            "target_1": target_1_price,
            "target_2": target_2_price,
            "qty": qty,
            "risk_per_share": risk_per_share,
            "raw_json": {
                "order_result": order,
                "order_bundle": order.get("order_bundle") if isinstance(order, dict) else None,
                "source": "execute_user_trade_task",
            },
        }
        db_ops.insert_trade(trade_payload)
        return True
    except Exception:
        _safe_log_exception(
            "execute_user_trade_task failed to persist trade row for order_id=%s user_id=%s",
            order_id,
            user_id,
        )
        return False


@celery_app.task
def execute_user_trade_task(user_id, scan_id, symbol, qty, entry_price, stop_price, target_1_price, target_2_price):
    """
    Worker task for parallel execution of AI-triggered setups.
    Updated to utilize the modern `place_managed_entry_order` from broker.py.
    """
    with _db_app.app_context():
        user = db.session.get(User, user_id)
        # Target only upgraded accounts for automated execution
        if not user or user.subscription_status != 'pro':
            return f'User {user_id} inactive or non-PRO. Trade aborted.'

        if qty < 1:
            return f'Risk sizing too small for User {user_id}'

        try:
            guard = validate_execution_against_approved_scan(
                redis_client=redis_client,
                user=user,
                symbol=symbol,
                scan_id=scan_id,
            )

            if not guard.get("ok"):
                return f'LIVE trade blocked for User {user_id}: {guard.get("error")}'

            active_trade = None
            try:
                active_trade = db_ops.get_active_trade_for_user_symbol(user_id, symbol)
            except Exception:
                _safe_log_exception(
                    "execute_user_trade_task active-trade duplicate check failed for user_id=%s symbol=%s",
                    user_id,
                    symbol,
                )

            if active_trade:
                created_at = active_trade.get("created_at")
                stale_cutoff = utc_now_aware().astimezone(timezone.utc) - timedelta(minutes=config.ORDER_RECONCILIATION_STALE_MINUTES)
                created_dt = _parse_utc_datetime(created_at)
                try_reconcile = created_dt is not None and created_dt <= stale_cutoff
                if try_reconcile:
                    try:
                        from order_reconciliation import reconcile_active_trade_orders
                        reconcile_active_trade_orders(user_id=user_id, limit=config.ORDER_RECONCILIATION_ACTIVE_LIMIT)
                        active_trade = db_ops.get_active_trade_for_user_symbol(user_id, symbol)
                    except Exception:
                        _safe_log_exception(
                            "execute_user_trade_task stale reconciliation failed user_id=%s symbol=%s",
                            user_id,
                            symbol,
                        )

            if active_trade:
                existing_order_id = active_trade.get("order_id")
                blocked_order_result = {
                    "id": existing_order_id,
                    "status": "blocked",
                    "reason": "duplicate_active_trade",
                    "existing_trade_id": active_trade.get("id"),
                    "existing_order_id": existing_order_id,
                    "symbol": symbol,
                }
                audit_trade_log(
                    logger=celery_app.log.get_default_logger(),
                    user=user,
                    symbol=symbol,
                    scan_id=scan_id,
                    qty=qty,
                    entry_price=entry_price,
                    stop_price=stop_price,
                    target_1=target_1_price,
                    target_2=target_2_price,
                    order_result=blocked_order_result,
                )
                return (
                    f'Duplicate active trade blocked for User {user_id}: {symbol} '
                    f'existing_order_id={existing_order_id}'
                )

            # Route through the same bracket logic used in manual testing
            order = place_managed_entry_order(
                symbol=symbol,
                qty=qty,
                entry_price=entry_price,
                stop_price=stop_price,
                target_1_price=target_1_price,
                target_2_price=target_2_price,
                user=user
            )

            order_id = order.get("id") if isinstance(order, dict) else None
            order_status = (order.get("status") if isinstance(order, dict) else None) or "submitted"

            if order_id:
                _persist_submitted_trade_safely(
                    user=user,
                    user_id=user_id,
                    scan_id=scan_id,
                    symbol=symbol,
                    qty=qty,
                    entry_price=entry_price,
                    stop_price=stop_price,
                    target_1_price=target_1_price,
                    target_2_price=target_2_price,
                    order=order,
                    order_id=order_id,
                    order_status=order_status,
                )

            audit_trade_log(
                logger=celery_app.log.get_default_logger(),
                user=user,
                symbol=symbol,
                scan_id=scan_id,
                qty=qty,
                entry_price=entry_price,
                stop_price=stop_price,
                target_1=target_1_price,
                target_2=target_2_price,
                order_result=order,
            )
            return f'Success: {qty} shares of {symbol} executed for User {user_id}. Order ID: {order.get("id")}'
        except Exception as e:
            return f'Execution failed for User {user_id}: {str(e)}'


def trigger_system_wide_buy(scan_id, symbol, entry, stop, target_1, target_2):
    """
    Called by the master scanner when an A/A+ setup is found.
    Calculates dynamic sizing per user based on their specific risk tolerances
    before pushing to the Celery broker.
    """
    with _db_app.app_context():
        active_users = User.query.filter_by(subscription_status='pro').all()

        for user in active_users:
            risk_per_share = entry - stop
            if risk_per_share <= 0:
                continue

            kelly_fraction = calculate_user_kelly_fraction(user.id)

            if kelly_fraction is None:
                user_risk_pct = getattr(user, 'risk_pct', 1.0)
                dollar_risk = user.bankroll * (user_risk_pct / 100.0)
            elif kelly_fraction == 0:
                continue
            else:
                dollar_risk = user.bankroll * kelly_fraction

            if dollar_risk > config.MAX_DOLLAR_LOSS_PER_TRADE:
                dollar_risk = config.MAX_DOLLAR_LOSS_PER_TRADE

            qty = int(dollar_risk // risk_per_share)

            if qty > 0:
                execute_user_trade_task.delay(
                    user.id,
                    scan_id,
                    symbol,
                    qty,
                    entry,
                    stop,
                    target_1,
                    target_2,
                )

        logger.info('Dispatched %s parallel execution tasks for %s', len(active_users), symbol)


def morning_pre_processing():
    """
    Runs the pre-market AI batch so scanner feature-store scores are ready before the opening scan.
    """
    symbols = get_refined_universe()
    if not symbols:
        return []
    batch_process_premarket(symbols)
    return symbols


def _safe_float(value):
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _fetch_snapshot(symbols):
    symbols_param = ','.join(symbols)
    feed = config.ALPACA_DATA_FEED
    params = {'symbols': symbols_param, 'feed': feed}
    try:
        response = requests.get(
            f'{config.ALPACA_DATA_BASE}/v2/stocks/snapshots',
            headers=ALPACA_HEADERS,
            params=params,
            timeout=10,
        )
        response.raise_for_status()
        return response.json()
    except requests.HTTPError as exc:
        status_code = getattr(getattr(exc, 'response', None), 'status_code', None)
        if feed == 'sip' and status_code in {400, 403}:
            celery_app.log.get_default_logger().warning(
                'SIP snapshot request failed with status=%s; retrying with IEX feed.',
                status_code,
            )
            fallback_response = requests.get(
                f'{config.ALPACA_DATA_BASE}/v2/stocks/snapshots',
                headers=ALPACA_HEADERS,
                params={'symbols': symbols_param, 'feed': 'iex'},
                timeout=10,
            )
            fallback_response.raise_for_status()
            return fallback_response.json()
        raise


def _extract_from_snapshot(snapshot):
    latest_trade = snapshot.get('latestTrade') or {}
    daily_bar = snapshot.get('dailyBar') or {}
    prev_daily_bar = snapshot.get('prevDailyBar') or {}
    return {
        'last_price': _safe_float(latest_trade.get('p') or daily_bar.get('c')),
        'day_high': _safe_float(daily_bar.get('h')),
        'day_low': _safe_float(daily_bar.get('l')),
        'prev_close': _safe_float(prev_daily_bar.get('c')),
    }


def _fetch_latest_bars(symbols):
    symbols_param = ','.join(symbols)
    feed = config.ALPACA_DATA_FEED
    params = {'symbols': symbols_param, 'feed': feed}
    try:
        response = requests.get(
            f'{config.ALPACA_DATA_BASE}/v2/stocks/bars/latest',
            headers=ALPACA_HEADERS,
            params=params,
            timeout=10,
        )
        response.raise_for_status()
        return response.json().get('bars') or {}
    except requests.HTTPError as exc:
        status_code = getattr(getattr(exc, 'response', None), 'status_code', None)
        if feed == 'sip' and status_code in {400, 403}:
            celery_app.log.get_default_logger().warning(
                'SIP latest bars request failed with status=%s; retrying with IEX feed.',
                status_code,
            )
            fallback_response = requests.get(
                f'{config.ALPACA_DATA_BASE}/v2/stocks/bars/latest',
                headers=ALPACA_HEADERS,
                params={'symbols': symbols_param, 'feed': 'iex'},
                timeout=10,
            )
            fallback_response.raise_for_status()
            return fallback_response.json().get('bars') or {}
        raise


@celery_app.task
def update_market_regime_task():
    """
    Refreshes the latest market regime classification every 5 minutes.
    Regime is 'high_volatility' when VIXY spikes >5% on day OR SPY is unusually tight/choppy.
    """
    if not config.ALPACA_API_KEY or not config.ALPACA_API_SECRET:
        return 'Skipped: missing ALPACA_API_KEY/ALPACA_API_SECRET.'

    try:
        snapshots = _fetch_snapshot(['SPY', 'VIXY'])
    except Exception as exc:
        return {'status': 'skipped', 'reason': f'snapshot fetch failed: {exc}'}

    spy_data = _extract_from_snapshot(snapshots.get('SPY') or {})
    vixy_data = _extract_from_snapshot(snapshots.get('VIXY') or {})
    latest_bars = {}
    if spy_data['last_price'] is None or vixy_data['last_price'] is None:
        try:
            latest_bars = _fetch_latest_bars(['SPY', 'VIXY'])
        except Exception as exc:
            celery_app.log.get_default_logger().warning(
                'Latest bars fallback unavailable: %s',
                exc,
            )

    if spy_data['last_price'] is None:
        spy_data['last_price'] = _safe_float((latest_bars.get('SPY') or {}).get('c'))
    if vixy_data['last_price'] is None:
        vixy_data['last_price'] = _safe_float((latest_bars.get('VIXY') or {}).get('c'))

    spy_price = spy_data['last_price']
    spy_day_high = spy_data['day_high']
    spy_day_low = spy_data['day_low']
    vixy_price = vixy_data['last_price']
    vixy_prev_close = vixy_data['prev_close']

    if (
        spy_price is None
        or spy_day_high is None
        or spy_day_low is None
        or vixy_price is None
        or vixy_prev_close is None
    ):
        return {
            'status': 'skipped',
            'reason': 'insufficient market regime inputs after snapshot/latest-bars fallback',
            'spy': spy_data,
            'vixy': vixy_data,
        }

    vixy_day_change_pct = None
    if vixy_price and vixy_prev_close and vixy_prev_close > 0:
        vixy_day_change_pct = ((vixy_price - vixy_prev_close) / vixy_prev_close) * 100.0

    spy_range_pct = None
    if spy_price and spy_price > 0 and spy_day_high is not None and spy_day_low is not None:
        spy_range_pct = (spy_day_high - spy_day_low) / spy_price

    high_vix = (vixy_day_change_pct or 0.0) > 5.0
    tight_chop = spy_range_pct is not None and spy_range_pct <= CHOP_RANGE_THRESHOLD_PCT
    regime_status = 'high_volatility' if (high_vix or tight_chop) else 'normal'

    with _db_app.app_context():
        latest = MarketRegime.query.order_by(MarketRegime.id.desc()).first()
        if latest is None:
            latest = MarketRegime()
            db.session.add(latest)

        latest.regime_status = regime_status
        # vix_value currently stores VIXY proxy price, not raw VIX index level.
        latest.vix_value = vixy_price
        latest.spy_trend = 'chop' if tight_chop else 'normal'
        latest.updated_at = utc_now_naive()
        db.session.commit()

    return {
        'regime_status': regime_status,
        'vixy_day_change_pct': vixy_day_change_pct,
        'spy_range_pct': spy_range_pct,
    }


@celery_app.task
def send_admin_daily_digest_task():
    import admin_daily_digest
    if not config.ADMIN_DAILY_DIGEST_ENABLED:
        return {'status': 'skipped', 'reason': 'disabled'}
    if config.ADMIN_DAILY_DIGEST_SKIP_WEEKENDS and utc_now_aware().weekday() >= 5:
        return {'status': 'skipped', 'reason': 'weekend'}
    with _db_app.app_context():
        return admin_daily_digest.send_admin_daily_digest()
