from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.modules.setdefault('redis', SimpleNamespace(Redis=SimpleNamespace(from_url=lambda *a, **k: SimpleNamespace(get=lambda *x, **y: None))))
sys.modules.setdefault('requests', SimpleNamespace())
sys.modules.setdefault('stripe', SimpleNamespace(api_key=''))
sys.modules.setdefault('dotenv', SimpleNamespace(load_dotenv=lambda *a, **k: None))

import scanner_effectiveness
import app as app_module


class FakeRedis:
    def __init__(self, mapping):
        self.mapping = mapping

    def get(self, key):
        return self.mapping.get(key)


def _set_redis(monkeypatch, mapping):
    monkeypatch.setattr(app_module, "redis_client", FakeRedis(mapping))


def _stub_user_query(monkeypatch):
    monkeypatch.setattr(scanner_effectiveness.User, "query", SimpleNamespace(get=lambda uid: SimpleNamespace(id=uid, trading_mode='paper', subscription_status='pro', alpaca_paper_account_id='x', paper_bankroll_set=True, paper_bankroll=100, onboarding_completed=True, playbook_reviewed=True, transparency_reviewed=True, broker_connection_started=True)))


def test_db_payload_json_best_pick_counted(monkeypatch):
    row = {"id": 5, "created_at": datetime.now(timezone.utc).isoformat(), "best_symbol": "AAPL", "best_decision": "BUY NOW", "payload_json": json.dumps({"scan_id": "s-1", "user_id": 1, "best_pick": {"symbol": "AAPL", "decision": "BUY NOW", "qty": 2, "entry_price": 10, "stop_price": 9, "target_1": 11, "target_2": 12}})}
    monkeypatch.setattr(scanner_effectiveness, "get_recent_scans", lambda limit=10: [row])
    _set_redis(monkeypatch, {})
    _stub_user_query(monkeypatch)
    with app_module.app.app_context():
        report = scanner_effectiveness.build_scanner_effectiveness_report(limit=10)
    assert report["best_pick_present_count"] == 1
    assert report["decision_counts"]["BUY NOW"] == 1


def test_filter_uses_payload_user_id(monkeypatch):
    row = {"id": 6, "created_at": datetime.now(timezone.utc).isoformat(), "payload_json": json.dumps({"scan_id": "s-2", "user_id": 77, "best_pick": {"symbol": "MSFT", "decision": "WATCH"}})}
    monkeypatch.setattr(scanner_effectiveness, "get_recent_scans", lambda limit=10: [row])
    _set_redis(monkeypatch, {})
    _stub_user_query(monkeypatch)
    with app_module.app.app_context():
        report = scanner_effectiveness.build_scanner_effectiveness_report(user=SimpleNamespace(id=77), limit=10)
    assert report["total_scans_analyzed"] == 1


def test_current_user_api_excludes_other_users(monkeypatch):
    rows = [
        {"id": 10, "created_at": datetime.now(timezone.utc).isoformat(), "payload_json": json.dumps({"scan_id": "s-10", "user_id": 77, "best_pick": {"symbol": "AAPL", "decision": "WATCH"}})},
        {"id": 11, "created_at": datetime.now(timezone.utc).isoformat(), "payload_json": json.dumps({"scan_id": "s-11", "user_id": 88, "best_pick": {"symbol": "TSLA", "decision": "WATCH"}})},
    ]
    monkeypatch.setattr(scanner_effectiveness, "get_recent_scans", lambda limit=10: rows)
    _set_redis(monkeypatch, {})
    _stub_user_query(monkeypatch)
    user = SimpleNamespace(id=77, is_authenticated=True)
    monkeypatch.setattr(app_module, "current_user", user)
    monkeypatch.setattr(app_module, "is_admin_user", lambda: False)
    with app_module.app.test_request_context('/api/scanner-effectiveness?limit=20', method='GET'):
        payload = app_module.api_scanner_effectiveness.__wrapped__().get_json()["data"]
    assert payload["total_scans_analyzed"] == 1
    assert payload["scans_by_user_count"] == {"77": 1} or payload["scans_by_user_count"] == {77: 1}


def test_invalid_payload_json_is_flagged_and_not_executable(monkeypatch):
    row = {"id": 12, "created_at": datetime.now(timezone.utc).isoformat(), "best_symbol": "NVDA", "best_decision": "BUY NOW", "payload_json": "{"}
    monkeypatch.setattr(scanner_effectiveness, "get_recent_scans", lambda limit=10: [row])
    _set_redis(monkeypatch, {})
    _stub_user_query(monkeypatch)
    with app_module.app.app_context():
        report = scanner_effectiveness.build_scanner_effectiveness_report(limit=10)
    assert report["executable_payload_ready_count"] == 0
    sample = report["sample_recent_failures"][0]
    assert "PAYLOAD_JSON_MISSING_OR_INVALID" in sample["payload_shape_notes"]


def test_redis_and_db_both_included_and_deduped(monkeypatch):
    row = {"id": 20, "created_at": datetime.now(timezone.utc).isoformat(), "payload_json": json.dumps({"scan_id": "shared", "user_id": 1, "best_pick": {"symbol": "AAPL", "decision": "WATCH"}})}
    monkeypatch.setattr(scanner_effectiveness, "get_recent_scans", lambda limit=10: [row])
    _set_redis(monkeypatch, {
        "latest_scan:1": json.dumps({"scan_id": "shared", "user_id": 1, "best_pick": {"symbol": "AAPL", "decision": "WATCH"}}),
        "latest_scan:2": json.dumps({"scan_id": "other", "user_id": 2, "best_pick": {"symbol": "TSLA", "decision": "WATCH"}}),
    })
    _stub_user_query(monkeypatch)
    with app_module.app.app_context():
        report = scanner_effectiveness.build_scanner_effectiveness_report(limit=10)
    assert report["total_scans_analyzed"] == 2
    assert report["source_counts"]["db_recent"] == 1
    assert report["source_counts"]["redis_latest"] == 1


def test_latest_age_uses_db_without_redis(monkeypatch):
    created = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
    row = {"id": 30, "created_at": created, "payload_json": json.dumps({"user_id": 1, "best_pick": {"symbol": "AAPL", "decision": "WATCH"}})}
    monkeypatch.setattr(scanner_effectiveness, "get_recent_scans", lambda limit=10: [row])
    _set_redis(monkeypatch, {})
    _stub_user_query(monkeypatch)
    with app_module.app.app_context():
        report = scanner_effectiveness.build_scanner_effectiveness_report(limit=10)
    assert isinstance(report["latest_scan_age_seconds"], int)
    assert report["latest_scan_age_seconds"] >= 0


def test_safe_samples_do_not_expose_payload_json(monkeypatch):
    row = {"id": 40, "created_at": datetime.now(timezone.utc).isoformat(), "payload_json": json.dumps({"user_id": 1, "best_pick": {"symbol": "AAPL", "decision": "WATCH"}, "alpaca_live_access_token": "secret"})}
    monkeypatch.setattr(scanner_effectiveness, "get_recent_scans", lambda limit=10: [row])
    _set_redis(monkeypatch, {})
    _stub_user_query(monkeypatch)
    with app_module.app.app_context():
        report = scanner_effectiveness.build_scanner_effectiveness_report(limit=10)
    sample = report["sample_recent_failures"][0]
    assert "payload_json" not in sample
    assert "alpaca_live_access_token" not in sample


def test_effectiveness_aggregates_skip_reasons_and_attribution(monkeypatch):
    row = {
        "id": 41,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "payload_json": json.dumps({
            "user_id": 9,
            "best_pick": {
                "symbol": "ATRA",
                "decision": "SKIP",
                "setup_grade": "NO TRADE",
                "score_total": 58,
                "scores": {"catalyst": 6, "liquidity": 4},
                "details": {"skip_reason": "BUY_WINDOW_CLOSED", "skip_reasons": ["BUY_WINDOW_CLOSED", "SETUP_GRADE_NO_TRADE"], "decision_reason": "Setup grade is NO TRADE"},
            },
        }),
    }
    monkeypatch.setattr(scanner_effectiveness, "get_recent_scans", lambda limit=10: [row])
    _set_redis(monkeypatch, {})
    _stub_user_query(monkeypatch)
    with app_module.app.app_context():
        report = scanner_effectiveness.build_scanner_effectiveness_report(limit=10)
    assert report["attributed_scan_count"] == 1
    assert report["unattributed_scan_count"] == 0
    assert report["user_context_missing_count"] == 0
    assert report["skip_reason_counts"]["BUY_WINDOW_CLOSED"] >= 1
    assert report["setup_grade_counts"]["NO TRADE"] == 1


def test_effectiveness_skip_reason_counts_deduped_per_scan(monkeypatch):
    row = {
        "id": 42,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "payload_json": json.dumps({
            "user_id": 9,
            "best_pick": {
                "symbol": "ATRA",
                "decision": "SKIP",
                "setup_grade": "NO TRADE",
                "details": {
                    "skip_reason": "BUY_WINDOW_CLOSED",
                    "skip_reasons": ["BUY_WINDOW_CLOSED", "BUY_WINDOW_CLOSED", "SETUP_GRADE_NO_TRADE"],
                },
            },
        }),
    }
    monkeypatch.setattr(scanner_effectiveness, "get_recent_scans", lambda limit=10: [row])
    _set_redis(monkeypatch, {})
    _stub_user_query(monkeypatch)
    with app_module.app.app_context():
        report = scanner_effectiveness.build_scanner_effectiveness_report(limit=10)
    assert report["skip_reason_counts"]["BUY_WINDOW_CLOSED"] == 1


def test_effectiveness_detects_dominant_symbol(monkeypatch):
    rows = []
    for i in range(10):
        symbol = "ATRA" if i < 9 else "MSFT"
        rows.append({
            "id": 100 + i,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "payload_json": json.dumps({"user_id": 1, "best_pick": {"symbol": symbol, "decision": "SKIP", "details": {"skip_reasons": ["SETUP_GRADE_NO_TRADE"]}}}),
        })
    monkeypatch.setattr(scanner_effectiveness, "get_recent_scans", lambda limit=20: rows)
    _set_redis(monkeypatch, {})
    _stub_user_query(monkeypatch)
    with app_module.app.app_context():
        report = scanner_effectiveness.build_scanner_effectiveness_report(limit=20)
    assert report["dominant_symbol_warning"] is True
    assert report["dominant_symbol"] == "ATRA"
    assert report["same_symbol_count"] == 9
