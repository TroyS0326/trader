import asyncio
import json
import logging
from contextlib import suppress
from typing import Dict
from zoneinfo import ZoneInfo

import requests
import websockets
from apscheduler.schedulers.background import BackgroundScheduler

import config
from broker import maybe_activate_runner_trailing
from db import get_trade_by_target1_id, update_trade_status

logger = logging.getLogger(__name__)

DISCOVERY_SECONDS = 60


def get_user_wss_url(trading_mode: str, sub_status: str) -> str:
    """Dynamically route to the correct WSS stream based on user's active tier."""
    if trading_mode == 'live' and sub_status == 'pro':
        return "wss://api.alpaca.markets/stream"
    return "wss://paper-api.alpaca.markets/stream"


def _alpaca_headers():
    return {
        'accept': 'application/json',
        'APCA-API-KEY-ID': config.ALPACA_API_KEY,
        'APCA-API-SECRET-KEY': config.ALPACA_API_SECRET,
    }


async def _execute_user_kill_switch(user_id: int, base_url: str, token: str):
    """Executes the kill switch for a specific user asynchronously."""
    loop = asyncio.get_running_loop()
    headers = {
        'accept': 'application/json',
        'Authorization': f'Bearer {token}',
    }
    logger.info('Executing kill switch for user %s at %s', user_id, base_url)
    try:
        # Run blocking requests in executor to avoid freezing the event loop
        await loop.run_in_executor(None, lambda: requests.delete(f'{base_url}/v2/orders', headers=headers, timeout=10))
        await loop.run_in_executor(None, lambda: requests.delete(f'{base_url}/v2/positions', headers=headers, timeout=10))
        logger.info('Book flattened for user %s.', user_id)
    except Exception as e:
        logger.error('Kill switch error for user %s: %s', user_id, e)


def flatten_book():
    """15:45 ET Kill Switch: Cancels all open orders and liquidates all positions."""
    logger.info('Executing 15:45 ET Kill Switch: Flattening the book for all users.')

    # Import inside the function to prevent circular imports with app.py
    from app import app
    from models import User
    from broker import get_execution_base_url

    users_data = []

    with app.app_context():
        # Query the database for all users possessing an Alpaca access token
        users = User.query.filter(
            (User._alpaca_access_token.isnot(None)) |
            (User._alpaca_paper_access_token.isnot(None)) |
            (User._alpaca_live_access_token.isnot(None))
        ).all()

        for user in users:
            token = user.alpaca_access_token
            # Verify the decrypted token is valid
            if token and token.strip():
                users_data.append({
                    'id': user.id,
                    'base_url': get_execution_base_url(user),
                    'token': token
                })

    if not users_data:
        logger.info('No active users with tokens found for kill switch.')
        return

    async def _dispatch_kill_switches():
        # Dispatch asynchronous tasks for each user to cancel and liquidate
        tasks = [
            asyncio.create_task(_execute_user_kill_switch(u['id'], u['base_url'], u['token']))
            for u in users_data
        ]
        await asyncio.gather(*tasks)

    # Since APScheduler runs this job in a background thread pool,
    # we can safely use asyncio.run to execute the coroutines concurrently.
    asyncio.run(_dispatch_kill_switches())
    logger.info('Multi-tenant kill switch execution completed.')


class SaaSExecutionManager:
    def __init__(self):
        self.active_streams: Dict[int, asyncio.Task] = {}
        self.running = True

    async def handle_fill(self, user_id: int, order: dict):
        """Processes fills for a specific user."""
        from app import app

        order_id = order.get('id')
        logger.info('USER %s FILL: Order %s filled.', user_id, order_id)
        if not order_id:
            return

        with app.app_context():
            from models import User
            user = User.query.get(user_id)
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
            updated_bundle = maybe_activate_runner_trailing(
                bundle,
                breakeven_price=entry_price,
                token=getattr(user, "alpaca_access_token", None),
                user=user,
            )

            raw_json['order_bundle'] = updated_bundle
            update_trade_status(trade['order_id'], {'raw_json': raw_json})

    async def listen_user_stream(self, user_id: int, token: str, wss_url: str):
        """Maintains a dedicated WebSocket for one user's trade updates."""
        logger.info('Starting stream for User %s at %s', user_id, wss_url)

        retry_delay = 1
        while self.running:
            try:
                async for websocket in websockets.connect(
                    wss_url,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=10,
                ):
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
                    retry_delay = 1
            except Exception as e:
                logger.error('Stream error for User %s: %s', user_id, e)
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 30)

    async def get_connected_users(self) -> list[dict]:
        """
        Fetch users with active Alpaca tokens using SQLAlchemy model properties.

        IMPORTANT:
        - Do not read encrypted token columns directly with raw SQLite.
        - user.alpaca_access_token returns the correct decrypted paper/live token
          based on the user's active trading_mode.
        """
        from app import app
        from models import User

        def _load_users() -> list[dict]:
            with app.app_context():
                rows = []
                users = User.query.filter(
                    (User._alpaca_access_token.isnot(None)) |
                    (User._alpaca_paper_access_token.isnot(None)) |
                    (User._alpaca_live_access_token.isnot(None))
                ).all()

                for user in users:
                    token = user.alpaca_access_token

                    if token and token.strip():
                        rows.append({
                            "id": user.id,
                            "alpaca_access_token": token,
                            "trading_mode": user.trading_mode,
                            "subscription_status": user.subscription_status,
                        })
                return rows

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _load_users)

    async def run_discovery_loop(self):
        """Periodically checks for new users to start listening to."""
        while self.running:
            users = await self.get_connected_users()
            active_ids = {user['id'] for user in users}

            for user in users:
                u_id = user['id']
                if u_id not in self.active_streams:
                    wss_url = get_user_wss_url(user['trading_mode'], user['subscription_status'])
                    task = asyncio.create_task(
                        self.listen_user_stream(u_id, user['alpaca_access_token'], wss_url)
                    )
                    self.active_streams[u_id] = task
                    logger.info('Started stream task for user %s', u_id)
                    await asyncio.sleep(0.5)

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
