from celery import Celery
from models import db, User
from broker import place_managed_entry_order
from ai_catalyst import batch_process_premarket
from scanner import get_refined_universe
import config

celery_app = Celery('veteran_engine', broker='redis://localhost:6379/0')

@celery_app.task
def execute_user_trade_task(user_id, symbol, qty, entry_price, stop_price, target_1_price, target_2_price):
    """
    Worker task for parallel execution of AI-triggered setups.
    Updated to utilize the modern `place_managed_entry_order` from broker.py.
    """
    user = User.query.get(user_id)
    # Target only upgraded accounts for automated execution
    if not user or user.subscription_status != 'pro':
        return f'User {user_id} inactive or non-PRO. Trade aborted.'

    if qty < 1:
        return f'Risk sizing too small for User {user_id}'

    try:
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
        return f'Success: {qty} shares of {symbol} executed for User {user_id}. Order ID: {order.get("id")}'
    except Exception as e:
        return f'Execution failed for User {user_id}: {str(e)}'


def trigger_system_wide_buy(symbol, entry, stop, target_1, target_2):
    """
    Called by the master scanner when an A/A+ setup is found.
    Calculates dynamic sizing per user based on their specific risk tolerances
    before pushing to the Celery broker.
    """
    active_users = User.query.filter_by(subscription_status='pro').all()
    
    for user in active_users:
        # Calculate dynamic position sizing locally based on the individual's bankroll
        risk_per_share = entry - stop
        if risk_per_share <= 0:
            continue
            
        # Defaults to a 1% risk if user hasn't specified
        user_risk_pct = getattr(user, 'risk_pct', 1.0)
        dollar_risk = user.bankroll * (user_risk_pct / 100.0)
        
        # Enforce maximum dollar risk cap
        if dollar_risk > config.MAX_DOLLAR_LOSS_PER_TRADE:
            dollar_risk = config.MAX_DOLLAR_LOSS_PER_TRADE
            
        qty = int(dollar_risk // risk_per_share)
        
        if qty > 0:
            execute_user_trade_task.delay(
                user.id, symbol, qty, entry, stop, target_1, target_2
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
