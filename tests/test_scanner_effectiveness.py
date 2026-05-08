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
import scanner
import app as app_module


class FakeRedis:
    def __init__(self, mapping):
        self.mapping = mapping

    def get(self, key):
        return self.mapping.get(key)


def _set_redis(monkeypatch, mapping):
    monkeypatch.setattr(app_module, "redis_client", FakeRedis(mapping))


def _stub_user_query(monkeypatch):
    monkeypatch.setattr(
        scanner_effectiveness.db.session,
        "get",
        lambda model, uid: SimpleNamespace(
            id=uid,
            trading_mode='paper',
            subscription_status='pro',
            alpaca_paper_account_id='x',
            paper_bankroll_set=True,
            paper_bankroll=100,
            onboarding_completed=True,
            playbook_reviewed=True,
            transparency_reviewed=True,
            broker_connection_started=True,
        ) if model is scanner_effectiveness.User else None,
    )


def test_source_no_query_get_or_datetime_utcnow_in_target_files():
    query_get_marker = ".query" + ".get("
    for path in ("scanner.py", "scanner_effectiveness.py", "app.py"):
        src = Path(path).read_text()
        assert query_get_marker not in src
    for path in ("scanner.py", "scanner_effectiveness.py", "app.py"):
        src = Path(path).read_text()
        assert "datetime.utcnow(" not in src


def test_utcnow_naive_returns_naive_datetime():
    dt = scanner.utcnow_naive()
    assert dt.tzinfo is None


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


def test_safe_scan_view_is_single_scan_only():
    view = scanner_effectiveness._safe_scan_view({"best_pick": {"symbol": "AAPL", "decision": "WATCH"}})
    assert view["symbol"] == "AAPL"
    assert "latest_attributed_scan_age_seconds" not in view
    assert "scanner_starvation_flags" not in view


def test_safe_scan_view_synthesizes_skip_reason_codes_from_old_scan():
    view = scanner_effectiveness._safe_scan_view({
        "best_pick": {"symbol": "AAPL", "decision": "SKIP", "details": {"skip_reasons": ["Opening range is not complete."], "skip_reason": "Spread is too wide."}}
    })
    assert view["skip_reason_codes"] == ["OPENING_RANGE_NOT_COMPLETE", "SPREAD_TOO_WIDE"]


def test_safe_scan_view_includes_or_diagnostics_when_present():
    view = scanner_effectiveness._safe_scan_view({
        "best_pick": {"symbol": "AAPL", "details": {"opening_range_bar_count": 20, "latest_bar_timestamp_et": "2026-01-01T10:00:00-05:00", "or_high": 10.5, "breakout_confirmed_reason": "BREAKOUT_NOT_CONFIRMED"}}
    })
    assert view["opening_range_bar_count"] == 20
    assert view["latest_bar_timestamp_et"]
    assert view["or_high"] == 10.5
    assert view["breakout_confirmed_reason"] == "BREAKOUT_NOT_CONFIRMED"


def test_report_exposes_latest_scan_diagnostics_and_stale_detection(monkeypatch):
    row = {"id": 60, "created_at": datetime.now(timezone.utc).isoformat(), "payload_json": json.dumps({"user_id": 9, "scan_attribution_version": 1, "scan_diagnostics": {"candidate_count_raw": 7, "top_5_candidates_by_score": ["AAPL"], "executable_candidate_count": 1}, "best_pick": {"symbol": "AAPL", "decision": "SKIP"}})}
    monkeypatch.setattr(scanner_effectiveness, "get_recent_scans", lambda limit=10: [row])
    _set_redis(monkeypatch, {})
    _stub_user_query(monkeypatch)
    with app_module.app.app_context():
        report = scanner_effectiveness.build_scanner_effectiveness_report(limit=10)
    assert report["latest_candidate_count_raw"] == 7
    assert report["latest_top_5_candidates_by_score"] == ["AAPL"]
    assert report["latest_scan_has_new_diagnostics"] is True
    assert "latest_bar_data_requested_symbols_count" in report


def test_report_marks_missing_scan_attribution_version_as_old(monkeypatch):
    row = {"id": 61, "created_at": datetime.now(timezone.utc).isoformat(), "payload_json": json.dumps({"user_id": 9, "best_pick": {"symbol": "AAPL", "decision": "SKIP"}})}
    monkeypatch.setattr(scanner_effectiveness, "get_recent_scans", lambda limit=10: [row])
    _set_redis(monkeypatch, {})
    _stub_user_query(monkeypatch)
    with app_module.app.app_context():
        report = scanner_effectiveness.build_scanner_effectiveness_report(limit=10)
    assert report["latest_scan_has_new_diagnostics"] is False
    assert report["latest_scan_missing_new_diagnostics_reason"] in {"SCAN_ATTRIBUTION_VERSION_MISSING", "OLD_SCAN_PAYLOAD"}
    assert report["recommended_next_action"].startswith("Run a fresh manual scan")


def test_report_empty_scans_has_expected_report_level_fields(monkeypatch):
    monkeypatch.setattr(scanner_effectiveness, "get_recent_scans", lambda limit=10: [])
    _set_redis(monkeypatch, {})
    _stub_user_query(monkeypatch)
    with app_module.app.app_context():
        report = scanner_effectiveness.build_scanner_effectiveness_report(limit=10)
    assert report["total_scans_analyzed"] == 0
    assert report["latest_attributed_scan_age_seconds"] is None
    assert report["latest_unattributed_scan_age_seconds"] is None
    assert isinstance(report["scanner_starvation_flags"], list)
    assert report["primary_blocker_summary"] in {"NONE", "NO_EXECUTABLE_CANDIDATES"}


def test_report_mixed_attribution_ages_and_warning(monkeypatch):
    now = datetime.now(timezone.utc)
    rows = [
        {"id": 1, "created_at": (now - timedelta(seconds=45)).isoformat(), "payload_json": json.dumps({"user_id": 1, "best_pick": {"symbol": "AAPL", "decision": "SKIP"}})},
        {"id": 2, "created_at": (now - timedelta(seconds=30)).isoformat(), "payload_json": json.dumps({"user_id": 0, "best_pick": {"symbol": "MSFT", "decision": "SKIP"}})},
    ]
    monkeypatch.setattr(scanner_effectiveness, "get_recent_scans", lambda limit=10: rows)
    _set_redis(monkeypatch, {})
    _stub_user_query(monkeypatch)
    with app_module.app.app_context():
        report = scanner_effectiveness.build_scanner_effectiveness_report(limit=10)
    assert isinstance(report["latest_attributed_scan_age_seconds"], int)
    assert isinstance(report["latest_unattributed_scan_age_seconds"], int)
    assert report["attribution_warning"] is True


def test_report_marks_bar_data_starvation_as_primary_blocker(monkeypatch):
    row = {"id": 500, "created_at": datetime.now(timezone.utc).isoformat(), "payload_json": json.dumps({
        "user_id": 9,
        "scan_attribution_version": 1,
        "scan_diagnostics": {
            "bar_data_requested_symbols_count": 10,
            "missing_daily_bars_symbols": ["A", "B", "C", "D", "E", "F"],
            "missing_minute_bars_symbols": ["A", "B", "C", "D", "E", "F"],
            "asset_filter_rejection_counts": {},
        },
        "best_pick": {"symbol": "AAPL", "decision": "SKIP"}
    })}
    monkeypatch.setattr(scanner_effectiveness, "get_recent_scans", lambda limit=10: [row])
    _set_redis(monkeypatch, {})
    _stub_user_query(monkeypatch)
    with app_module.app.app_context():
        report = scanner_effectiveness.build_scanner_effectiveness_report(limit=10)
    assert report["primary_blocker_summary"] == "BAR_DATA_STARVATION"


def test_recent_and_dashboard_scopes(monkeypatch):
    now = datetime.now(timezone.utc)
    rows = [
        {"id": 1, "created_at": (now - timedelta(minutes=5)).isoformat(), "payload_json": json.dumps({"user_id": 1, "scan_attribution_version": 1, "best_pick": {"symbol": "RXT", "decision": "WATCH", "setup_grade": "WATCH"}})},
        {"id": 2, "created_at": (now - timedelta(minutes=50)).isoformat(), "payload_json": json.dumps({"user_id": 1, "scan_attribution_version": 1, "best_pick": {"symbol": "AEHL", "decision": "SKIP"}})},
        {"id": 3, "created_at": (now - timedelta(hours=5)).isoformat(), "payload_json": json.dumps({"user_id": 1, "scan_attribution_version": 1, "best_pick": {"symbol": "ATRA", "decision": "SKIP"}})},
    ]
    monkeypatch.setattr(scanner_effectiveness, "get_recent_scans", lambda limit=20: rows)
    _set_redis(monkeypatch, {})
    _stub_user_query(monkeypatch)
    with app_module.app.app_context():
        report = scanner_effectiveness.build_scanner_effectiveness_report(limit=20)
    assert report["operational_scans_recent_15m_count"] == 1
    assert report["operational_scans_recent_60m_count"] == 2
    assert report["all_operational_summary"]["count"] == 3
    assert report["current_dashboard_summary_scope"] == "recent_15m"
    assert report["current_dashboard_summary"]["symbol_counts"] == {"RXT": 1}


def test_dashboard_falls_back_to_latest_scan_and_legacy_excluded(monkeypatch):
    now = datetime.now(timezone.utc)
    rows = [
        {"id": 11, "created_at": (now - timedelta(hours=6)).isoformat(), "payload_json": json.dumps({"user_id": 9, "scan_attribution_version": 1, "scan_diagnostics": {"candidate_count_raw": 3}, "best_pick": {"symbol": "RXT", "decision": "WATCH"}})},
        {"id": 12, "created_at": (now - timedelta(minutes=2)).isoformat(), "payload_json": json.dumps({"user_id": 0, "legacy_flags": {"unattributed_scan_legacy": True}, "best_pick": {"symbol": "AEHL", "decision": "SKIP"}})},
    ]
    monkeypatch.setattr(scanner_effectiveness, "get_recent_scans", lambda limit=20: rows)
    _set_redis(monkeypatch, {})
    _stub_user_query(monkeypatch)
    with app_module.app.app_context():
        report = scanner_effectiveness.build_scanner_effectiveness_report(limit=20)
    assert report["current_dashboard_summary_scope"] == "latest_scan"
    assert report["legacy_summary"]["count"] == 1
    assert report["current_dashboard_summary"]["symbol_counts"] == {"RXT": 1}
    assert "AEHL" not in report["current_dashboard_summary"]["symbol_counts"]


def test_recommended_action_uses_recent_scope(monkeypatch):
    now = datetime.now(timezone.utc)
    rows = [
        {"id": 21, "created_at": (now - timedelta(hours=4)).isoformat(), "payload_json": json.dumps({"user_id": 1, "scan_attribution_version": 1, "best_pick": {"symbol": "OLD", "decision": "BUY NOW", "qty": 1, "entry_price": 1, "stop_price": 0.9, "target_1": 1.1, "target_2": 1.2}})}
    ]
    monkeypatch.setattr(scanner_effectiveness, "get_recent_scans", lambda limit=20: rows)
    _set_redis(monkeypatch, {})
    _stub_user_query(monkeypatch)
    monkeypatch.setattr(scanner_effectiveness, "_watch_snapshot", lambda user=None: {"active_watch_candidate_count": 1})
    with app_module.app.app_context():
        report = scanner_effectiveness.build_scanner_effectiveness_report(limit=20)
    assert report["recommended_next_action"] == "No recent attributed scans; run a fresh user scan."
    assert report["has_recent_operational_scans"] is False
    assert report["recent_operational_scan_window_used"] == "none"


def test_report_exposes_degraded_asset_metadata_fields(monkeypatch):
    row = {"id": 501, "created_at": datetime.now(timezone.utc).isoformat(), "payload_json": json.dumps({
        "user_id": 9,
        "scan_attribution_version": 1,
        "scan_diagnostics": {
            "asset_filter_rejection_counts": {},
            "asset_metadata_degraded_allowed_count": 2,
            "asset_metadata_degraded_allowed_symbols": ["AAPL", "MSFT"],
            "asset_metadata_degraded_rejection_counts": {"WARRANT_OR_RIGHT": 1},
            "asset_metadata_degraded_rejection_samples": [{"symbol": "ABCWS", "reason": "WARRANT_OR_RIGHT"}],
        },
        "best_pick": {"symbol": "AAPL", "decision": "SKIP"}
    })}
    monkeypatch.setattr(scanner_effectiveness, "get_recent_scans", lambda limit=10: [row])
    _set_redis(monkeypatch, {})
    _stub_user_query(monkeypatch)
    with app_module.app.app_context():
        report = scanner_effectiveness.build_scanner_effectiveness_report(limit=10)
    assert report["latest_asset_metadata_degraded_allowed_count"] == 2
    assert report["latest_asset_metadata_degraded_allowed_symbols"] == ["AAPL", "MSFT"]
    assert report["latest_asset_metadata_degraded_rejection_counts"] == {"WARRANT_OR_RIGHT": 1}


def test_normalize_skip_reason_code_mappings():
    assert scanner.normalize_skip_reason_code("Opening range is not complete.") == "OPENING_RANGE_NOT_COMPLETE"
    assert scanner.normalize_skip_reason_code("Opening-range breakout is not confirmed yet.") == "OPENING_RANGE_BREAKOUT_NOT_CONFIRMED"
    assert scanner.normalize_skip_reason_code("Spread is too wide.") == "SPREAD_TOO_WIDE"
    assert scanner.normalize_skip_reason_code("Premarket dollar volume is too light for this setup right now.") == "PREMARKET_DOLLAR_VOLUME_TOO_LIGHT"
    assert scanner.normalize_skip_reason_code("Premarket dollar volume unavailable from current data feed.") == "PREMARKET_DOLLAR_VOLUME_UNAVAILABLE"


def test_scanner_effectiveness_exposes_premarket_volume_summary(monkeypatch):
    row = {"id": 61, "created_at": datetime.now(timezone.utc).isoformat(), "payload_json": json.dumps({
        "user_id": 9, "scan_attribution_version": 1,
        "scan_diagnostics": {"latest_premarket_volume_summary": {"symbols_checked": 3, "available_count": 1, "unavailable_count": 2, "passed_count": 1, "failed_count": 0, "feed_used": "iex"}},
        "best_pick": {"symbol": "AAPL", "decision": "SKIP"}
    })}
    monkeypatch.setattr(scanner_effectiveness, "get_recent_scans", lambda limit=10: [row])
    _set_redis(monkeypatch, {})
    _stub_user_query(monkeypatch)
    with app_module.app.app_context():
        report = scanner_effectiveness.build_scanner_effectiveness_report(limit=10)
    assert report["latest_premarket_volume_summary"]["symbols_checked"] == 3


def test_analyze_symbol_assigns_skip_reason_codes_before_result_build():
    import inspect
    src = inspect.getsource(scanner.analyze_symbol)
    assign_idx = src.find("skip_reason_codes = []")
    details_idx = src.find("'skip_reason_codes': skip_reason_codes")
    assert assign_idx != -1
    assert details_idx != -1
    assert assign_idx < details_idx


def test_analyze_symbol_persists_or_diagnostics_in_details_source():
    import inspect
    src = inspect.getsource(scanner.analyze_symbol)
    assert "'opening_range_bar_count': or_stats.get('opening_range_bar_count')" in src
    assert "'latest_bar_timestamp_et': or_stats.get('latest_bar_timestamp_et')" in src
    assert "'breakout_confirmed_reason': or_stats.get('breakout_confirmed_reason')" in src

def test_scanner_effectiveness_exposes_new_quality_summaries(monkeypatch):
    row = {"id": 99, "created_at": datetime.now(timezone.utc).isoformat(), "payload_json": json.dumps({
        "user_id": 9, "scan_attribution_version": 1,
        "scan_diagnostics": {
            "latest_catalyst_score_summary": {"symbols_checked": 2, "average_catalyst_score": 2.0},
            "latest_vwap_alignment_summary": {"aligned_count": 0, "not_aligned_count": 2},
            "latest_liquidity_failure_summary": {"low_liquidity_count": 2, "wide_spread_count": 1},
            "latest_candidate_quality_summary": {"analyzed_count": 2, "skip_count": 2},
        },
        "best_pick": {"symbol": "AAPL", "decision": "SKIP"}
    })}
    monkeypatch.setattr(scanner_effectiveness, "get_recent_scans", lambda limit=10: [row])
    _set_redis(monkeypatch, {})
    _stub_user_query(monkeypatch)
    with app_module.app.app_context():
        report = scanner_effectiveness.build_scanner_effectiveness_report(limit=10)
    assert report["latest_catalyst_score_summary"]["symbols_checked"] == 2
    assert report["latest_vwap_alignment_summary"]["not_aligned_count"] == 2
    assert report["latest_liquidity_failure_summary"]["low_liquidity_count"] == 2
    assert "latest_candidate_source_quality_summary" in report
    assert "latest_catalyst_baseline_reason_counts" in report


def test_scanner_top5_and_catalyst_diag_fields_present_in_source():
    import inspect
    src = inspect.getsource(scanner.run_scan)
    assert "'catalyst_source'" in src
    assert "'catalyst_strength_reason'" in src
    assert "'vwap_trend_aligned'" in src
    assert "'vwap_trend_reason'" in src
    assert "'liquidity_score_reason'" in src
    assert "'liquidity_failure_codes'" in src
    assert "'sources'" in src
    assert "'catalyst_score_baseline_reason'" in src
    assert "'catalyst_missing_reason'" in src


def test_catalyst_score_baseline_reason_and_no_news_diagnostics_in_source():
    import inspect
    src = inspect.getsource(scanner.analyze_symbol)
    assert "catalyst_score_baseline_reason" in src
    assert "catalyst_missing_reason" in src
    assert "UNKNOWN_BASELINE_REASON" in src

def test_scanner_effectiveness_exposes_news_evidence_scoring_summary(monkeypatch):
    row = {"id": 101, "created_at": datetime.now(timezone.utc).isoformat(), "payload_json": json.dumps({
        "user_id": 9, "scan_attribution_version": 1,
        "scan_diagnostics": {
            "latest_news_evidence_scoring_summary": {"qualified_news_symbols": ["RXT"], "news_symbols_adjusted_count": 1},
            "latest_news_catalyst_score_blockers": [{"symbol": "RXT", "catalyst_score": 2}],
            "top_5_candidates_by_score": [{"symbol": "RXT", "catalyst_positive_terms": ["ai"], "catalyst_score_components": {"keyword_boost": 0.12}}],
        },
        "best_pick": {"symbol": "AAPL", "decision": "SKIP"}
    })}
    monkeypatch.setattr(scanner_effectiveness, "get_recent_scans", lambda limit=10: [row])
    _set_redis(monkeypatch, {})
    _stub_user_query(monkeypatch)
    with app_module.app.app_context():
        report = scanner_effectiveness.build_scanner_effectiveness_report(limit=10)
    assert report["latest_news_evidence_scoring_summary"]["qualified_news_symbols"] == ["RXT"]
    assert report["latest_news_catalyst_score_blockers"][0]["symbol"] == "RXT"

def test_scanner_effectiveness_exposes_watch_diagnostics(monkeypatch):
    row = {"id": 1, "created_at": datetime.now(timezone.utc).isoformat(), "payload_json": json.dumps({"scan_id": "s-1", "user_id": 1, "best_pick": {"symbol": "AAPL", "decision": "WATCH"}})}
    monkeypatch.setattr(scanner_effectiveness, "get_recent_scans", lambda limit=10: [row])
    monkeypatch.setattr(scanner_effectiveness, "_watch_snapshot", lambda user=None: {"active_watch_candidate_count": 2, "latest_watch_candidates": [{"symbol": "RXT"}], "downgraded_watch_candidate_count": 3, "rejected_watch_candidate_count": 1, "latest_downgraded_watch_candidates": [{"symbol": "NVDA"}], "latest_rejected_watch_candidates": [{"symbol": "BITO"}], "latest_watch_recheck_summary": None, "watch_promoted_count_today": 1, "watch_expired_count_today": 1, "watch_top_blockers": [["VWAP_TREND_NOT_ALIGNED", 2]], "best_active_watch_symbol": "RXT", "best_active_watch_missing_confirmations": ["VWAP_TREND_NOT_ALIGNED"]})
    with scanner_effectiveness.app.app_context() if hasattr(scanner_effectiveness, 'app') else __import__('contextlib').nullcontext():
        report = scanner_effectiveness.build_scanner_effectiveness_report(limit=10)
    assert report["active_watch_candidate_count"] == 2
    assert report["best_active_watch_symbol"] == "RXT"
    assert report["downgraded_watch_candidate_count"] == 3
    assert report["latest_downgraded_watch_candidates"][0]["symbol"] == "NVDA"


def test_report_has_watch_top_level_fields_when_scan_diagnostics_empty(monkeypatch):
    row = {"id": 999, "created_at": datetime.now(timezone.utc).isoformat(), "payload_json": json.dumps({"user_id": 1, "scan_attribution_version": 1, "scan_diagnostics": {}, "best_pick": {"symbol": "AAPL", "decision": "SKIP"}})}
    monkeypatch.setattr(scanner_effectiveness, "get_recent_scans", lambda limit=10: [row])
    _set_redis(monkeypatch, {"latest_watch_recheck_summary": json.dumps({"checked_count": 7, "execution_attempted": False})})
    _stub_user_query(monkeypatch)
    monkeypatch.setattr(scanner_effectiveness, "_watch_snapshot", lambda user=None: {"active_watch_candidate_count": 1, "latest_watch_candidates": [{"symbol": "RXT", "status": "ACTIVE", "missing_buy_confirmations": ["VWAP_TREND_NOT_ALIGNED"]}], "latest_watch_recheck_summary": {"checked_count": 7, "execution_attempted": False}, "watch_promoted_count_today": 0, "watch_expired_count_today": 0, "watch_top_blockers": [["VWAP_TREND_NOT_ALIGNED", 1]], "best_active_watch_symbol": "RXT", "best_active_watch_missing_confirmations": ["VWAP_TREND_NOT_ALIGNED"]})
    with app_module.app.app_context():
        report = scanner_effectiveness.build_scanner_effectiveness_report(limit=10)
    assert report["active_watch_candidate_count"] == 1
    assert report["latest_watch_candidates"][0]["symbol"] == "RXT"
    assert report["latest_watch_recheck_summary"]["checked_count"] == 7

def test_safe_scan_view_source_has_no_undefined_enriched_vars():
    src = Path(scanner_effectiveness.__file__).read_text()
    fn = src[src.index("def _safe_scan_view"):src.index("def normalize_scan_record")]
    assert "latest_enriched_scan" not in fn
    assert "watch_snapshot" not in fn
    assert "now_utc" not in fn
    assert "latest_diag" not in fn
    assert "user is not None" not in fn
    assert "alpaca_reconnect" not in fn


def test_build_report_source_has_stale_scan_calculation():
    src = Path(scanner_effectiveness.__file__).read_text()
    fn = src[src.index("def build_scanner_effectiveness_report"):src.index("def main(")]
    assert "latest_enriched_scan_age_seconds" in fn
    assert "stale_scan_warning" in fn

def test_report_exposes_asset_metadata_reconnect_fields(monkeypatch):
    row = {"id": 80, "created_at": datetime.now(timezone.utc).isoformat(), "payload_json": json.dumps({"user_id": 9, "scan_diagnostics": {"alpaca_user_oauth_asset_metadata_health": "unauthorized", "alpaca_asset_metadata_reconnect_required": True, "alpaca_asset_metadata_reconnect_reason": "USER_OAUTH_UNAUTHORIZED_FOR_ASSET_METADATA", "alpaca_asset_metadata_server_fallback_success_count": 2, "asset_metadata_degraded_mode": False}, "best_pick": {"symbol": "AAPL", "decision": "WATCH"}})}
    monkeypatch.setattr(scanner_effectiveness, "get_recent_scans", lambda limit=10: [row])
    _set_redis(monkeypatch, {})
    _stub_user_query(monkeypatch)
    with app_module.app.app_context():
        report = scanner_effectiveness.build_scanner_effectiveness_report(limit=10)
    assert report["latest_alpaca_user_oauth_asset_metadata_health"] == "unauthorized"
    assert report["latest_alpaca_asset_metadata_reconnect_required"] is True
    assert report["alpaca_user_action_reconnect_required"] is False
    assert report["alpaca_user_action_reconnect_url"] is None
    assert "server fallback" in (report["alpaca_metadata_fallback_notice"] or "").lower()


def test_scanner_effectiveness_prefers_attributed_over_unattributed(monkeypatch):
    rows = [
        {"id": 200, "created_at": datetime.now(timezone.utc).isoformat(), "payload_json": json.dumps({"user_id": None, "scan_attribution_version": 0, "scan_diagnostics": {"candidate_count_after_dedupe": 1}, "best_pick": {"symbol": "OLD", "decision": "SKIP"}})},
        {"id": 199, "created_at": datetime.now(timezone.utc).isoformat(), "payload_json": json.dumps({"user_id": 1, "scan_attribution_version": 1, "scan_diagnostics": {"candidate_count_after_dedupe": 9}, "best_pick": {"symbol": "RXT", "decision": "WATCH"}})},
    ]
    monkeypatch.setattr(scanner_effectiveness, "get_recent_scans", lambda limit=20: rows)
    _set_redis(monkeypatch, {})
    _stub_user_query(monkeypatch)
    with app_module.app.app_context():
        report = scanner_effectiveness.build_scanner_effectiveness_report(user=SimpleNamespace(id=1), limit=20)
    assert report["current_report_scan_scope"] == "attributed_user"
    assert report["current_report_uses_unattributed_scan"] is False
    assert report["latest_candidate_count_after_dedupe"] == 9


def test_scanner_effectiveness_exposes_legacy_unattributed_counts(monkeypatch):
    row = {"id": 201, "created_at": datetime.now(timezone.utc).isoformat(), "payload_json": json.dumps({"user_id": None, "scan_attribution_version": 0, "legacy_flags": {"unattributed_scan_legacy": True}, "scan_diagnostics": {}, "best_pick": {"symbol": "OLD", "decision": "SKIP"}})}
    monkeypatch.setattr(scanner_effectiveness, "get_recent_scans", lambda limit=10: [row])
    _set_redis(monkeypatch, {})
    _stub_user_query(monkeypatch)
    with app_module.app.app_context():
        report = scanner_effectiveness.build_scanner_effectiveness_report(limit=10)
    assert report["legacy_unattributed_scan_count"] == 1
    assert report["ignored_unattributed_scan_count"] >= 1


def test_scanner_and_effectiveness_import_together_without_cycle():
    import importlib
    import scanner as scanner_module
    import scanner_effectiveness as scanner_eff_module

    importlib.reload(scanner_module)
    importlib.reload(scanner_eff_module)
    assert hasattr(scanner_module, 'normalize_skip_reason_code')

def test_operational_excludes_legacy_unattributed_from_aggregates(monkeypatch):
    rows = [
        {"id": 1, "created_at": datetime.now(timezone.utc).isoformat(), "payload_json": json.dumps({"user_id": 1, "scan_attribution_version": 1, "scan_diagnostics": {"candidate_count_raw": 1}, "best_pick": {"symbol": "RXT", "decision": "WATCH"}})},
        {"id": 2, "created_at": datetime.now(timezone.utc).isoformat(), "payload_json": json.dumps({"user_id": 0, "scan_attribution_version": 0, "legacy_flags": {"unattributed_scan_legacy": True}, "best_pick": {"symbol": "AEHL", "decision": "SKIP"}})},
    ]
    monkeypatch.setattr(scanner_effectiveness, "get_recent_scans", lambda limit=10: rows)
    _set_redis(monkeypatch, {})
    _stub_user_query(monkeypatch)
    with app_module.app.app_context():
        report = scanner_effectiveness.build_scanner_effectiveness_report(limit=10)
    assert report["total_scans_loaded_count"] == 2
    assert report["total_scans_analyzed"] == 1
    assert report["decision_counts"] == {"WATCH": 1}
    assert report["symbol_counts"] == {"RXT": 1}
    assert report["legacy_unattributed_scan_count"] == 1
    assert report["ignored_unattributed_scan_count"] >= 1
    assert report["current_report_scan_scope"] == "attributed_any_user"


def test_legacy_fallback_scope_when_no_attributed(monkeypatch):
    rows = [
        {"id": 2, "created_at": datetime.now(timezone.utc).isoformat(), "payload_json": json.dumps({"user_id": 0, "scan_attribution_version": 0, "best_pick": {"symbol": "AEHL", "decision": "SKIP"}})},
    ]
    monkeypatch.setattr(scanner_effectiveness, "get_recent_scans", lambda limit=10: rows)
    _set_redis(monkeypatch, {})
    _stub_user_query(monkeypatch)
    with app_module.app.app_context():
        report = scanner_effectiveness.build_scanner_effectiveness_report(limit=10)
    assert report["current_report_scan_scope"] == "legacy_unattributed_fallback"
    assert report["current_report_uses_unattributed_scan"] is True
    assert report["total_scans_analyzed"] == 1


def test_recommended_action_active_watch_no_executable(monkeypatch):
    row = {"id": 60, "created_at": datetime.now(timezone.utc).isoformat(), "payload_json": json.dumps({"user_id": 9, "scan_attribution_version": 1, "scan_diagnostics": {"candidate_count_raw": 1}, "best_pick": {"symbol": "RXT", "decision": "WATCH"}})}
    monkeypatch.setattr(scanner_effectiveness, "get_recent_scans", lambda limit=10: [row])
    _set_redis(monkeypatch, {})
    _stub_user_query(monkeypatch)
    monkeypatch.setattr(scanner_effectiveness, "_watch_snapshot", lambda user=None: {"active_watch_candidate_count": 1, "latest_watch_candidates": []})
    with app_module.app.app_context():
        report = scanner_effectiveness.build_scanner_effectiveness_report(limit=10)
    assert report["recommended_next_action"] == "Active WATCH candidate exists; continue rechecking until missing confirmations clear."
    assert report["has_recent_operational_scans"] is True
    assert report["recent_operational_scan_window_used"] == "recent_15m"


def test_api_scanner_effectiveness_includes_dashboard_watch_and_health_fields(monkeypatch):
    row = {"id": 70, "created_at": datetime.now(timezone.utc).isoformat(), "payload_json": json.dumps({"user_id": 77, "scan_attribution_version": 1, "scan_diagnostics": {"alpaca_user_oauth_asset_metadata_health": "unauthorized", "alpaca_asset_metadata_reconnect_required": True, "alpaca_asset_metadata_reconnect_reason": "USER_OAUTH_UNAUTHORIZED_FOR_ASSET_METADATA", "alpaca_asset_metadata_server_fallback_success_count": 22, "asset_metadata_degraded_mode": False}, "best_pick": {"symbol": "RXT", "decision": "WATCH", "setup_grade": "WATCH"}})}
    monkeypatch.setattr(scanner_effectiveness, "get_recent_scans", lambda limit=50: [row])
    _set_redis(monkeypatch, {})
    _stub_user_query(monkeypatch)
    monkeypatch.setattr(scanner_effectiveness, "_watch_snapshot", lambda user=None: {"active_watch_candidate_count": 1, "latest_watch_candidates": [{"symbol": "RXT", "status": "ACTIVE"}], "best_active_watch_symbol": "RXT", "best_active_watch_missing_confirmations": ["VWAP_TREND_NOT_ALIGNED"]})
    user = SimpleNamespace(id=77, is_authenticated=True, trading_mode='paper')
    monkeypatch.setattr(app_module, "current_user", user)
    monkeypatch.setattr(app_module, "is_admin_user", lambda: False)
    with app_module.app.test_request_context('/api/scanner/effectiveness?limit=50', method='GET'):
        payload = app_module.api_scanner_effectiveness_v2.__wrapped__().get_json()["data"]
    assert payload["current_dashboard_summary"]["latest_symbol"] == "RXT"
    assert payload["current_dashboard_summary"]["latest_decision"] == "WATCH"
    assert payload["active_watch_candidate_count"] == 1
    assert payload["best_active_watch_symbol"] == "RXT"
    assert payload["latest_alpaca_user_oauth_asset_metadata_health"] == "unauthorized"
    assert payload["latest_alpaca_asset_metadata_reconnect_required"] is True
    assert payload["alpaca_user_action_reconnect_required"] is False
    assert payload["alpaca_metadata_fallback_notice"] is not None
    assert "token" not in str(payload).lower()


def test_dashboard_template_renders_scanner_cards_and_watch_non_executable_copy():
    src = Path("templates/dashboard.html").read_text()
    assert "id=\"scanner-current-decision\"" in src
    assert "id=\"scanner-active-watch\"" in src
    assert "id=\"scanner-oauth-health\"" in src
    assert "id=\"scanner-reconnect-cta\"" in src
    assert "Reconnect Alpaca Paper" in src
    assert "WATCH is non-executable" in src
    assert "PREMARKET_DOLLAR_VOLUME_TOO_LIGHT" in src


def test_dashboard_template_scanner_operational_layout_hooks_present():
    src = Path("templates/dashboard.html").read_text()
    assert ".bottom-row > .full-width" in src
    assert "scanner-ops-grid" in src
    assert "scanner-metric-grid" in src
    assert "scanner-confirmation-list" in src
    assert "scanner-alpaca-health" in src


def test_dashboard_js_humanizes_reconnect_and_avoids_raw_decision_json_rendering():
    src = Path("templates/dashboard.html").read_text()
    assert "function humanizeReconnectReason(code)" in src
    assert "USER_OAUTH_UNAUTHORIZED_FOR_ASSET_METADATA" in src
    assert "SERVER_KEYS_SUCCEEDED" in src
    assert "HTTP_401" in src and "HTTP_403" in src
    assert "reconnectCtaEl.style.display = reconnectRequired ? 'block' : 'none';" in src
    assert "report.alpaca_user_action_reconnect_url" in src
    assert "report.alpaca_user_action_reconnect_label" in src
    assert "function renderCountBadges(counts)" in src
    assert "JSON.stringify(summary.decision_counts" not in src


def test_dashboard_scanner_render_avoids_unescaped_innerhtml_for_backend_strings():
    src = Path("templates/dashboard.html").read_text()
    fn = src[src.index("function renderScannerEffectiveness") : src.index("async function refreshScannerEffectiveness")]
    assert ".innerHTML" not in fn


def test_scanner_effectiveness_reconnect_fields_null_when_not_required(monkeypatch):
    row = {"id": 71, "created_at": datetime.now(timezone.utc).isoformat(), "payload_json": json.dumps({"user_id": 77, "scan_attribution_version": 1, "scan_diagnostics": {"alpaca_asset_metadata_reconnect_required": False}, "best_pick": {"symbol": "RXT", "decision": "WATCH", "setup_grade": "WATCH"}})}
    monkeypatch.setattr(scanner_effectiveness, "get_recent_scans", lambda limit=10: [row])
    _set_redis(monkeypatch, {})
    _stub_user_query(monkeypatch)
    report = scanner_effectiveness.build_scanner_effectiveness_report(user=SimpleNamespace(id=77, trading_mode='paper', alpaca_paper_access_token='p', alpaca_live_access_token='l'), limit=10)
    assert report["alpaca_user_action_reconnect_env"] is None
    assert report["alpaca_user_action_reconnect_url"] is None
    assert report["alpaca_user_action_reconnect_label"] is None


def test_scanner_effectiveness_reconnect_url_uses_live_mode(monkeypatch):
    row = {"id": 72, "created_at": datetime.now(timezone.utc).isoformat(), "payload_json": json.dumps({"user_id": 88, "scan_attribution_version": 1, "scan_diagnostics": {"alpaca_asset_metadata_reconnect_required": True}, "best_pick": {"symbol": "RXT", "decision": "WATCH", "setup_grade": "WATCH"}})}
    monkeypatch.setattr(scanner_effectiveness, "get_recent_scans", lambda limit=10: [row])
    _set_redis(monkeypatch, {})
    _stub_user_query(monkeypatch)
    report = scanner_effectiveness.build_scanner_effectiveness_report(user=SimpleNamespace(id=88, trading_mode='live', alpaca_live_access_token=None), limit=10)
    assert report["alpaca_user_action_reconnect_required"] is True
    assert report["alpaca_user_action_reconnect_env"] == "live"
    assert report["alpaca_user_action_reconnect_url"] == "/alpaca/login?env=live"
    assert report["alpaca_user_action_reconnect_label"] == "Reconnect Alpaca Live"


def test_dashboard_template_has_reconnect_disclosure_link_and_no_trade_cta_in_reconnect_block():
    src = Path("templates/dashboard.html").read_text()
    block = src[src.index('id="scanner-alpaca-reconnect-card"'):src.index('id="scanner-no-reconnect-needed"')]
    assert '/broker-integration' in block
    assert 'review Alpaca authorization terms' in block
    assert 'Buy' not in block and 'Execute' not in block and 'Trade now' not in block



def test_build_scan_aggregate_summary_runtime_smoke():
    scans = [
        {"scan_id": "1", "created_at": datetime.now(timezone.utc).isoformat(), "best_pick": {"symbol": "AAPL", "decision": "WATCH"}, "scan_diagnostics": {"candidate_count_raw": 3}},
        {"scan_id": "2", "created_at": datetime.now(timezone.utc).isoformat(), "best_pick": {"symbol": "MSFT", "decision": "SKIP"}, "scan_diagnostics": {"candidate_count_raw": 2}},
    ]
    summary = scanner_effectiveness.build_scan_aggregate_summary(scans)
    assert isinstance(summary, dict)
    assert summary["scan_count"] == 2


def test_scanner_effectiveness_requires_paper_reconnect_when_paper_token_missing(monkeypatch):
    row = {"id": 73, "created_at": datetime.now(timezone.utc).isoformat(), "payload_json": json.dumps({"user_id": 77, "scan_attribution_version": 1, "scan_diagnostics": {}, "best_pick": {"symbol": "RXT", "decision": "WATCH"}})}
    monkeypatch.setattr(scanner_effectiveness, "get_recent_scans", lambda limit=10: [row])
    _set_redis(monkeypatch, {})
    _stub_user_query(monkeypatch)
    report = scanner_effectiveness.build_scanner_effectiveness_report(user=SimpleNamespace(id=77, trading_mode='paper', alpaca_paper_access_token=None, alpaca_access_token=None), limit=10)
    assert report["alpaca_user_action_reconnect_required"] is True
    assert report["alpaca_user_action_reconnect_env"] == "paper"
    assert report["alpaca_user_action_reconnect_url"] == "/alpaca/login?env=paper"
