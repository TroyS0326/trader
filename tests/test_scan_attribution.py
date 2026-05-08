from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.modules.setdefault('redis', SimpleNamespace(Redis=SimpleNamespace(from_url=lambda *a, **k: SimpleNamespace(get=lambda *x, **y: None))))
sys.modules.setdefault('requests', SimpleNamespace())
sys.modules.setdefault('stripe', SimpleNamespace(api_key=''))

import app as app_module
import scanner_service


def test_dashboard_scan_persists_user_attribution(monkeypatch):
    user = SimpleNamespace(id=42, is_authenticated=True, first_scan_completed=False, trading_mode='paper', subscription_status='pro')
    saved = {}

    monkeypatch.setattr(app_module, 'current_user', user)
    monkeypatch.setattr(app_module, 'fetch_and_sync_bankroll', lambda *_: None)
    monkeypatch.setattr(app_module, 'run_scan', lambda *_: {'best_pick': {'symbol': 'AAPL', 'decision': 'SKIP', 'setup_grade': 'NO TRADE'}})
    monkeypatch.setattr(app_module, 'validate_scan_payload_contract', lambda *_: {'has_best_pick': True, 'best_pick_key_used': 'best_pick', 'executable_payload_ready': False, 'missing_order_fields': [], 'decision': 'SKIP', 'qty_valid': True, 'payload_shape_notes': []})
    monkeypatch.setattr(app_module, 'get_latest_dynamic_orb_state', lambda: {})
    monkeypatch.setattr(app_module, 'get_failed_trades_today', lambda: 0)
    monkeypatch.setattr(app_module, 'buy_window_open', lambda: True)
    monkeypatch.setattr(app_module, 'approve_scan_for_user', lambda *_: {})
    monkeypatch.setattr(app_module, 'track_user_event', lambda *a, **k: None)
    monkeypatch.setattr(app_module.redis_client, 'setex', lambda *a, **k: None)
    monkeypatch.setattr(app_module.watchlist_manager, 'set_items', lambda *a, **k: None)
    monkeypatch.setattr(app_module, 'get_recent_scans', lambda: [])
    monkeypatch.setattr(app_module, 'get_recent_trades', lambda: [])
    monkeypatch.setattr(app_module.db.session, 'commit', lambda: None)

    def fake_insert_scan(payload):
        saved.update(payload)
        return 100

    monkeypatch.setattr(app_module, 'insert_scan', fake_insert_scan)

    with app_module.app.test_request_context('/api/scan', method='GET'):
        resp = app_module.api_scan.__wrapped__()
        assert resp.status_code == 200

    assert saved['user_id'] == 42
    assert saved['report_user_id'] == 42
    assert saved['trading_mode'] == 'paper'
    assert saved['subscription_status'] == 'pro'
    assert saved['scan_source'] == 'dashboard_manual'


def test_central_scanner_persists_user_attribution(monkeypatch):
    user = SimpleNamespace(id=7, trading_mode='paper', subscription_status='pro')
    saved = {}

    monkeypatch.setattr(scanner_service, '_eligible_users', lambda: [user])
    monkeypatch.setattr(scanner_service, '_run_scan_for_user', lambda *_: {'best_pick': {'symbol': 'MSFT', 'decision': 'WATCH'}})
    monkeypatch.setattr(scanner_service, 'validate_scan_payload_contract', lambda *_: {'has_best_pick': True, 'best_pick_key_used': 'best_pick', 'executable_payload_ready': False, 'missing_order_fields': [], 'decision': 'WATCH', 'qty_valid': True, 'payload_shape_notes': []})
    monkeypatch.setattr(scanner_service, 'approve_scan_for_user', lambda *a, **k: None)
    monkeypatch.setattr(scanner_service, '_dispatch_execution_if_allowed', lambda *a, **k: None)

    def fake_insert_scan(payload):
        saved.update(payload)
        return 55

    monkeypatch.setattr(scanner_service, 'insert_scan', fake_insert_scan)

    class Ctx:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(scanner_service.app, 'app_context', lambda: Ctx())
    scanner_service.run_central_scan_cycle('test-cycle')

    assert saved['user_id'] == 7
    assert saved['report_user_id'] == 7
    assert saved['trading_mode'] == 'paper'
    assert saved['subscription_status'] == 'pro'
    assert saved['scan_source'] == 'central_scanner'
