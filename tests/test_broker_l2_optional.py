from types import SimpleNamespace

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
