import logging
import json
import time
import threading
import uuid
import contextlib
from typing import Any, Dict, List
from types import SimpleNamespace

import requests
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

import config
from config import (
    ALPACA_API_KEY,
    ALPACA_API_SECRET,
    ALPACA_DATA_BASE,
    ENTRY_ORDER_POLL_SECONDS,
    ENTRY_ORDER_TIMEOUT_SECONDS,
    TARGET2_TRAILING_STOP_PCT,
    STOCK_L2_ORDERBOOK_CHECK_ENABLED,
    UNPROTECTED_POSITION_REPAIR_ENABLED,
    UNPROTECTED_POSITION_REPAIR_LIVE_ENABLED,
    EMERGENCY_EXIT_SLIPPAGE_PCT,
)
from db import get_current_market_regime

TIMEOUT = 20
logger = logging.getLogger(__name__)


class BrokerError(Exception):
    pass

def _build_client_order_id(user_id: Any, scan_id: Any) -> str:
    """
    Generates a stable, traceable client_order_id for every Alpaca submission.

    Format : xvi-{user_id}-{scan_id}-{8-char uuid hex}
    Example: xvi-42-1891-a3f2c9d1

    Alpaca deduplicates on client_order_id within a short window, so if Celery
    retries the same task the second broker call returns the existing order
    rather than creating a duplicate position.

    Max 50 chars — well within Alpaca's 128-char limit.
    """
    short_uid = uuid.uuid4().hex[:8]
    safe_user = str(user_id or 'u')
    safe_scan = str(scan_id or '0')
    return f"xvi-{safe_user}-{safe_scan}-{short_uid}"

def _snapshot_execution_user_context(user: Any | None, token: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        trading_mode=getattr(user, 'trading_mode', 'paper'),
        subscription_status=getattr(user, 'subscription_status', 'free'),
        alpaca_data_feed=getattr(user, 'alpaca_data_feed', None),
        alpaca_access_token=(
            token
            if token is not None
            else (getattr(user, 'alpaca_access_token', None) if user is not None else None)
        ),
    )


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
        logger.error('Broker GET request failed (url=%s, status=%s).', url, resp.status_code)
        raise BrokerError(resp.text)
    return resp.json()


def _post_json(url: str, payload: Dict[str, Any], token: str | None = None) -> Any:
    resp = _request_with_retry('POST', url, payload=payload, token=token)
    if resp.status_code >= 400:
        logger.error('Broker POST request failed (url=%s, status=%s).', url, resp.status_code)
        raise BrokerError(resp.text)
    return resp.json()


def _patch_json(url: str, payload: Dict[str, Any], token: str | None = None) -> Any:
    resp = _request_with_retry('PATCH', url, payload=payload, token=token)
    if resp.status_code >= 400:
        logger.error('Broker PATCH request failed (url=%s, status=%s).', url, resp.status_code)
        raise BrokerError(resp.text)
    return resp.json()




def _quote_mid_or_usable_price(quote: dict) -> float | None:
    if not isinstance(quote, dict):
        return None
    try:
        ask = float(quote.get('ap') or 0)
    except (TypeError, ValueError):
        ask = 0
    try:
        bid = float(quote.get('bp') or 0)
    except (TypeError, ValueError):
        bid = 0

    if bid > 0 and ask > 0:
        return (bid + ask) / 2
    if ask > 0:
        return ask
    if bid > 0:
        return bid
    return None


def _neutral_order_book_metrics(reason: str = '') -> Dict[str, Any]:
    if reason:
        logger.warning('Using neutral stock L2 metrics: %s', reason)
    return {
        'imbalance_ratio': 1.0,
        'dominant_side': 'unknown',
        'institutional_wall_price': None,
        'institutional_wall_side': None,
    }


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


def analyze_order_book_imbalance(symbol: str, api_client: Any) -> Dict[str, Any]:
    symbol = symbol.upper()
    order_book = api_client.get_latest_orderbook(symbol)

    # Alpaca clients may return either model objects or plain dicts.
    bids = getattr(order_book, 'bids', None)
    asks = getattr(order_book, 'asks', None)
    if bids is None or asks is None:
        bids = (order_book or {}).get('bids', [])
        asks = (order_book or {}).get('asks', [])

    def _price_size(level: Any) -> tuple[float, float]:
        if isinstance(level, dict):
            price = float(level.get('p') or level.get('price') or 0)
            size = float(level.get('s') or level.get('size') or 0)
            return price, size
        price = float(getattr(level, 'p', getattr(level, 'price', 0)) or 0)
        size = float(getattr(level, 's', getattr(level, 'size', 0)) or 0)
        return price, size

    top_bid_volume = sum(_price_size(level)[1] for level in list(bids)[:10])
    top_ask_volume = sum(_price_size(level)[1] for level in list(asks)[:10])

    if top_ask_volume <= 0 and top_bid_volume <= 0:
        imbalance_ratio = 1.0
    elif top_ask_volume <= 0:
        imbalance_ratio = float('inf')
    else:
        imbalance_ratio = top_bid_volume / top_ask_volume

    dominant_side = 'buy' if top_bid_volume >= top_ask_volume else 'sell'

    all_levels = [_price_size(level) for level in list(bids) + list(asks)]
    total_book_volume = sum(size for _, size in all_levels)
    institutional_wall_price = None
    institutional_wall_side = None

    if total_book_volume > 0:
        wall_threshold = total_book_volume * 0.30
        for price, size in [_price_size(level) for level in list(asks)]:
            if size > wall_threshold:
                institutional_wall_price = price
                institutional_wall_side = 'sell'
                break

    return {
        'imbalance_ratio': imbalance_ratio,
        'dominant_side': dominant_side,
        'institutional_wall_price': institutional_wall_price,
        'institutional_wall_side': institutional_wall_side,
    }


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


def _order_filled_qty(order: dict) -> float:
    try:
        value = float((order or {}).get('filled_qty') or 0)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, value)




def parse_broker_error_json(exc_or_text: Any) -> dict:
    text = str(exc_or_text)
    if isinstance(exc_or_text, BrokerError):
        text = str(exc_or_text)
    if not isinstance(text, str):
        return {}
    payload = text.strip()
    if not payload:
        return {}
    try:
        parsed = json.loads(payload)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def get_open_orders(symbol: str | None = None, user: Any | None = None, token: str | None = None) -> list[dict]:
    base_url = get_execution_base_url(user)

    def _fetch(params: dict[str, Any]) -> list[dict]:
        resp = _request_with_retry('GET', f'{base_url}/v2/orders', params=params, token=token)
        if resp.status_code >= 400:
            raise BrokerError(resp.text)
        data = resp.json()
        if not data:
            return []
        return data if isinstance(data, list) else []

    params = {'status': 'open', 'nested': 'true', 'limit': 500}
    if symbol:
        params['symbol'] = symbol.upper()
        try:
            return _fetch(params)
        except BrokerError:
            all_open = _fetch({'status': 'open', 'nested': 'true', 'limit': 500})
            return [o for o in all_open if (o.get('symbol') or '').upper() == symbol.upper()]
    return _fetch(params)


def extract_open_sell_order_coverage(open_orders: list[dict], symbol: str) -> dict:
    active_statuses = {'new', 'accepted', 'pending_new', 'accepted_for_bidding', 'partially_filled', 'held'}
    target = (symbol or '').upper()
    held_qty = 0.0
    order_ids: list[str] = []
    orders: list[dict] = []
    for order in open_orders or []:
        if (order.get('symbol') or '').upper() != target:
            continue
        if (order.get('side') or '').lower() != 'sell':
            continue
        if (order.get('status') or '').lower() not in active_statuses:
            continue
        try:
            qty = float(order.get('qty') or 0)
            filled_qty = float(order.get('filled_qty') or 0)
        except Exception:
            continue
        remaining_qty = max(qty - filled_qty, 0.0)
        if remaining_qty <= 0:
            continue
        held_qty += remaining_qty
        if order.get('id'):
            order_ids.append(order.get('id'))
        orders.append(order)
    return {'held_qty': held_qty, 'order_ids': order_ids, 'orders': orders}
def get_open_position(symbol: str, user: Any | None = None, token: str | None = None) -> dict | None:
    base_url = get_execution_base_url(user)
    resp = _request_with_retry('GET', f'{base_url}/v2/positions/{symbol.upper()}', token=token)
    if resp.status_code == 404:
        return None
    if resp.status_code >= 400:
        raise BrokerError(resp.text)
    return resp.json()


def _poll_for_fill(
    order_id: str,
    timeout_seconds: float,
    token: str | None = None,
    user: Any | None = None,
    on_status: Any | None = None,
) -> Dict[str, Any]:
    started = time.time()
    while True:
        order = get_order(order_id, token=token, user=user)
        if callable(on_status):
            on_status(order)
        status = (order.get('status') or '').lower()
        filled_qty = _order_filled_qty(order)
        if status == 'filled':
            return order
        if status in {'canceled', 'expired', 'done_for_day'} and filled_qty > 0:
            return order
        if status in {'rejected'} and filled_qty <= 0:
            raise BrokerError(f'Entry order {order_id} ended as {status}.')
        if status in {'canceled', 'expired', 'done_for_day'}:
            raise BrokerError(f'Entry order {order_id} ended as {status}.')
        if time.time() - started >= timeout_seconds:
            cancel_order(order_id, token=token, user=user)
            try:
                final_order = get_order(order_id, token=token, user=user)
            except BrokerError:
                if callable(on_status):
                    on_status({'id': order_id, 'status': 'canceled', 'order_status': 'canceled'})
                raise BrokerError(f'Entry order was not filled in {int(timeout_seconds)} seconds and was canceled to avoid slippage.')
            if _order_filled_qty(final_order) > 0:
                if callable(on_status):
                    on_status(final_order)
                return final_order
            if isinstance(final_order, dict):
                canceled_payload = dict(final_order)
                canceled_payload['status'] = 'canceled'
                canceled_payload['order_status'] = 'canceled'
                canceled_payload.setdefault('id', order_id)
            else:
                canceled_payload = {'id': order_id, 'status': 'canceled', 'order_status': 'canceled'}
            if callable(on_status):
                on_status(canceled_payload)
            raise BrokerError(f'Entry order was not filled in {int(timeout_seconds)} seconds and was canceled to avoid slippage.')
        time.sleep(max(0.25, ENTRY_ORDER_POLL_SECONDS))


def _pegged_limit_entry(
    symbol: str,
    qty: int,
    side: str = 'buy',
    user: Any | None = None,
    client_order_id: str | None = None,
) -> Dict[str, Any]:
    """
    Submits a pegged limit entry order.
    P0: Stamps client_order_id on every submission for broker-side
    deduplication and cross-reference during reconciliation.
    """
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

    order_payload: Dict[str, Any] = {
        'symbol': symbol.upper(),
        'qty': str(qty),
        'side': side,
        'type': 'limit',
        'time_in_force': 'day',
        'limit_price': round(peg_price, 2),
    }

    # Stamp client_order_id for broker-side dedup and audit trail
    if client_order_id:
        order_payload['client_order_id'] = client_order_id

    return submit_order(order_payload, token=user_token, user=user)


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
    def _update_entry_trade_status_safely(target_order_id: str, updates: Dict[str, Any]) -> None:
        try:
            from app import app
            from db import update_trade_status
            with app.app_context():
                update_trade_status(target_order_id, updates)
        except Exception as exc:
            logger.error('Failed updating entry trade status for %s: %s', target_order_id, exc)

    def _is_emergency_exit_allowed() -> bool:
        mode = getattr(user, 'trading_mode', 'paper')
        return UNPROTECTED_POSITION_REPAIR_ENABLED if mode != 'live' else UNPROTECTED_POSITION_REPAIR_LIVE_ENABLED

    def _record_unprotected(reason: str, payload: dict | None = None) -> None:
        _update_entry_trade_status_safely(entry_id, {
            'notes': f'unprotected_position_detected:{reason}',
            'raw_json': {'unprotected_position_detected': {'reason': reason, 'payload': payload or {}}},
        })

    def _entry_status_callback(order_payload: Dict[str, Any]) -> None:
        raw_payload = {'latest_entry_order': order_payload}
        updates = {
            'status': order_payload.get('status'),
            'order_status': order_payload.get('status'),
            'filled_avg_price': order_payload.get('filled_avg_price'),
            'filled_qty': order_payload.get('filled_qty'),
            'raw_json': raw_payload,
        }
        _update_entry_trade_status_safely(entry_id, updates)

    try:
        _ = entry_price
        filled_entry = _poll_for_fill(
            entry_id,
            ENTRY_ORDER_TIMEOUT_SECONDS,
            token=user_token,
            user=user,
            on_status=_entry_status_callback,
        )
        status = (filled_entry.get('status') or 'filled').lower()
        _update_entry_trade_status_safely(entry_id, {
            'status': 'filled' if status == 'filled' else 'partially_filled',
            'order_status': status,
            'filled_avg_price': filled_entry.get('filled_avg_price'),
            'filled_qty': filled_entry.get('filled_qty'),
            'raw_json': {'latest_entry_order': filled_entry},
        })
        filled_qty = int(_order_filled_qty(filled_entry) or qty)
        if filled_qty < 1:
            return
        quote = get_latest_quote(symbol, user=user)
        current_sellable = float(quote.get('bp') or quote.get('ap') or filled_entry.get('filled_avg_price') or entry_price or 0)
        if stop_price >= current_sellable > 0:
            if _is_emergency_exit_allowed():
                emergency = place_emergency_exit_order(symbol, filled_qty, user, reason='invalid_stop_for_current_price', reference_order_id=entry_id)
                _update_entry_trade_status_safely(entry_id, {'notes': 'emergency_exit_submitted:invalid_stop_for_current_price', 'raw_json': {'emergency_exit_order': emergency, 'reason': 'invalid_stop_for_current_price', 'source': 'background_leg_placement'}})
            else:
                _record_unprotected('invalid_stop_for_current_price', {'stop_price': stop_price, 'current_sellable': current_sellable})
            return

        qty_target_1 = max(1, filled_qty // 2)
        qty_runner = max(0, filled_qty - qty_target_1)

        target_1_order = submit_order(
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
            runner_stop_order = submit_order(
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
        else:
            runner_stop_order = None

        try:
            from app import app
            from db import get_trade_by_order_id, update_trade_status
            with app.app_context():
                trade = get_trade_by_order_id(entry_id) or {}
                raw = trade.get('raw_json') or {}
                if isinstance(raw, str):
                    import json
                    with contextlib.suppress(Exception):
                        raw = json.loads(raw)
                if not isinstance(raw, dict):
                    raw = {}
                bundle = raw.get('order_bundle') if isinstance(raw.get('order_bundle'), dict) else {}
                bundle['target_1_order_id'] = target_1_order.get('id')
                bundle['runner_stop_order_id'] = (runner_stop_order or {}).get('id')
                bundle['target_1_order'] = target_1_order
                bundle['runner_stop_order'] = runner_stop_order or {}
                raw['order_bundle'] = bundle
                update_trade_status(entry_id, {'raw_json': raw})
        except Exception as exc:
            logger.error('Failed persisting managed leg IDs for %s: %s', entry_id, exc)
    except BrokerError as exc:
        message = str(exc).lower()
        if 'ended as canceled' in message or 'ended as expired' in message or 'ended as rejected' in message or 'ended as done_for_day' in message:
            status = message.split('ended as ', 1)[-1].rstrip('.')
            _update_entry_trade_status_safely(entry_id, {'status': status, 'order_status': status})
        if 'was not filled' in message and 'was canceled' in message:
            _update_entry_trade_status_safely(entry_id, {
                'status': 'canceled',
                'order_status': 'canceled',
                'notes': 'entry timeout cancellation',
                'raw_json': {'managed_leg_placement_failed': 'entry_timeout_cancellation'},
            })
        logger.error('Failed to execute background legs for %s: %s', entry_id, exc)
    except Exception as exc:
        logger.error('Managed leg placement failed after entry fill for %s: %s', entry_id, exc)
        _update_entry_trade_status_safely(entry_id, {'notes': 'managed_leg_placement_failed', 'raw_json': {'managed_leg_placement_failed': str(exc)}})
        try:
            if _is_emergency_exit_allowed():
                emergency = place_emergency_exit_order(symbol, qty, user, reason='managed_leg_placement_failed', reference_order_id=entry_id)
                _update_entry_trade_status_safely(entry_id, {'notes': 'emergency_exit_submitted:managed_leg_placement_failed', 'raw_json': {'managed_leg_placement_failed': str(exc), 'emergency_exit_order': emergency, 'source': 'background_leg_placement'}})
        except Exception as emergency_exc:
            logger.error('Emergency exit attempt failed for %s: %s', entry_id, emergency_exc)
            _update_entry_trade_status_safely(entry_id, {'raw_json': {'managed_leg_placement_failed': str(exc), 'emergency_exit_error': str(emergency_exc)}})


def place_emergency_exit_order(symbol, qty, user, reason, reference_order_id=None) -> dict:
    try:
        numeric_qty = int(float(qty))
    except (TypeError, ValueError):
        raise BrokerError(f'Invalid emergency exit qty for {symbol}: {qty}')
    if numeric_qty <= 0:
        raise BrokerError(f'Invalid emergency exit qty for {symbol}: {qty}')
    quote = get_latest_quote(symbol, user=user)
    anchor = float(quote.get('bp') or quote.get('ap') or 0)
    if anchor <= 0:
        raise BrokerError(f'No valid quote available for emergency exit on {symbol}.')
    limit_price = max(0.01, round(anchor * (1 - EMERGENCY_EXIT_SLIPPAGE_PCT), 2))
    token = getattr(user, 'alpaca_access_token', None) if user else None
    return submit_order({'symbol': symbol.upper(), 'qty': str(numeric_qty), 'side': 'sell', 'type': 'limit', 'time_in_force': 'day', 'limit_price': limit_price}, token=token, user=user)

def place_managed_entry_order(
    symbol: str,
    qty: int,
    entry_price: float,
    stop_price: float,
    target_1_price: float,
    target_2_price: float,
    avg_1m_volume: float = 0.0,
    user: Any | None = None,
    scan_id: Any = None,
) -> Dict[str, Any]:
    """
    Places the managed bracket entry order.
    P0: Accepts scan_id to generate and stamp a client_order_id on the
    entry leg for broker-side deduplication and reconciliation tracing.
    """
    regime_data = get_current_market_regime() or {}
    regime_status = (regime_data.get('regime_status') or 'normal').lower()
    user_token = getattr(user, 'alpaca_access_token', None) if user else None
    execution_user = _snapshot_execution_user_context(user, token=user_token)
    user_token = execution_user.alpaca_access_token

    if regime_status in {'high_volatility', 'chop'}:
        qty = max(1, qty // 2)
        stop_distance = abs(float(entry_price) - float(stop_price))
        tightened_stop_distance = stop_distance * 0.7
        if stop_price <= entry_price:
            stop_price = float(entry_price) - tightened_stop_distance
        else:
            stop_price = float(entry_price) + tightened_stop_distance

    # Microstructure liquidity cap (max 5% of 1-minute volume)
    if avg_1m_volume > 0:
        max_safe_qty = int(0.05 * avg_1m_volume)
        if qty > max_safe_qty:
            qty = max(1, max_safe_qty)

    # Reality check: trade cost must not exceed live broker buying power
    actual_buying_power: float | None = None
    try:
        account_data = _get_json(
            f'{get_execution_base_url(execution_user)}/v2/account', token=user_token
        )
        actual_buying_power = float(account_data.get('buying_power') or 0.0)
    except (BrokerError, TypeError, ValueError) as exc:
        logger.warning('Unable to validate buying power for %s: %s', symbol, exc)

    estimated_cost = float(qty) * float(entry_price)
    if actual_buying_power is not None and estimated_cost > actual_buying_power:
        rejection_reason = (
            f'Account reality check failed. Intended trade cost '
            f'${estimated_cost:.2f} exceeds actual broker buying power '
            f'of ${actual_buying_power:.2f}.'
        )
        logger.warning(rejection_reason)
        return {
            'status': 'rejected',
            'symbol': symbol.upper(),
            'reason': rejection_reason,
        }

    # P0: build client_order_id before touching the broker
    user_id = getattr(execution_user, 'id', None) or getattr(user, 'id', None)
    coid = _build_client_order_id(user_id, scan_id)
    logger.info(
        'place_managed_entry_order user_id=%s symbol=%s scan_id=%s client_order_id=%s',
        user_id, symbol, scan_id, coid,
    )

    _ = target_2_price  # reserved for external broker adapters and journaling
    entry = _pegged_limit_entry(
        symbol=symbol,
        qty=qty,
        side='buy',
        user=execution_user,
        client_order_id=coid,
    )
    entry_id = entry.get('id')
    if not entry_id:
        raise BrokerError('Broker did not return an order id for entry.')

    thread = threading.Thread(
        target=_background_leg_placement,
        args=(entry_id, symbol, qty, entry_price, stop_price, target_1_price, user_token, execution_user),
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
        'client_order_id': coid,
    }

    current_price = float((quote_price if quote_price is not None else 0) or entry_price or 0)
    order_book_metrics = _safe_order_book_metrics(symbol, user_token, execution_user)
    imbalance_ratio = float(order_book_metrics.get('imbalance_ratio') or 0.0)
    institutional_wall_price = order_book_metrics.get('institutional_wall_price')
    institutional_wall_side = (order_book_metrics.get('institutional_wall_side') or '').lower()

    has_massive_sell_pressure = imbalance_ratio > 0 and imbalance_ratio <= (1 / 3)
    has_nearby_sell_wall = (
        institutional_wall_side == 'sell'
        and institutional_wall_price is not None
        and current_price > 0
        and 0 <= (float(institutional_wall_price) - current_price) / current_price <= 0.01
    )

    if has_massive_sell_pressure or has_nearby_sell_wall:
        rejection_reason = 'L2 Liquidity Rejection: Massive sell wall detected.'
        logger.warning(rejection_reason)
        return {
            'status': 'rejected',
            'symbol': symbol.upper(),
            'reason': rejection_reason,
        }

    _ = target_2_price  # reserved for external broker adapters and journaling.
    entry = _pegged_limit_entry(symbol=symbol, qty=qty, side='buy', user=execution_user)
    entry_id = entry.get('id')
    if not entry_id:
        raise BrokerError('Broker did not return an order id for entry.')

    thread = threading.Thread(
        target=_background_leg_placement,
        args=(entry_id, symbol, qty, entry_price, stop_price, target_1_price, user_token, execution_user),
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
