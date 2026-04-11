import asyncio
import json
import logging
from contextlib import suppress
from dataclasses import dataclass
from typing import Dict, Optional
import threading
from zoneinfo import ZoneInfo

import aiosqlite
import requests
import websockets
from apscheduler.schedulers.background import BackgroundScheduler

from broker import maybe_activate_runner_trailing
from config import ALPACA_API_KEY, ALPACA_API_SECRET, ALPACA_PAPER_BASE, DB_PATH, TIMEZONE_LABEL
from db import update_trade_status

logger = logging.getLogger(__name__)

# For live trading later: wss://api.alpaca.markets/stream
ALPACA_WSS_URL = ALPACA_PAPER_BASE.replace('https', 'wss') + '/stream'
DISCOVERY_SECONDS = 15


@dataclass
class UserStreamConfig:
    user_id: int
    api_key: str
    api_secret: str


def _parse_user_token(token: str) -> Optional[tuple[str, str]]:
    # Temporary support for token values stored as "api_key:api_secret".
    if ':' not in token:
        return None
    api_key, api_secret = token.split(':', 1)
    api_key = api_key.strip()
    api_secret = api_secret.strip()
    if not api_key or not api_secret:
        return None
    return api_key, api_secret


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
    """Backward-compatible wrapper for old callsites."""
    await handle_fill_event_for_user(user_id=0, order=order)


async def _find_trade_for_fill(user_id: int, order_id: str) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            '''
            SELECT * FROM trades
            WHERE user_id = ? AND order_id = ?
            ORDER BY id DESC
            LIMIT 1
            ''',
            (user_id, order_id),
        )
        row = await cur.fetchone()
        if row:
            return dict(row)

        # Fallback path for target orders where the stream order_id is stored in raw_json.
        cur = await conn.execute(
            '''
            SELECT * FROM trades
            WHERE user_id = ?
              AND json_extract(raw_json, '$.order_bundle.target_1_order_id') = ?
            ORDER BY id DESC
            LIMIT 1
            ''',
            (user_id, order_id),
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def handle_fill_event_for_user(user_id: int, order: dict):
    """Processes fills instantly and scopes updates to the owning user."""
    order_id = order.get('id')
    filled_qty = order.get('filled_qty')

    logger.info(f'WS EVENT: User {user_id} order {order_id} filled for {filled_qty} shares.')
    if not order_id:
        return

    trade = await _find_trade_for_fill(user_id=user_id, order_id=order_id)
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


async def _discover_active_user_streams() -> Dict[int, UserStreamConfig]:
    users: Dict[int, UserStreamConfig] = {}
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            '''
            SELECT id, alpaca_access_token
            FROM user
            WHERE alpaca_access_token IS NOT NULL
              AND trim(alpaca_access_token) != ''
            '''
        )
        rows = await cur.fetchall()
    for row in rows:
        parsed = _parse_user_token(row['alpaca_access_token'])
        if not parsed:
            logger.warning('Skipping user %s: access token is not in key:secret format.', row['id'])
            continue
        api_key, api_secret = parsed
        users[row['id']] = UserStreamConfig(user_id=row['id'], api_key=api_key, api_secret=api_secret)
    return users


async def _alpaca_trade_listener_for_user(config: UserStreamConfig):
    logger.info('Connecting user %s to Alpaca stream...', config.user_id)
    while True:
        try:
            async for websocket in websockets.connect(ALPACA_WSS_URL):
                try:
                    auth_msg = {'action': 'auth', 'key': config.api_key, 'secret': config.api_secret}
                    await websocket.send(json.dumps(auth_msg))
                    auth_reply = await websocket.recv()
                    logger.info('User %s auth reply: %s', config.user_id, auth_reply)

                    sub_msg = {'action': 'listen', 'data': {'streams': ['trade_updates']}}
                    await websocket.send(json.dumps(sub_msg))
                    sub_reply = await websocket.recv()
                    logger.info('User %s sub reply: %s', config.user_id, sub_reply)

                    async for message in websocket:
                        data = json.loads(message)
                        if data.get('stream') != 'trade_updates':
                            continue
                        event = data.get('data', {}).get('event')
                        order = data.get('data', {}).get('order', {})
                        if event in ('fill', 'partial_fill'):
                            await handle_fill_event_for_user(config.user_id, order)
                except websockets.ConnectionClosed:
                    logger.warning('User %s websocket closed. Reconnecting...', config.user_id)
                    await asyncio.sleep(1)
                except Exception as e:
                    logger.error('User %s websocket error: %s', config.user_id, e)
                    await asyncio.sleep(1)
        except Exception as e:
            logger.error('User %s stream outer loop error: %s', config.user_id, e)
            await asyncio.sleep(2)


async def run_multi_user_stream_manager():
    tasks: Dict[int, asyncio.Task] = {}
    while True:
        active_users = await _discover_active_user_streams()
        active_ids = set(active_users.keys())
        running_ids = set(tasks.keys())

        for user_id in sorted(active_ids - running_ids):
            task = asyncio.create_task(_alpaca_trade_listener_for_user(active_users[user_id]))
            tasks[user_id] = task
            logger.info('Started stream task for user %s', user_id)

        for user_id in sorted(running_ids - active_ids):
            task = tasks.pop(user_id)
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            logger.info('Stopped stream task for user %s (token removed).', user_id)

        await asyncio.sleep(DISCOVERY_SECONDS)


def run_async_loop_in_thread():
    """Runs the asyncio manager loop in a background OS thread."""
    with suppress(Exception):
        import uvloop

        uvloop.install()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(run_multi_user_stream_manager())


def start_execution_engine():
    """Starts the background execution managers (Cron + WebSocket)."""
    scheduler = BackgroundScheduler(timezone=ZoneInfo(TIMEZONE_LABEL))
    scheduler.add_job(flatten_book, 'cron', day_of_week='mon-fri', hour=15, minute=45)
    scheduler.start()

    ws_thread = threading.Thread(target=run_async_loop_in_thread, daemon=True)
    ws_thread.start()

    logger.info('Execution Engine Fully Started (Cron + Multi-User WebSockets).')


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    start_execution_engine()

    try:
        import time

        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info('Execution Engine shutting down.')
