from contextlib import nullcontext
from types import ModuleType, SimpleNamespace
import sys


class _FakeCelery:
    def __init__(self, *args, **kwargs):
        self.conf = SimpleNamespace(beat_schedule=None, timezone=None)
        self.log = SimpleNamespace(get_default_logger=lambda: SimpleNamespace())

    def task(self, fn):
        fn.run = fn
        return fn


def _install_import_stubs():
    sys.modules.setdefault("redis", ModuleType("redis"))
    sys.modules["redis"].Redis = SimpleNamespace(from_url=lambda *args, **kwargs: object())

    sys.modules.setdefault("requests", ModuleType("requests"))

    celery_stub = ModuleType("celery")
    celery_stub.Celery = _FakeCelery
    sys.modules.setdefault("celery", celery_stub)

    schedules_stub = ModuleType("celery.schedules")
    schedules_stub.crontab = lambda *args, **kwargs: None
    sys.modules.setdefault("celery.schedules", schedules_stub)

    flask_stub = ModuleType("flask")

    class _FakeFlask:
        def __init__(self, *args, **kwargs):
            self.config = {}

        def app_context(self):
            return nullcontext()

    flask_stub.Flask = _FakeFlask
    sys.modules.setdefault("flask", flask_stub)

    models_stub = ModuleType("models")
    models_stub.User = object()
    models_stub.MarketRegime = object()
    models_stub.db = SimpleNamespace(
        init_app=lambda app: None,
        session=SimpleNamespace(get=lambda model, user_id: None),
    )
    sys.modules.setdefault("models", models_stub)

    broker_stub = ModuleType("broker")
    broker_stub.place_managed_entry_order = lambda **kwargs: {"id": "stub"}
    sys.modules.setdefault("broker", broker_stub)

    guard_stub = ModuleType("execution_guard")
    guard_stub.validate_execution_against_approved_scan = lambda **kwargs: {"ok": True}
    guard_stub.audit_trade_log = lambda **kwargs: None
    sys.modules.setdefault("execution_guard", guard_stub)

    ai_stub = ModuleType("ai_catalyst")
    ai_stub.batch_process_premarket = lambda symbols: None
    sys.modules.setdefault("ai_catalyst", ai_stub)

    scanner_stub = ModuleType("scanner")
    scanner_stub.get_refined_universe = lambda: []
    sys.modules.setdefault("scanner", scanner_stub)

    perf_stub = ModuleType("analyze_performance")
    perf_stub.calculate_user_kelly_fraction = lambda user_id: None
    sys.modules.setdefault("analyze_performance", perf_stub)

    config_stub = ModuleType("config")
    config_stub.REDIS_URL = "redis://example"
    config_stub.SQLALCHEMY_DATABASE_URI = "sqlite://"
    config_stub.SQLALCHEMY_ENGINE_OPTIONS = {}
    config_stub.ALPACA_API_KEY = ""
    config_stub.ALPACA_API_SECRET = ""
    sys.modules.setdefault("config", config_stub)

    sentry_stub = ModuleType("sentry_setup")
    sentry_stub.init_sentry = lambda name: None
    sys.modules.setdefault("sentry_setup", sentry_stub)


_install_import_stubs()

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
