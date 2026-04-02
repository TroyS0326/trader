import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from apscheduler.schedulers.background import BackgroundScheduler

from config import ALPACA_API_KEY, ALPACA_API_SECRET, ALPACA_PAPER_BASE, TIMEZONE_LABEL

logger = logging.getLogger(__name__)


def _alpaca_headers():
    return {
        'accept': 'application/json',
        'APCA-API-KEY-ID': ALPACA_API_KEY,
        'APCA-API-SECRET-KEY': ALPACA_API_SECRET,
    }


def flatten_book():
    """15:45 ET Kill Switch: Cancels all open orders and liquidates all positions."""
    logger.info('Executing 15:45 ET Kill Switch: Flattening the book.')
    _ = datetime.now(ZoneInfo(TIMEZONE_LABEL))

    # 1. Cancel all open orders
    try:
        requests.delete(f'{ALPACA_PAPER_BASE}/v2/orders', headers=_alpaca_headers())
        logger.info('All open orders cancelled.')
    except Exception as e:
        logger.error(f'Failed to cancel open orders: {e}')

    # 2. Close all open positions at market
    try:
        requests.delete(f'{ALPACA_PAPER_BASE}/v2/positions', headers=_alpaca_headers())
        logger.info('All open positions liquidated.')
    except Exception as e:
        logger.error(f'Failed to liquidate positions: {e}')


def start_execution_engine():
    """Starts the background execution managers and schedulers."""
    scheduler = BackgroundScheduler(timezone=ZoneInfo(TIMEZONE_LABEL))

    # Schedule the Flatten Book function at 15:45 ET Monday-Friday
    scheduler.add_job(
        flatten_book,
        'cron',
        day_of_week='mon-fri',
        hour=15,
        minute=45,
    )

    scheduler.start()
    logger.info('Execution Engine started. 15:45 Kill Switch armed.')


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    start_execution_engine()

    # Keep the main thread alive while background scheduler runs
    try:
        import time

        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info('Execution Engine shutting down.')
