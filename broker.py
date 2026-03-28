from typing import Any, Dict

import requests

from config import ALPACA_API_KEY, ALPACA_API_SECRET, ALPACA_PAPER_BASE

TIMEOUT = 20


class BrokerError(Exception):
    pass


def _headers() -> Dict[str, str]:
    if not ALPACA_API_KEY or not ALPACA_API_SECRET:
        raise BrokerError('Missing Alpaca paper-trading credentials in .env')
    return {
        'accept': 'application/json',
        'content-type': 'application/json',
        'APCA-API-KEY-ID': ALPACA_API_KEY,
        'APCA-API-SECRET-KEY': ALPACA_API_SECRET,
    }


def place_bracket_order(symbol: str, qty: int, entry_price: float, stop_price: float, target_price: float) -> Dict[str, Any]:
    payload = {
        'symbol': symbol,
        'qty': str(qty),
        'side': 'buy',
        'type': 'limit',
        'time_in_force': 'day',
        'limit_price': round(entry_price, 2),
        'order_class': 'bracket',
        'take_profit': {'limit_price': round(target_price, 2)},
        'stop_loss': {'stop_price': round(stop_price, 2)},
    }
    resp = requests.post(f'{ALPACA_PAPER_BASE}/v2/orders', json=payload, headers=_headers(), timeout=TIMEOUT)
    if resp.status_code >= 400:
        raise BrokerError(resp.text)
    return resp.json()


def get_order(order_id: str) -> Dict[str, Any]:
    resp = requests.get(
        f'{ALPACA_PAPER_BASE}/v2/orders/{order_id}',
        params={'nested': 'true'},
        headers=_headers(),
        timeout=TIMEOUT,
    )
    if resp.status_code >= 400:
        raise BrokerError(resp.text)
    return resp.json()
