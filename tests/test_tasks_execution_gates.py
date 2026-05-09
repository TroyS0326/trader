from contextlib import nullcontext
from types import SimpleNamespace

import pytest

pytest.importorskip("redis")
pytest.importorskip("requests")
pytest.importorskip("celery")
pytest.importorskip("flask")

import tasks


def _call_task(**overrides):
    payload = {
        "user_id": 7,
        "scan_id": "scan_1",
        "symbol": "AAPL",
        "qty": 5,
        "entry_price": 101.5,
        "stop_price": 99.0,
        "target_1_price": 104.0,
        "target_2_price": 106.0,
    }
    payload.update(overrides)
    return tasks.execute_user_trade_task.run(**payload)


def _patch_context(monkeypatch):
    monkeypatch.setattr(tasks, "_db_app", SimpleNamespace(app_context=lambda: nullcontext()))


def test_non_pro_user_rejected_before_guard_order_and_audit(monkeypatch):
    _patch_context(monkeypatch)
    calls = {"guard": 0, "order": 0, "audit": 0}
    monkeypatch.setattr(tasks.db.session, "get", lambda model, user_id: SimpleNamespace(subscription_status="free"))
    monkeypatch.setattr(tasks, "validate_execution_against_approved_scan", lambda **kwargs: calls.__setitem__("guard", calls["guard"] + 1))
    monkeypatch.setattr(tasks, "place_managed_entry_order", lambda **kwargs: calls.__setitem__("order", calls["order"] + 1))
    monkeypatch.setattr(tasks, "audit_trade_log", lambda **kwargs: calls.__setitem__("audit", calls["audit"] + 1))
    result = _call_task()
    assert "inactive or non-PRO" in result
    assert calls == {"guard": 0, "order": 0, "audit": 0}


def test_missing_user_rejected_before_guard_order_and_audit(monkeypatch):
    _patch_context(monkeypatch)
    calls = {"guard": 0, "order": 0, "audit": 0}
    monkeypatch.setattr(tasks.db.session, "get", lambda model, user_id: None)
    monkeypatch.setattr(tasks, "validate_execution_against_approved_scan", lambda **kwargs: calls.__setitem__("guard", calls["guard"] + 1))
    monkeypatch.setattr(tasks, "place_managed_entry_order", lambda **kwargs: calls.__setitem__("order", calls["order"] + 1))
    monkeypatch.setattr(tasks, "audit_trade_log", lambda **kwargs: calls.__setitem__("audit", calls["audit"] + 1))
    result = _call_task()
    assert "inactive or non-PRO" in result
    assert calls == {"guard": 0, "order": 0, "audit": 0}


def test_qty_below_one_short_circuits_before_guard_order_and_audit(monkeypatch):
    _patch_context(monkeypatch)
    calls = {"guard": 0, "order": 0, "audit": 0}
    monkeypatch.setattr(tasks.db.session, "get", lambda model, user_id: SimpleNamespace(subscription_status="pro"))
    monkeypatch.setattr(tasks, "validate_execution_against_approved_scan", lambda **kwargs: calls.__setitem__("guard", calls["guard"] + 1))
    monkeypatch.setattr(tasks, "place_managed_entry_order", lambda **kwargs: calls.__setitem__("order", calls["order"] + 1))
    monkeypatch.setattr(tasks, "audit_trade_log", lambda **kwargs: calls.__setitem__("audit", calls["audit"] + 1))
    result = _call_task(qty=0)
    assert "Risk sizing too small" in result
    assert calls == {"guard": 0, "order": 0, "audit": 0}


def test_guard_failure_blocks_live_trade_before_order_and_audit(monkeypatch):
    _patch_context(monkeypatch)
    calls = {"order": 0, "audit": 0}
    monkeypatch.setattr(tasks.db.session, "get", lambda model, user_id: SimpleNamespace(subscription_status="pro"))
    monkeypatch.setattr(tasks, "validate_execution_against_approved_scan", lambda **kwargs: {"ok": False, "error": "approved_scan_mismatch"})
    monkeypatch.setattr(tasks, "place_managed_entry_order", lambda **kwargs: calls.__setitem__("order", calls["order"] + 1))
    monkeypatch.setattr(tasks, "audit_trade_log", lambda **kwargs: calls.__setitem__("audit", calls["audit"] + 1))
    result = _call_task()
    assert "LIVE trade blocked" in result
    assert "approved_scan_mismatch" in result
    assert calls == {"order": 0, "audit": 0}


def test_guard_success_places_order_and_audits_once_with_expected_args(monkeypatch):
    _patch_context(monkeypatch)
    calls = {"order": 0, "audit": 0}
    user = SimpleNamespace(subscription_status="pro", id=7)
    observed = {}
    monkeypatch.setattr(tasks.db.session, "get", lambda model, user_id: user)
    monkeypatch.setattr(tasks, "validate_execution_against_approved_scan", lambda **kwargs: {"ok": True})

    def fake_order(**kwargs):
        calls["order"] += 1
        observed.update(kwargs)
        return {"id": "order_test_123"}

    monkeypatch.setattr(tasks, "place_managed_entry_order", fake_order)
    monkeypatch.setattr(tasks, "audit_trade_log", lambda **kwargs: calls.__setitem__("audit", calls["audit"] + 1))
    result = _call_task(symbol="MSFT", qty=9, entry_price=201.0, stop_price=198.5, target_1_price=205.0, target_2_price=210.0)
    assert calls == {"order": 1, "audit": 1}
    assert observed["symbol"] == "MSFT"
    assert observed["qty"] == 9
    assert observed["entry_price"] == 201.0
    assert observed["stop_price"] == 198.5
    assert observed["target_1_price"] == 205.0
    assert observed["target_2_price"] == 210.0
    assert observed["user"] is user
    assert "Success" in result
    assert "order_test_123" in result


def test_exception_path_returns_error_message(monkeypatch):
    _patch_context(monkeypatch)
    monkeypatch.setattr(tasks.db.session, "get", lambda model, user_id: SimpleNamespace(subscription_status="pro"))
    monkeypatch.setattr(tasks, "validate_execution_against_approved_scan", lambda **kwargs: {"ok": True})
    monkeypatch.setattr(tasks, "place_managed_entry_order", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("broker down")))
    monkeypatch.setattr(tasks, "audit_trade_log", lambda **kwargs: None)
    result = _call_task()
    assert "Execution failed" in result
    assert "broker down" in result
