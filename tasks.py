from datetime import datetime

import redis
import requests
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

celery_app = Celery('veteran_engine', broker='redis://localhost:6379/0')
redis_client = redis.Redis.from_url('redis://localhost:6379/0', decode_responses=True)
celery_app.conf.timezone = 'UTC'
celery_app.conf.beat_schedule = {
    'update-market-regime-every-5-minutes': {
        'task': 'tasks.update_market_regime_task',
        'schedule': crontab(minute='*/5'),
    },
}

_db_app = Flask(__name__)
_db_app.config['SQLALCHEMY_DATABASE_URI'] = f"sqlite:///{config.DB_PATH}"
_db_app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db.init_app(_db_app)

ALPACA_HEADERS = {
    'APCA-API-KEY-ID': config.ALPACA_API_KEY,
    'APCA-API-SECRET-KEY': config.ALPACA_API_SECRET,
}
CHOP_RANGE_THRESHOLD_PCT = 0.006  # 0.6% daily range on SPY ~= tight/choppy market

@celery_app.task
def execute_user_trade_task(user_id, scan_id, symbol, qty, entry_price, stop_price, target_1_price, target_2_price):
    """
    Worker task for parallel execution of AI-triggered setups.
    Updated to utilize the modern `place_managed_entry_order` from broker.py.
    """
    with _db_app.app_context():
        user = User.query.get(user_id)
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

            # Enforce maximum dollar risk cap
            if dollar_risk > config.MAX_DOLLAR_LOSS_PER_TRADE:
                dollar_risk = config.MAX_DOLLAR_LOSS_PER_TRADE

            qty = int(dollar_risk // risk_per_share)

            if qty > 0:
                execute_user_trade_task.delay(
                    user.id, scan_id, symbol, qty, entry, stop, target_1, target_2
                )

        print(f'Dispatched {len(active_users)} parallel execution tasks for {symbol}!')


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
    response = requests.get(
        f'{config.ALPACA_DATA_BASE}/v2/stocks/snapshots',
        headers=ALPACA_HEADERS,
        params={'symbols': ','.join(symbols), 'feed': 'iex'},
        timeout=10,
    )
    response.raise_for_status()
    return response.json()


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


def _fetch_latest_15m_bars(symbols):
    response = requests.get(
        f'{config.ALPACA_DATA_BASE}/v2/stocks/bars/latest',
        headers=ALPACA_HEADERS,
        params={'symbols': ','.join(symbols), 'timeframe': '15Min', 'feed': 'iex'},
        timeout=10,
    )
    response.raise_for_status()
    return response.json().get('bars') or {}


@celery_app.task
def update_market_regime_task():
    """
    Refreshes the latest market regime classification every 5 minutes.
    Regime is 'high_volatility' when VIXY spikes >5% on day OR SPY is unusually tight/choppy.
    """
    if not config.ALPACA_API_KEY or not config.ALPACA_API_SECRET:
        return 'Skipped: missing ALPACA_API_KEY/ALPACA_API_SECRET.'

    snapshots = _fetch_snapshot(['SPY', 'VIXY'])
    spy_data = _extract_from_snapshot(snapshots.get('SPY') or {})
    vixy_data = _extract_from_snapshot(snapshots.get('VIXY') or {})
    latest_bars = _fetch_latest_15m_bars(['SPY', 'VIXY'])

    if spy_data['last_price'] is None:
        spy_data['last_price'] = _safe_float((latest_bars.get('SPY') or {}).get('c'))
    if vixy_data['last_price'] is None:
        vixy_data['last_price'] = _safe_float((latest_bars.get('VIXY') or {}).get('c'))

    spy_price = spy_data['last_price']
    spy_day_high = spy_data['day_high']
    spy_day_low = spy_data['day_low']
    vixy_price = vixy_data['last_price']
    vixy_prev_close = vixy_data['prev_close']

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
        latest.vix_value = vixy_price
        latest.spy_trend = 'chop' if tight_chop else 'normal'
        latest.updated_at = datetime.utcnow()
        db.session.commit()

    return {
        'regime_status': regime_status,
        'vixy_day_change_pct': vixy_day_change_pct,
        'spy_range_pct': spy_range_pct,
    }
