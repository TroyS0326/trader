import asyncio
import json
import logging
import threading
from zoneinfo import ZoneInfo

import requests
import websockets
from apscheduler.schedulers.background import BackgroundScheduler

from broker import maybe_activate_runner_trailing
from config import ALPACA_API_KEY, ALPACA_API_SECRET, ALPACA_PAPER_BASE, TIMEZONE_LABEL
from db import get_trade_by_target1_id, update_trade_status

logger = logging.getLogger(__name__)

# For live trading later: wss://api.alpaca.markets/stream
ALPACA_WSS_URL = ALPACA_PAPER_BASE.replace('https', 'wss') + '/stream'


def _alpaca_headers():
    return {
        'accept': 'application/json',
        'APCA-API-KEY-ID': ALPACA_API_KEY,
        'APCA-API-SECRET-KEY': ALPACA_API_SECRET,
    }


def flatten_book():
    """15:45 ET Kill Switch: Cancels all open orders and liquidates all positions."""
    logger.info('Executing 15:45 ET Kill Switch: Flattening the book.')
    try:
        requests.delete(f'{ALPACA_PAPER_BASE}/v2/orders', headers=_alpaca_headers())
        requests.delete(f'{ALPACA_PAPER_BASE}/v2/positions', headers=_alpaca_headers())
        logger.info('Book flattened.')
    except Exception as e:
        logger.error(f'Kill switch error: {e}')


async def handle_fill_event(order: dict):
    """Processes fills instantly without UI polling."""
    order_id = order.get('id')
    filled_qty = order.get('filled_qty')

    logger.info(f'WS EVENT: Order {order_id} filled for {filled_qty} shares.')
    if not order_id:
        return

    trade = get_trade_by_target1_id(order_id)
    if not trade:
        return

    logger.info(f"Target 1 hit for {trade['symbol']}! Activating trailing runner instantly.")

    raw_json = trade.get('raw_json')
    if isinstance(raw_json, str):
        try:
            raw_json = json.loads(raw_json)
        except json.JSONDecodeError:
            raw_json = {}
    raw_json = raw_json or {}

    bundle = raw_json.get('order_bundle', {})
    entry_price = float(trade.get('entry_price') or 0.0)
    updated_bundle = maybe_activate_runner_trailing(bundle, breakeven_price=entry_price)

    raw_json['order_bundle'] = updated_bundle
    update_trade_status(trade['order_id'], {'raw_json': raw_json})


async def alpaca_trade_listener():
    """Persistent WebSocket connection to listen for zero-latency fills."""
    logger.info(f'Connecting to Alpaca Trade Stream at {ALPACA_WSS_URL}...')

    async for websocket in websockets.connect(ALPACA_WSS_URL):
        try:
            auth_msg = {'action': 'auth', 'key': ALPACA_API_KEY, 'secret': ALPACA_API_SECRET}
            await websocket.send(json.dumps(auth_msg))
            auth_reply = await websocket.recv()
            logger.info(f'Alpaca Auth Reply: {auth_reply}')

            sub_msg = {'action': 'listen', 'data': {'streams': ['trade_updates']}}
            await websocket.send(json.dumps(sub_msg))
            sub_reply = await websocket.recv()
            logger.info(f'Alpaca Sub Reply: {sub_reply}')

            async for message in websocket:
                data = json.loads(message)
                if data.get('stream') != 'trade_updates':
                    continue

                event = data.get('data', {}).get('event')
                order = data.get('data', {}).get('order', {})
                if event in ('fill', 'partial_fill'):
                    await handle_fill_event(order)

        except websockets.ConnectionClosed:
            logger.warning('Alpaca WebSocket closed. Reconnecting...')
            await asyncio.sleep(1)
        except Exception as e:
            logger.error(f'WebSocket listener error: {e}')
            await asyncio.sleep(1)


def run_async_loop_in_thread():
    """Runs the asyncio loop in a separate OS thread so it does not block Flask."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(alpaca_trade_listener())


def start_execution_engine():
    """Starts the background execution managers (Cron + WebSocket)."""
    scheduler = BackgroundScheduler(timezone=ZoneInfo(TIMEZONE_LABEL))
    scheduler.add_job(flatten_book, 'cron', day_of_week='mon-fri', hour=15, minute=45)
    scheduler.start()

    ws_thread = threading.Thread(target=run_async_loop_in_thread, daemon=True)
    ws_thread.start()

    logger.info('Execution Engine Fully Started (Cron + WebSockets).')


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    start_execution_engine()

    try:
        import time

        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info('Execution Engine shutting down.')
