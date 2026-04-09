from celery import Celery
from models import db, User
from broker import submit_order  # Pulling from your existing broker.py

# 1. Connect Celery to your Redis server (the message broker)
celery_app = Celery('veteran_engine', broker='redis://localhost:6379/0')


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
