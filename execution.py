import asyncio
import json
import logging
from contextlib import suppress
from typing import Dict
from zoneinfo import ZoneInfo

import aiosqlite
import requests
import websockets
from apscheduler.schedulers.background import BackgroundScheduler

import config
from app import app
from broker import maybe_activate_runner_trailing
from db import get_trade_by_target1_id, update_trade_status

logger = logging.getLogger(__name__)

# Sandbox WSS endpoint for paper trading
ALPACA_WSS_URL = "wss://broker-api.sandbox.alpaca.markets/stream"
DISCOVERY_SECONDS = 60


def _alpaca_headers():
    return {
        'accept': 'application/json',
        'APCA-API-KEY-ID': config.ALPACA_API_KEY,
        'APCA-API-SECRET-KEY': config.ALPACA_API_SECRET,
    }


def flatten_book():
    """15:45 ET Kill Switch: Cancels all open orders and liquidates all positions."""
    logger.info('Executing 15:45 ET Kill Switch: Flattening the book.')
    try:
        requests.delete(f'{config.ALPACA_PAPER_BASE}/v2/orders', headers=_alpaca_headers(), timeout=10)
        requests.delete(f'{config.ALPACA_PAPER_BASE}/v2/positions', headers=_alpaca_headers(), timeout=10)
        logger.info('Book flattened.')
    except Exception as e:
        logger.error('Kill switch error: %s', e)


class SaaSExecutionManager:
    def __init__(self):
        self.active_streams: Dict[int, asyncio.Task] = {}
        self.running = True

    async def handle_fill(self, user_id: int, order: dict):
        """Processes fills for a specific user."""
        order_id = order.get('id')
        logger.info('USER %s FILL: Order %s filled.', user_id, order_id)
        if not order_id:
            return

        with app.app_context():
            trade = get_trade_by_target1_id(order_id, user_id=user_id)
            if not trade:
                return

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

    async def listen_user_stream(self, user_id: int, token: str):
        """Maintains a dedicated WebSocket for one user's trade updates."""
        logger.info('Starting stream for User %s', user_id)

        async for websocket in websockets.connect(ALPACA_WSS_URL):
            try:
                auth_msg = {"action": "auth", "key": token}
                await websocket.send(json.dumps(auth_msg))

                sub_msg = {"action": "listen", "data": {"streams": ["trade_updates"]}}
                await websocket.send(json.dumps(sub_msg))

                async for message in websocket:
                    data = json.loads(message)
                    if data.get('stream') == 'trade_updates':
                        event = data.get('data', {}).get('event')
                        if event in ('fill', 'partial_fill'):
                            await self.handle_fill(user_id, data.get('data', {}).get('order', {}))
            except Exception as e:
                logger.error('Stream error for User %s: %s', user_id, e)
                await asyncio.sleep(5)

    async def get_connected_users(self):
        """Fetches all users who have linked their Alpaca accounts."""
        async with aiosqlite.connect(config.DB_PATH) as conn:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute(
                """
                SELECT id, alpaca_access_token
                FROM user
                WHERE alpaca_access_token IS NOT NULL
                  AND trim(alpaca_access_token) != ''
                """
            )
            rows = await cur.fetchall()
        return rows

    async def run_discovery_loop(self):
        """Periodically checks for new users to start listening to."""
        while self.running:
            users = await self.get_connected_users()
            active_ids = {user['id'] for user in users}

            for user in users:
                u_id = user['id']
                if u_id not in self.active_streams:
                    task = asyncio.create_task(
                        self.listen_user_stream(u_id, user['alpaca_access_token'])
                    )
                    self.active_streams[u_id] = task
                    logger.info('Started stream task for user %s', u_id)

            removed_ids = [u_id for u_id in self.active_streams if u_id not in active_ids]
            for u_id in removed_ids:
                task = self.active_streams.pop(u_id)
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
                logger.info('Stopped stream task for user %s (token removed).', u_id)

            await asyncio.sleep(DISCOVERY_SECONDS)


def start_engine():
    manager = SaaSExecutionManager()
    loop = asyncio.get_event_loop()

    scheduler = BackgroundScheduler(timezone=ZoneInfo(config.TIMEZONE_LABEL))
    # Note: flatten_book must be updated to iterate over all active user tokens.
    scheduler.add_job(flatten_book, 'cron', day_of_week='mon-fri', hour=15, minute=45)
    scheduler.start()

    try:
        loop.run_until_complete(manager.run_discovery_loop())
    except KeyboardInterrupt:
        manager.running = False
        logger.info('SaaS Execution Engine shutting down.')


def start_execution_engine():
    """Backward-compatible app entrypoint."""
    start_engine()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    start_engine()
