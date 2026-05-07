import importlib

import pytest
import requests


def test_alpaca_data_feed_accepts_valid_values(monkeypatch):
    monkeypatch.setenv('ALPACA_DATA_FEED', 'sip')
    import config
    config = importlib.reload(config)
    assert config.ALPACA_DATA_FEED == 'sip'


def test_alpaca_data_feed_invalid_defaults_to_iex(monkeypatch):
    monkeypatch.setenv('ALPACA_DATA_FEED', 'not-a-feed')
    import config
    config = importlib.reload(config)
    assert config.ALPACA_DATA_FEED == 'iex'


def test_fetch_latest_bars_uses_no_timeframe(monkeypatch):
    import tasks

    captured = {}

    class DummyResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {'bars': {}}

    def fake_get(url, headers=None, params=None, timeout=None):
        captured['params'] = dict(params or {})
        return DummyResponse()

    monkeypatch.setattr(tasks.requests, 'get', fake_get)
    tasks._fetch_latest_bars(['SPY', 'VIXY'])

    assert captured['params']['symbols'] == 'SPY,VIXY'
    assert captured['params']['feed'] == tasks.config.ALPACA_DATA_FEED
    assert 'timeframe' not in captured['params']


def test_market_regime_survives_latest_bars_failure_with_snapshot_data(monkeypatch):
    import tasks

    monkeypatch.setattr(tasks, '_fetch_snapshot', lambda symbols: {
        'SPY': {
            'latestTrade': {'p': 100},
            'dailyBar': {'h': 100.5, 'l': 100.0, 'c': 100.2},
            'prevDailyBar': {'c': 100.0},
        },
        'VIXY': {
            'latestTrade': {'p': 20},
            'dailyBar': {'h': 21.0, 'l': 19.0, 'c': 20.0},
            'prevDailyBar': {'c': 18.0},
        },
    })

    def boom(symbols):
        raise requests.RequestException('latest bars unavailable')

    monkeypatch.setattr(tasks, '_fetch_latest_bars', boom)

    result = tasks.update_market_regime_task.run()
    assert result['regime_status'] in {'high_volatility', 'normal'}
