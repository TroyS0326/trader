from types import SimpleNamespace
import json

import broker


def _user():
    return SimpleNamespace(id=16, alpaca_access_token='user-token', trading_mode='paper', subscription_status='pro')


class _NoopThread:
    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        return None


def _base_patches(monkeypatch):
    monkeypatch.setattr(broker.threading, 'Thread', _NoopThread)
    monkeypatch.setattr(broker, 'get_current_market_regime', lambda: {'regime_status': 'normal'})
    monkeypatch.setattr(broker, '_pegged_limit_entry', lambda **kwargs: {'id': 'entry-1', 'status': 'new'})


def test_l2_disabled_skips_orderbook_and_places_order(monkeypatch):
    _base_patches(monkeypatch)
    monkeypatch.setattr(broker, 'STOCK_L2_ORDERBOOK_CHECK_ENABLED', False)
    monkeypatch.setattr(broker, '_get_json', lambda url, **kwargs: {'buying_power': '100000'} if '/v2/account' in url else {'quotes': {'AAPL': {'ap': 100, 'bp': 99}}})

    called = {'analyze': 0}

    def _fail_analyze(*args, **kwargs):
        called['analyze'] += 1
        raise AssertionError('analyze_order_book_imbalance should not be called when disabled')

    monkeypatch.setattr(broker, 'analyze_order_book_imbalance', _fail_analyze)

    out = broker.place_managed_entry_order('AAPL', 5, 100, 98, 105, 110, user=_user())
    assert out['id'] == 'entry-1'
    assert called['analyze'] == 0


def test_l2_enabled_not_found_falls_back_and_places_order(monkeypatch):
    _base_patches(monkeypatch)
    monkeypatch.setattr(broker, 'STOCK_L2_ORDERBOOK_CHECK_ENABLED', True)

    def _fake_get_json(url, **kwargs):
        if '/v2/account' in url:
            return {'buying_power': '100000'}
        if '/v2/stocks/quotes/latest' in url:
            return {'quotes': {'AAPL': {'ap': 100, 'bp': 99}}}
        raise AssertionError(f'unexpected url {url}')

    monkeypatch.setattr(broker, '_get_json', _fake_get_json)
    monkeypatch.setattr(broker, 'analyze_order_book_imbalance', lambda *args, **kwargs: (_ for _ in ()).throw(broker.BrokerError('{"message":"Not Found"}')))

    out = broker.place_managed_entry_order('AAPL', 5, 100, 98, 105, 110, user=_user())
    assert out['id'] == 'entry-1'


def test_l2_enabled_sell_pressure_rejects_without_order(monkeypatch):
    _base_patches(monkeypatch)
    monkeypatch.setattr(broker, 'STOCK_L2_ORDERBOOK_CHECK_ENABLED', True)
    monkeypatch.setattr(broker, '_get_json', lambda url, **kwargs: {'buying_power': '100000'} if '/v2/account' in url else {'quotes': {'AAPL': {'ap': 100, 'bp': 99}}})
    monkeypatch.setattr(broker, 'analyze_order_book_imbalance', lambda *args, **kwargs: {
        'imbalance_ratio': 0.2,
        'dominant_side': 'sell',
        'institutional_wall_price': None,
        'institutional_wall_side': None,
    })

    order_calls = {'count': 0}

    def _count_order(**kwargs):
        order_calls['count'] += 1
        return {'id': 'entry-1'}

    monkeypatch.setattr(broker, '_pegged_limit_entry', _count_order)
    out = broker.place_managed_entry_order('AAPL', 5, 100, 98, 105, 110, user=_user())
    assert out['status'] == 'rejected'
    assert 'L2 Liquidity Rejection' in out['reason']
    assert order_calls['count'] == 0


def test_buying_power_rejection_still_works(monkeypatch):
    _base_patches(monkeypatch)
    monkeypatch.setattr(broker, 'STOCK_L2_ORDERBOOK_CHECK_ENABLED', False)
    monkeypatch.setattr(broker, '_get_json', lambda url, **kwargs: {'buying_power': '100'})

    out = broker.place_managed_entry_order('AAPL', 5, 100, 98, 105, 110, user=_user())
    assert out['status'] == 'rejected'
    assert 'buying power' in out['reason']


def test_snapshot_execution_user_context_fields():
    user = SimpleNamespace(
        trading_mode='live',
        subscription_status='pro',
        alpaca_data_feed='sip',
        alpaca_access_token='abc',
    )
    snap = broker._snapshot_execution_user_context(user)
    assert isinstance(snap, SimpleNamespace)
    assert snap.trading_mode == 'live'
    assert snap.subscription_status == 'pro'
    assert snap.alpaca_data_feed == 'sip'
    assert snap.alpaca_access_token == 'abc'


def test_get_execution_base_url_with_snapshot_semantics():
    live_pro = SimpleNamespace(trading_mode='live', subscription_status='pro')
    live_free = SimpleNamespace(trading_mode='live', subscription_status='free')
    paper_pro = SimpleNamespace(trading_mode='paper', subscription_status='pro')

    assert broker.get_execution_base_url(live_pro) == 'https://api.alpaca.markets'
    assert broker.get_execution_base_url(live_free) == 'https://paper-api.alpaca.markets'
    assert broker.get_execution_base_url(paper_pro) == 'https://paper-api.alpaca.markets'


def test_resolve_feed_with_snapshot():
    sip = SimpleNamespace(alpaca_data_feed='SIP')
    iex = SimpleNamespace(alpaca_data_feed='iex')
    bad = SimpleNamespace(alpaca_data_feed='bogus')
    assert broker._resolve_feed(sip) == 'sip'
    assert broker._resolve_feed(iex) == 'iex'
    assert broker._resolve_feed(bad) == 'iex'


def test_place_managed_entry_order_passes_snapshot_to_thread(monkeypatch):
    _base_patches(monkeypatch)
    monkeypatch.setattr(broker, 'STOCK_L2_ORDERBOOK_CHECK_ENABLED', False)
    monkeypatch.setattr(broker, '_get_json', lambda url, **kwargs: {'buying_power': '100000'} if '/v2/account' in url else {'quotes': {'AAPL': {'ap': 100, 'bp': 99}}})

    captured = {}

    class _CaptureThread:
        def __init__(self, *args, **kwargs):
            captured['args'] = kwargs.get('args')

        def start(self):
            return None

    monkeypatch.setattr(broker.threading, 'Thread', _CaptureThread)
    user = _user()
    out = broker.place_managed_entry_order('AAPL', 5, 100, 98, 105, 110, user=user)

    assert out['id'] == 'entry-1'
    thread_args = captured['args']
    assert thread_args[-1] is not user
    assert isinstance(thread_args[-1], SimpleNamespace)


def test_background_thread_path_does_not_touch_original_user(monkeypatch):
    _base_patches(monkeypatch)
    monkeypatch.setattr(broker, 'STOCK_L2_ORDERBOOK_CHECK_ENABLED', False)

    class GuardUser:
        def __init__(self):
            self._allow = True

        def freeze(self):
            self._allow = False

        @property
        def trading_mode(self):
            if not self._allow:
                raise RuntimeError('detached trading_mode access')
            return 'paper'

        @property
        def subscription_status(self):
            if not self._allow:
                raise RuntimeError('detached subscription_status access')
            return 'pro'

        @property
        def alpaca_data_feed(self):
            if not self._allow:
                raise RuntimeError('detached alpaca_data_feed access')
            return 'iex'

        @property
        def alpaca_access_token(self):
            if not self._allow:
                raise RuntimeError('detached token access')
            return 'user-token'

    user = GuardUser()
    monkeypatch.setattr(broker, '_get_json', lambda url, **kwargs: {'buying_power': '100000'} if '/v2/account' in url else {'quotes': {'AAPL': {'ap': 100, 'bp': 99}}})
    monkeypatch.setattr(broker, '_poll_for_fill', lambda *args, **kwargs: {'filled_qty': '2', 'status': 'filled'})

    def _submit(payload, token=None, user=None):
        assert isinstance(user, SimpleNamespace)
        return {'id': 'order-1', 'status': 'new', 'qty': payload.get('qty', '1')}

    monkeypatch.setattr(broker, 'submit_order', _submit)

    captured = {}

    class _CaptureThread:
        def __init__(self, *args, **kwargs):
            captured['args'] = kwargs.get('args')
            captured['target'] = kwargs.get('target')

        def start(self):
            user.freeze()
            captured['target'](*captured['args'])

    monkeypatch.setattr(broker.threading, 'Thread', _CaptureThread)
    out = broker.place_managed_entry_order('AAPL', 5, 100, 98, 105, 110, user=user)
    assert out['id'] == 'entry-1'


def test_poll_for_fill_calls_status_callback(monkeypatch):
    seen = []
    monkeypatch.setattr(broker, 'get_order', lambda *args, **kwargs: {'id': 'o1', 'status': 'accepted'} if not seen else {'id': 'o1', 'status': 'filled'})
    monkeypatch.setattr(broker.time, 'sleep', lambda *_: None)
    out = broker._poll_for_fill('o1', 2, on_status=lambda order: seen.append(order.get('status')))
    assert out['status'] == 'filled'
    assert 'accepted' in seen


def test_poll_for_fill_terminal_status_raises_after_callback(monkeypatch):
    seen = []
    monkeypatch.setattr(broker, 'get_order', lambda *args, **kwargs: {'id': 'o2', 'status': 'rejected'})
    try:
        broker._poll_for_fill('o2', 1, on_status=lambda order: seen.append(order.get('status')))
        assert False
    except broker.BrokerError:
        assert seen == ['rejected']


def test_poll_for_fill_canceled_with_partial_returns(monkeypatch):
    monkeypatch.setattr(broker, 'get_order', lambda *args, **kwargs: {'id': 'o4', 'status': 'canceled', 'filled_qty': '1'})
    out = broker._poll_for_fill('o4', 1)
    assert out['status'] == 'canceled'


def test_poll_for_fill_canceled_with_zero_raises(monkeypatch):
    monkeypatch.setattr(broker, 'get_order', lambda *args, **kwargs: {'id': 'o5', 'status': 'canceled', 'filled_qty': '0'})
    try:
        broker._poll_for_fill('o5', 1)
        assert False
    except broker.BrokerError:
        assert True


def test_poll_for_fill_timeout_returns_final_partial(monkeypatch):
    seq = [{'id': 'o6', 'status': 'new', 'filled_qty': '0'}, {'id': 'o6', 'status': 'canceled', 'filled_qty': '1'}]
    monkeypatch.setattr(broker, 'get_order', lambda *args, **kwargs: seq.pop(0))
    monkeypatch.setattr(broker, 'cancel_order', lambda *args, **kwargs: None)
    ticks = {'n': 0}
    monkeypatch.setattr(broker.time, 'time', lambda: (ticks.__setitem__('n', ticks['n'] + 2) or ticks['n']))
    monkeypatch.setattr(broker.time, 'sleep', lambda *_: None)
    out = broker._poll_for_fill('o6', 1)
    assert out['filled_qty'] == '1'


def test_poll_for_fill_timeout_cancels_and_callbacks_canceled(monkeypatch):
    seen = []
    monkeypatch.setattr(broker, 'get_order', lambda *args, **kwargs: {'id': 'o3', 'status': 'new'})
    monkeypatch.setattr(broker, 'cancel_order', lambda *args, **kwargs: None)
    ticks = {'n': 0}
    monkeypatch.setattr(broker.time, 'time', lambda: (ticks.__setitem__('n', ticks['n'] + 2) or ticks['n']))
    monkeypatch.setattr(broker.time, 'sleep', lambda *_: None)
    try:
        broker._poll_for_fill('o3', 1, on_status=lambda order: seen.append(order.get('status')))
        assert False
    except broker.BrokerError:
        assert 'canceled' in seen


def test_parse_broker_error_json_parses_held_payload():
    payload = {"available": "0", "existing_qty": "8", "held_for_orders": "8", "related_orders": ["a"], "symbol": "NVDA"}
    out = broker.parse_broker_error_json(broker.BrokerError(json.dumps(payload)))
    assert out["available"] == "0"
    assert out["related_orders"] == ["a"]


def test_parse_broker_error_json_non_json_returns_empty():
    assert broker.parse_broker_error_json("403 forbidden") == {}


def test_get_open_orders_symbol_fallback_local_filter(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(broker, "get_execution_base_url", lambda user=None: "http://x")
    class R:
        def __init__(self, code, text, body):
            self.status_code = code
            self.text = text
            self._body = body
        def json(self):
            return self._body
    def _fake(method, url, params=None, payload=None, token=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return R(422, "bad symbol", [])
        return R(200, "", [{"id": "1", "symbol": "NVDA"}, {"id": "2", "symbol": "AAPL"}])
    monkeypatch.setattr(broker, "_request_with_retry", _fake)
    out = broker.get_open_orders(symbol="NVDA")
    assert [o["id"] for o in out] == ["1"]


def test_extract_open_sell_order_coverage_active_only():
    orders = [
        {"id": "1", "symbol": "NVDA", "side": "sell", "status": "new", "qty": "10", "filled_qty": "2"},
        {"id": "2", "symbol": "NVDA", "side": "sell", "status": "partially_filled", "qty": "5", "filled_qty": "1"},
    ]
    out = broker.extract_open_sell_order_coverage(orders, "NVDA")
    assert out["held_qty"] == 12
    assert out["order_ids"] == ["1", "2"]


def test_extract_open_sell_order_coverage_ignores_non_matching():
    orders = [
        {"id": "1", "symbol": "NVDA", "side": "buy", "status": "new", "qty": "10", "filled_qty": "0"},
        {"id": "2", "symbol": "NVDA", "side": "sell", "status": "canceled", "qty": "10", "filled_qty": "0"},
        {"id": "3", "symbol": "AAPL", "side": "sell", "status": "new", "qty": "10", "filled_qty": "0"},
    ]
    out = broker.extract_open_sell_order_coverage(orders, "NVDA")
    assert out["held_qty"] == 0
    assert out["order_ids"] == []


def test_poll_for_fill_timeout_get_order_error_still_callbacks_canceled(monkeypatch):
    seen = []
    calls = {'n': 0}

    def _get_order(*args, **kwargs):
        calls['n'] += 1
        if calls['n'] == 1:
            return {'id': 'o7', 'status': 'new'}
        raise broker.BrokerError('transient read error')

    monkeypatch.setattr(broker, 'get_order', _get_order)
    monkeypatch.setattr(broker, 'cancel_order', lambda *args, **kwargs: None)
    ticks = {'n': 0}
    monkeypatch.setattr(broker.time, 'time', lambda: (ticks.__setitem__('n', ticks['n'] + 2) or ticks['n']))
    monkeypatch.setattr(broker.time, 'sleep', lambda *_: None)

    try:
        broker._poll_for_fill('o7', 1, on_status=lambda order: seen.append(order.get('status')))
        assert False
    except broker.BrokerError:
        assert seen[-1] == 'canceled'
