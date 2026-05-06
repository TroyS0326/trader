import os
from types import SimpleNamespace

for k in ["SECRET_KEY","TOKEN_ENCRYPTION_KEY","ALPACA_CLIENT_ID","ALPACA_CLIENT_SECRET","ALPACA_REDIRECT_URI","FINNHUB_API_KEY","GEMINI_API_KEY"]:
    os.environ.setdefault(k, "secure-value")

import broker
import execution_guard
import scanner_service


class FakeRedis:
    def __init__(self, payload=None): self.payload = payload
    def get(self, _): return self.payload


def test_execution_base_url_gates_live():
    assert broker.get_execution_base_url(SimpleNamespace(trading_mode='paper', subscription_status='pro')).startswith('https://paper-api')
    assert broker.get_execution_base_url(SimpleNamespace(trading_mode='live', subscription_status='free')).startswith('https://paper-api')
    assert broker.get_execution_base_url(SimpleNamespace(trading_mode='live', subscription_status='pro')) == 'https://api.alpaca.markets'


def test_validate_execution_gates():
    u = SimpleNamespace(id=1, trading_mode='paper', subscription_status='free', alpaca_live_access_token=None)
    assert execution_guard.validate_execution_against_approved_scan(FakeRedis(), u, 'AAPL', None)['ok']
    u.trading_mode = 'live'
    assert 'not PRO' in execution_guard.validate_execution_against_approved_scan(FakeRedis(), u, 'AAPL', 1)['error']


def test_scanner_dispatch_disabled(monkeypatch):
    u = SimpleNamespace(id=1, subscription_status='pro', alpaca_access_token='t', trading_mode='paper', onboarding_completed=True, paper_bankroll_set=True, playbook_reviewed=True, transparency_reviewed=True, broker_connection_started=True)
    called = {'v': False}
    class T: 
        @staticmethod
        def delay(*args, **kwargs): called['v'] = True
    monkeypatch.setenv('CENTRAL_SCANNER_EXECUTION_ENABLED','0')
    monkeypatch.setattr(scanner_service, 'buy_window_open', lambda: True)
    scanner_service._dispatch_execution_if_allowed(u, {'scan_id':1,'best_pick':{'decision':'BUY NOW','symbol':'AAPL','qty':1,'entry_price':1,'stop_price':0.5,'target_1':1.2,'target_2':1.4}})
    assert called['v'] is False
