from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.modules.setdefault('redis', SimpleNamespace(Redis=SimpleNamespace(from_url=lambda *a, **k: SimpleNamespace(get=lambda *x, **y: None))))
sys.modules.setdefault('requests', SimpleNamespace())
sys.modules.setdefault('stripe', SimpleNamespace(api_key=''))
sys.modules.setdefault('dotenv', SimpleNamespace(load_dotenv=lambda *a, **k: None))

import scanner_effectiveness
import app as app_module


def test_report_handles_no_scans(monkeypatch):
    monkeypatch.setattr(scanner_effectiveness, "get_recent_scans", lambda limit=10: [])
    monkeypatch.setattr(scanner_effectiveness.User, "query", SimpleNamespace(get=lambda uid: None))
    with app_module.app.app_context():
        report = scanner_effectiveness.build_scanner_effectiveness_report(user=None, limit=5)
    assert report["total_scans_analyzed"] == 0


def test_report_counts_decisions_and_missing_fields(monkeypatch):
    scans = [
        {"id": 1, "user_id": 1, "best_pick": {"symbol": "AAPL", "decision": "BUY NOW", "qty": 2, "entry_price": 1, "stop_price": 0.9, "target_1": 1.1, "target_2": 1.2}},
        {"id": 2, "user_id": 1, "best_pick": {"symbol": "MSFT", "decision": "WATCH", "qty": 1, "entry_price": 2}},
    ]
    monkeypatch.setattr(scanner_effectiveness, "get_recent_scans", lambda limit=10: scans)
    monkeypatch.setattr(scanner_effectiveness.User, "query", SimpleNamespace(get=lambda uid: SimpleNamespace(id=uid, trading_mode='paper', subscription_status='pro', alpaca_paper_account_id='x', paper_bankroll_set=True, paper_bankroll=100, onboarding_completed=True, playbook_reviewed=True, transparency_reviewed=True, broker_connection_started=True)))
    with app_module.app.app_context():
        report = scanner_effectiveness.build_scanner_effectiveness_report(limit=10)
    assert report["decision_counts"]["BUY NOW"] == 1
    assert report["decision_counts"]["WATCH"] == 1
    assert report["executable_payload_ready_count"] == 1
    assert report["missing_order_field_counts"]["stop_price"] >= 1


def test_api_scanner_effectiveness_current_user(monkeypatch):
    user = SimpleNamespace(id=77, is_authenticated=True)
    monkeypatch.setattr(app_module, "current_user", user)
    monkeypatch.setattr(app_module, "is_admin_user", lambda: False)
    monkeypatch.setattr(app_module, "build_scanner_effectiveness_report", lambda user=None, limit=50: {"total_scans_analyzed": 1, "scans_by_user_count": {77: 1}})
    with app_module.app.test_request_context('/api/scanner-effectiveness?limit=20', method='GET'):
        resp = app_module.api_scanner_effectiveness.__wrapped__()
        payload = resp.get_json()
        assert payload['ok'] is True
        assert payload['data']['total_scans_analyzed'] == 1
