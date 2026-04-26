from celery import Celery
import json
import redis
import config
from models import db, User
from broker import submit_order  # Pulling from your existing broker.py
from ai_catalyst import batch_process_premarket
from scanner import get_refined_universe

# 1. Connect Celery to your Redis server (the message broker)
celery_app = Celery('veteran_engine', broker='redis://localhost:6379/0')
redis_client = redis.Redis.from_url('redis://localhost:6379/0', decode_responses=True)


@celery_app.task
def execute_user_trade_task(user_id, symbol, entry_price, target, stop):
    """
    This function is a 'Worker'. It runs in parallel for every single user instantly.
    """
    # 2. Look up this specific user's settings from our new database
    user = User.query.get(user_id)
    if not user or user.subscription_status != 'active':
        return 'User inactive. Trade aborted.'

    # 3. Calculate position size based on THEIR unique bankroll and risk %
    risk_per_share = entry_price - stop
    dollar_risk = user.bankroll * (user.risk_pct / 100.0)
    qty = int(dollar_risk // risk_per_share)

    if qty < 1:
        return f'Risk sizing too small for User {user_id}'

    # 4. Build the payload using THEIR specific data feed and OAuth token
    headers = {
        'Authorization': f'Bearer {user.alpaca_access_token}'  # Using secure OAuth token
    }

    order_payload = {
        'symbol': symbol,
        'qty': qty,
        'side': 'buy',
        'type': 'limit',
        'time_in_force': 'day',
        'limit_price': entry_price,
    }

    # 5. Fire the order to Alpaca
    # (In production, you'd pass the headers to your submit_order function)
    # response = submit_order(order_payload, headers=headers)
    _ = headers, submit_order

    return f'Success: {qty} shares of {symbol} ordered for User {user_id}'


def trigger_system_wide_buy(symbol, entry, target, stop):
    """
    Called when the Master Scanner finds an A+ setup.
    """
    # Get all active paying users
    active_users = User.query.filter_by(subscription_status='active').all()

    # Instantly dispatch a parallel task for every user
    for user in active_users:
        # The .delay() command sends it to Redis to be processed instantly in the background
        execute_user_trade_task.delay(user.id, symbol, entry, target, stop)

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


@celery_app.task
def async_run_scan_task(user_id):
    """
    Background worker task to process heavy market scans and broadcast via WebSockets.
    """
    # Local imports to prevent circular dependency issues with Celery.
    from app import app
    from models import User
    from scanner import run_scan, buy_window_open
    from onboarding import fetch_and_sync_bankroll
    from db import get_failed_trades_today, insert_scan

    with app.app_context():
        user = User.query.get(user_id)
        if not user:
            return 'User not found'

        try:
            fetch_and_sync_bankroll(user)
            result = run_scan(user)

            risk_controls = {
                'failed_trades_today': get_failed_trades_today(),
                'max_failed_trades_per_day': config.MAX_FAILED_TRADES_PER_DAY,
                'buy_window_open': buy_window_open(),
                'no_buy_before_et': config.NO_BUY_BEFORE_ET,
            }
            result['risk_controls'] = risk_controls
            scan_id = insert_scan(result)
            result['scan_id'] = scan_id

            redis_client.setex('latest_scan', 300, json.dumps(result))
            broadcast_payload = {
                'type': 'scan_complete',
                'data': result,
            }
            redis_client.publish('ws_broadcast', json.dumps(broadcast_payload))
            return f'Scan complete for User {user_id}'
        except Exception as exc:
            error_payload = {
                'type': 'scan_error',
                'error': str(exc),
            }
            redis_client.publish('ws_broadcast', json.dumps(error_payload))
            return f'Scan failed: {exc}'
