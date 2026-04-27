import logging
import time
import threading
from typing import Any, Dict, List

import requests
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from config import (
    ALPACA_API_KEY,
    ALPACA_API_SECRET,
    ALPACA_DATA_BASE,
    ENTRY_ORDER_POLL_SECONDS,
    ENTRY_ORDER_TIMEOUT_SECONDS,
    TARGET2_TRAILING_STOP_PCT,
)

TIMEOUT = 20
logger = logging.getLogger(__name__)


class BrokerError(Exception):
    pass


def get_execution_base_url(user: Any | None = None) -> str:
    # Safely extract user properties
    trading_mode = getattr(user, 'trading_mode', 'paper')
    sub_status = getattr(user, 'subscription_status', 'free')

    # STRICT GATE: Only route to LIVE if both conditions are met
    if trading_mode == 'live' and sub_status == 'pro':
        return 'https://api.alpaca.markets'

    # Default fallback is ALWAYS the paper environment
    return 'https://paper-api.alpaca.markets'


def _is_retryable_request_error(exc: BaseException) -> bool:
    if isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
        return True
    if isinstance(exc, requests.exceptions.HTTPError):
        response = getattr(exc, 'response', None)
        return bool(response is not None and response.status_code >= 500)
    return False


def _headers(token: str | None = None) -> Dict[str, str]:
    """
    If a token is provided, use OAuth Bearer auth.
    Otherwise, fall back to master keys (used for the scanner).
    """
    if token:
        return {
            'accept': 'application/json',
            'content-type': 'application/json',
            'Authorization': f'Bearer {token}',
        }

    if not ALPACA_API_KEY or not ALPACA_API_SECRET:
        raise BrokerError('Missing Alpaca paper-trading credentials in .env')
    return {
        'accept': 'application/json',
        'content-type': 'application/json',
        'APCA-API-KEY-ID': ALPACA_API_KEY,
        'APCA-API-SECRET-KEY': ALPACA_API_SECRET,
    }


def _get_json(url: str, params: Dict[str, Any] | None = None, token: str | None = None) -> Any:
    resp = _request_with_retry('GET', url, params=params, token=token)
    if resp.status_code >= 400:
        raise BrokerError(resp.text)
    return resp.json()


def _post_json(url: str, payload: Dict[str, Any], token: str | None = None) -> Any:
    resp = _request_with_retry('POST', url, payload=payload, token=token)
    if resp.status_code >= 400:
        raise BrokerError(resp.text)
    return resp.json()


def _patch_json(url: str, payload: Dict[str, Any], token: str | None = None) -> Any:
    resp = _request_with_retry('PATCH', url, payload=payload, token=token)
    if resp.status_code >= 400:
        raise BrokerError(resp.text)
    return resp.json()


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=10),
    retry=retry_if_exception(_is_retryable_request_error),
    reraise=True,
)
def _request_with_retry(
    method: str,
    url: str,
    params: Dict[str, Any] | None = None,
    payload: Dict[str, Any] | None = None,
    token: str | None = None,
) -> requests.Response:
    resp = requests.request(
        method=method,
        url=url,
        params=params or {},
        json=payload,
        headers=_headers(token),
        timeout=TIMEOUT,
    )
    if resp.status_code >= 500:
        resp.raise_for_status()
    return resp


def _resolve_feed(user: Any | None = None) -> str:
    candidate = (getattr(user, 'alpaca_data_feed', '') or '').strip().lower()
    return candidate if candidate in {'iex', 'sip'} else 'iex'


def get_latest_quote(symbol: str, user: Any | None = None) -> Dict[str, Any]:
    symbol = symbol.upper()
    data = _get_json(
        f'{ALPACA_DATA_BASE}/v2/stocks/quotes/latest',
        params={'symbols': symbol, 'feed': _resolve_feed(user)},
    )
    return (data.get('quotes') or {}).get(symbol, {})


def submit_order(payload: Dict[str, Any], token: str | None = None, user: Any | None = None) -> Dict[str, Any]:
    base_url = get_execution_base_url(user)
    return _post_json(f'{base_url}/v2/orders', payload, token=token)


def replace_order(
    order_id: str,
    payload: Dict[str, Any],
    token: str | None = None,
    user: Any | None = None,
) -> Dict[str, Any]:
    base_url = get_execution_base_url(user)
    return _patch_json(f'{base_url}/v2/orders/{order_id}', payload, token=token)


def cancel_order(order_id: str, token: str | None = None, user: Any | None = None) -> None:
    base_url = get_execution_base_url(user)
    resp = _request_with_retry('DELETE', f'{base_url}/v2/orders/{order_id}', token=token)
    if resp.status_code not in {200, 204, 404, 422}:
        raise BrokerError(resp.text)


def get_order(order_id: str, token: str | None = None, user: Any | None = None) -> Dict[str, Any]:
    base_url = get_execution_base_url(user)
    resp = _request_with_retry(
        'GET',
        f'{base_url}/v2/orders/{order_id}',
        params={'nested': 'true'},
        token=token,
    )
    if resp.status_code >= 400:
        raise BrokerError(resp.text)
    return resp.json()


def get_orders(order_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for oid in order_ids:
        if not oid:
            continue
        try:
            out[oid] = get_order(oid)
        except BrokerError:
            continue
    return out


def _poll_for_fill(
    order_id: str,
    timeout_seconds: float,
    token: str | None = None,
    user: Any | None = None,
) -> Dict[str, Any]:
    started = time.time()
    while True:
        order = get_order(order_id, token=token, user=user)
        status = (order.get('status') or '').lower()
        if status == 'filled':
            return order
        if status in {'canceled', 'expired', 'rejected', 'done_for_day'}:
            raise BrokerError(f'Entry order {order_id} ended as {status}.')
        if time.time() - started >= timeout_seconds:
            cancel_order(order_id, token=token, user=user)
            raise BrokerError(
                f'Entry order was not filled in {int(timeout_seconds)} seconds and was canceled to avoid slippage.'
            )
        time.sleep(max(0.25, ENTRY_ORDER_POLL_SECONDS))


def _pegged_limit_entry(symbol: str, qty: int, side: str = 'buy', user: Any | None = None) -> Dict[str, Any]:
    user_token = getattr(user, 'alpaca_access_token', None) if user else None
    quote = get_latest_quote(symbol, user=user)
    ask = float(quote.get('ap') or 0)
    bid = float(quote.get('bp') or 0)
    if side == 'buy':
        peg_price = ask or bid
    else:
        peg_price = bid or ask
    if peg_price <= 0:
        raise BrokerError(f'No valid quote available to peg entry order for {symbol}.')
    return submit_order(
        {
            'symbol': symbol.upper(),
            'qty': str(qty),
            'side': side,
            'type': 'limit',
            'time_in_force': 'day',
            'limit_price': round(peg_price, 2),
        },
        token=user_token,
        user=user,
    )


def _background_leg_placement(
    entry_id: str,
    symbol: str,
    qty: int,
    entry_price: float,
    stop_price: float,
    target_1_price: float,
    user_token: str | None,
    user: Any | None,
) -> None:
    """Runs asynchronously to prevent blocking the main Flask API thread."""
    try:
        _ = entry_price
        filled_entry = _poll_for_fill(entry_id, ENTRY_ORDER_TIMEOUT_SECONDS, token=user_token, user=user)
        filled_qty = int(float(filled_entry.get('filled_qty') or qty))
        if filled_qty < 1:
            return

        qty_target_1 = max(1, filled_qty // 2)
        qty_runner = max(0, filled_qty - qty_target_1)

        submit_order(
            {
                'symbol': symbol.upper(),
                'qty': str(qty_target_1),
                'side': 'sell',
                'type': 'limit',
                'time_in_force': 'day',
                'order_class': 'oco',
                'take_profit': {
                    'limit_price': round(target_1_price, 2),
                },
                'stop_loss': {
                    'stop_price': round(stop_price, 2),
                },
            },
            token=user_token,
            user=user,
        )

        if qty_runner > 0:
            submit_order(
                {
                    'symbol': symbol.upper(),
                    'qty': str(qty_runner),
                    'side': 'sell',
                    'type': 'stop',
                    'time_in_force': 'day',
                    'stop_price': round(stop_price, 2),
                },
                token=user_token,
                user=user,
            )
    except BrokerError as exc:
        logger.error('Failed to execute background legs for %s: %s', entry_id, exc)


def place_managed_entry_order(
    symbol: str,
    qty: int,
    entry_price: float,
    stop_price: float,
    target_1_price: float,
    target_2_price: float,
    avg_1m_volume: float = 0.0,
    user: Any | None = None,
) -> Dict[str, Any]:
    user_token = getattr(user, 'alpaca_access_token', None) if user else None
    # Microstructure liquidity cap (max 5% of 1-minute volume).
    if avg_1m_volume > 0:
        max_safe_qty = int(0.05 * avg_1m_volume)
        if qty > max_safe_qty:
            qty = max(1, max_safe_qty)

    # Reality check: ensure intended trade cost does not exceed live broker buying power.
    actual_buying_power: float | None = None
    try:
        account_data = _get_json(f'{get_execution_base_url(user)}/v2/account', token=user_token)
        actual_buying_power = float(account_data.get('buying_power') or 0.0)
    except (BrokerError, TypeError, ValueError) as exc:
        logger.warning('Unable to validate buying power for %s: %s', symbol, exc)

    estimated_cost = float(qty) * float(entry_price)
    if actual_buying_power is not None and estimated_cost > actual_buying_power:
        return {
            'status': 'rejected',
            'symbol': symbol.upper(),
            'reason': (
                'Account reality check failed. Intended trade cost '
                f'${estimated_cost:.2f} exceeds actual broker buying power '
                f'of ${actual_buying_power:.2f}.'
            ),
        }

    _ = target_2_price  # reserved for external broker adapters and journaling.
    entry = _pegged_limit_entry(symbol=symbol, qty=qty, side='buy', user=user)
    entry_id = entry.get('id')
    if not entry_id:
        raise BrokerError('Broker did not return an order id for entry.')

    thread = threading.Thread(
        target=_background_leg_placement,
        args=(entry_id, symbol, qty, entry_price, stop_price, target_1_price, user_token, user),
        daemon=True,
    )
    thread.start()

    return {
        'id': entry_id,
        'status': entry.get('status', 'new'),
        'symbol': symbol.upper(),
        'filled_qty': '0',
        'strategy': 'target1_then_trailing_runner',
        'entry_order': entry,
        'runner_trailing_pct': TARGET2_TRAILING_STOP_PCT,
    }


def maybe_activate_runner_trailing(
    raw_trade_payload: Dict[str, Any],
    breakeven_price: float,
    token: str | None = None,
    user: Any | None = None,
) -> Dict[str, Any]:
    if (raw_trade_payload or {}).get('strategy') != 'target1_then_trailing_runner':
        return raw_trade_payload
    if raw_trade_payload.get('runner_trailing_activated'):
        return raw_trade_payload

    target_1_id = raw_trade_payload.get('target_1_order_id')
    runner_stop_id = raw_trade_payload.get('runner_stop_order_id')
    if not target_1_id or not runner_stop_id:
        return raw_trade_payload

    target_1 = get_order(target_1_id, token=token, user=user)
    if (target_1.get('status') or '').lower() != 'filled':
        return raw_trade_payload

    # Lock in a "base hit": move stop to breakeven first, then convert to trailing.
    replace_order(runner_stop_id, {'stop_price': round(breakeven_price, 2)}, token=token, user=user)
    cancel_order(runner_stop_id, token=token, user=user)
    runner_qty = int(float(target_1.get('qty') or 0))
    remaining_qty = int(float(raw_trade_payload.get('filled_qty') or 0)) - runner_qty
    if remaining_qty < 1:
        raw_trade_payload['runner_trailing_activated'] = True
        return raw_trade_payload

    trailing = submit_order(
        {
            'symbol': raw_trade_payload.get('symbol'),
            'qty': str(remaining_qty),
            'side': 'sell',
            'type': 'trailing_stop',
            'time_in_force': 'day',
            'trail_percent': str(round(TARGET2_TRAILING_STOP_PCT, 4)),
        },
        token=token,
        user=user,
    )
    raw_trade_payload['runner_trailing_activated'] = True
    raw_trade_payload['runner_trailing_order_id'] = trailing.get('id')
    raw_trade_payload['runner_breakeven_price'] = round(breakeven_price, 2)
    return raw_trade_payload
