from contextlib import nullcontext
from types import ModuleType, SimpleNamespace
import logging as real_logging
import sys


class _FakeCelery:
    def __init__(self, *args, **kwargs):
        self.conf = SimpleNamespace(beat_schedule=None, timezone=None)
        self.log = SimpleNamespace(get_default_logger=lambda: SimpleNamespace())

    def task(self, fn):
        fn.run = fn
        return fn


_MODULE_SENTINEL = object()


def _install_import_stubs():
    inserted_modules = {}

    def _set_or_get(name, module):
        existing = sys.modules.get(name, _MODULE_SENTINEL)
        if existing is _MODULE_SENTINEL:
            sys.modules[name] = module
            inserted_modules[name] = module
            return module
        return existing

    redis_module = _set_or_get("redis", ModuleType("redis"))
    redis_module.Redis = SimpleNamespace(from_url=lambda *args, **kwargs: object())

    _set_or_get("requests", ModuleType("requests"))

    sqlalchemy_stub = ModuleType("sqlalchemy")
    sqlalchemy_stub.inspect = lambda *args, **kwargs: SimpleNamespace(get_table_names=lambda: [])
    sqlalchemy_stub.text = lambda query: query
    _set_or_get("sqlalchemy", sqlalchemy_stub)

    celery_stub = ModuleType("celery")
    celery_stub.Celery = _FakeCelery
    _set_or_get("celery", celery_stub)

    schedules_stub = ModuleType("celery.schedules")
    schedules_stub.crontab = lambda *args, **kwargs: None
    _set_or_get("celery.schedules", schedules_stub)

    flask_stub = ModuleType("flask")

    class _FakeFlask:
        def __init__(self, *args, **kwargs):
            self.config = {}

        def app_context(self):
            return nullcontext()

    flask_stub.Flask = _FakeFlask
    _set_or_get("flask", flask_stub)

    models_stub = ModuleType("models")
    models_stub.User = object()
    models_stub.MarketRegime = object()
    models_stub.db = SimpleNamespace(
        init_app=lambda app: None,
        session=SimpleNamespace(get=lambda model, user_id: None),
    )
    _set_or_get("models", models_stub)

    broker_stub = ModuleType("broker")
    broker_stub.place_managed_entry_order = lambda **kwargs: {"id": "stub"}
    _set_or_get("broker", broker_stub)

    guard_stub = ModuleType("execution_guard")
    guard_stub.validate_execution_against_approved_scan = lambda **kwargs: {"ok": True}
    guard_stub.audit_trade_log = lambda **kwargs: None
    _set_or_get("execution_guard", guard_stub)

    ai_stub = ModuleType("ai_catalyst")
    ai_stub.batch_process_premarket = lambda symbols: None
    _set_or_get("ai_catalyst", ai_stub)

    scanner_stub = ModuleType("scanner")
    scanner_stub.get_refined_universe = lambda: []
    _set_or_get("scanner", scanner_stub)

    perf_stub = ModuleType("analyze_performance")
    perf_stub.calculate_user_kelly_fraction = lambda user_id: None
    _set_or_get("analyze_performance", perf_stub)

    config_stub = ModuleType("config")
    config_stub.REDIS_URL = "redis://example"
    config_stub.SQLALCHEMY_DATABASE_URI = "sqlite://"
    config_stub.SQLALCHEMY_ENGINE_OPTIONS = {}
    config_stub.ALPACA_API_KEY = ""
    config_stub.ALPACA_API_SECRET = ""
    config_stub.IS_PRODUCTION = False
    config_stub.IS_TESTING = True
    config_stub.FLASK_ENV = "testing"
    _set_or_get("config", config_stub)

    sentry_stub = ModuleType("sentry_setup")
    sentry_stub.init_sentry = lambda name: None
    _set_or_get("sentry_setup", sentry_stub)
    db_stub = ModuleType("db")
    db_stub.get_trade_by_order_id = lambda order_id: None
    db_stub.insert_trade = lambda payload: 1
    _set_or_get("db", db_stub)
    return inserted_modules


_inserted_stub_modules = _install_import_stubs()
try:
    import tasks
finally:
    def _cleanup_import_stubs():
        for name, module in _inserted_stub_modules.items():
            if sys.modules.get(name) is module:
                sys.modules.pop(name)

    _cleanup_import_stubs()


def test_tasks_stub_import_cleanup_keeps_real_module_imports_available():
    from filters import passes_hard_gatekeeper
    from models import SymbolMarketStats

    assert callable(passes_hard_gatekeeper)
    assert SymbolMarketStats is not None


def test_tasks_stub_import_cleanup_keeps_real_app_imports_available():
    import app

    assert hasattr(app, "app")


def _install_db_ops_stubs(monkeypatch, *, existing_trade=None, get_raises=False, insert_raises=False):
    calls = {"get": 0, "insert": 0, "insert_payload": None}

    def _get_trade(order_id):
        calls["get"] += 1
        if get_raises:
            raise RuntimeError("lookup failed")
        return existing_trade

    def _insert_trade(payload):
        calls["insert"] += 1
        calls["insert_payload"] = payload
        if insert_raises:
            raise RuntimeError("insert failed")
        return 123

    monkeypatch.setattr(tasks.db_ops, "get_trade_by_order_id", _get_trade)
    monkeypatch.setattr(tasks.db_ops, "insert_trade", _insert_trade)
    return calls


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


def test_order_with_id_inserts_trade_before_audit(monkeypatch):
    _patch_context(monkeypatch)
    user = SimpleNamespace(subscription_status="pro", id=7)
    monkeypatch.setattr(tasks.db.session, "get", lambda model, user_id: user)
    monkeypatch.setattr(tasks, "validate_execution_against_approved_scan", lambda **kwargs: {"ok": True})
    monkeypatch.setattr(tasks, "place_managed_entry_order", lambda **kwargs: {"id": "oid-1", "status": "pending_new"})
    calls = _install_db_ops_stubs(monkeypatch)
    audit_calls = {"count": 0}
    monkeypatch.setattr(tasks, "audit_trade_log", lambda **kwargs: audit_calls.__setitem__("count", audit_calls["count"] + 1))

    result = _call_task()

    assert calls["get"] == 1 and calls["insert"] == 1
    assert calls["insert_payload"]["order_id"] == "oid-1"
    assert calls["insert_payload"]["status"] == "pending_new"
    assert calls["insert_payload"]["qty"] == 5
    assert calls["insert_payload"]["entry_price"] == 101.5
    assert calls["insert_payload"]["stop_price"] == 99.0
    assert audit_calls["count"] == 1
    assert "Success" in result and "oid-1" in result


def test_existing_trade_by_order_id_skips_duplicate_insert(monkeypatch):
    _patch_context(monkeypatch)
    user = SimpleNamespace(subscription_status="pro", id=7)
    monkeypatch.setattr(tasks.db.session, "get", lambda model, user_id: user)
    monkeypatch.setattr(tasks, "validate_execution_against_approved_scan", lambda **kwargs: {"ok": True})
    monkeypatch.setattr(tasks, "place_managed_entry_order", lambda **kwargs: {"id": "oid-2", "status": "pending_new"})
    calls = _install_db_ops_stubs(monkeypatch, existing_trade={"id": 55})
    monkeypatch.setattr(tasks, "audit_trade_log", lambda **kwargs: None)

    _call_task()

    assert calls["get"] == 1 and calls["insert"] == 0


def test_rejected_order_without_id_audits_without_trade_insert(monkeypatch):
    _patch_context(monkeypatch)
    user = SimpleNamespace(subscription_status="pro", id=7)
    monkeypatch.setattr(tasks.db.session, "get", lambda model, user_id: user)
    monkeypatch.setattr(tasks, "validate_execution_against_approved_scan", lambda **kwargs: {"ok": True})
    monkeypatch.setattr(tasks, "place_managed_entry_order", lambda **kwargs: {"status": "rejected", "reason": "no buying power"})
    calls = _install_db_ops_stubs(monkeypatch)
    audit_calls = {"count": 0}
    monkeypatch.setattr(tasks, "audit_trade_log", lambda **kwargs: audit_calls.__setitem__("count", audit_calls["count"] + 1))

    result = _call_task()

    assert calls["get"] == 0 and calls["insert"] == 0
    assert audit_calls["count"] == 1
    assert "Success" in result


def test_insert_trade_failure_does_not_block_audit_or_success(monkeypatch):
    _patch_context(monkeypatch)
    user = SimpleNamespace(subscription_status="pro", id=7)
    monkeypatch.setattr(tasks.db.session, "get", lambda model, user_id: user)
    monkeypatch.setattr(tasks, "validate_execution_against_approved_scan", lambda **kwargs: {"ok": True})
    monkeypatch.setattr(tasks, "place_managed_entry_order", lambda **kwargs: {"id": "oid-3", "status": "pending_new"})
    calls = _install_db_ops_stubs(monkeypatch, insert_raises=True)
    audit_calls = {"count": 0}
    monkeypatch.setattr(tasks, "audit_trade_log", lambda **kwargs: audit_calls.__setitem__("count", audit_calls["count"] + 1))

    result = _call_task()

    assert calls["insert"] == 1
    assert audit_calls["count"] == 1
    assert "Success" in result and "oid-3" in result


def test_insert_trade_failure_still_audits_when_all_exception_logging_fails(monkeypatch):
    _patch_context(monkeypatch)
    user = SimpleNamespace(subscription_status="pro", id=7)
    monkeypatch.setattr(tasks.db.session, "get", lambda model, user_id: user)
    monkeypatch.setattr(tasks, "validate_execution_against_approved_scan", lambda **kwargs: {"ok": True})
    monkeypatch.setattr(tasks, "place_managed_entry_order", lambda **kwargs: {"id": "oid-4", "status": "pending_new"})
    calls = _install_db_ops_stubs(monkeypatch, insert_raises=True)
    audit_calls = {"count": 0}
    monkeypatch.setattr(tasks, "audit_trade_log", lambda **kwargs: audit_calls.__setitem__("count", audit_calls["count"] + 1))

    class _BrokenLogger:
        def exception(self, *args, **kwargs):
            raise RuntimeError("logger broken")

    monkeypatch.setattr(tasks.celery_app.log, "get_default_logger", lambda: _BrokenLogger())
    monkeypatch.setattr(tasks, "logging", SimpleNamespace(getLogger=lambda name=None: _BrokenLogger()))

    result = _call_task()

    assert calls["insert"] == 1
    assert audit_calls["count"] == 1
    assert "Success" in result and "oid-4" in result
    assert callable(real_logging.getLogger)
    assert real_logging.getLogger() is not None


def test_get_trade_by_order_id_failure_does_not_block_audit_or_success(monkeypatch):
    _patch_context(monkeypatch)
    user = SimpleNamespace(subscription_status="pro", id=7)
    monkeypatch.setattr(tasks.db.session, "get", lambda model, user_id: user)
    monkeypatch.setattr(tasks, "validate_execution_against_approved_scan", lambda **kwargs: {"ok": True})
    monkeypatch.setattr(tasks, "place_managed_entry_order", lambda **kwargs: {"id": "oid-5", "status": "pending_new"})
    calls = _install_db_ops_stubs(monkeypatch, get_raises=True)
    audit_calls = {"count": 0}
    monkeypatch.setattr(tasks, "audit_trade_log", lambda **kwargs: audit_calls.__setitem__("count", audit_calls["count"] + 1))

    result = _call_task()

    assert calls["get"] == 1
    assert calls["insert"] == 0
    assert audit_calls["count"] == 1
    assert "Success" in result and "oid-5" in result


def test_minimal_order_dict_still_audits_and_returns_success(monkeypatch):
    _patch_context(monkeypatch)
    user = SimpleNamespace(subscription_status="pro", id=7)
    monkeypatch.setattr(tasks.db.session, "get", lambda model, user_id: user)
    monkeypatch.setattr(tasks, "validate_execution_against_approved_scan", lambda **kwargs: {"ok": True})
    monkeypatch.setattr(tasks, "place_managed_entry_order", lambda **kwargs: {"id": "oid-6"})
    calls = _install_db_ops_stubs(monkeypatch)
    audit_calls = {"count": 0}
    monkeypatch.setattr(tasks, "audit_trade_log", lambda **kwargs: audit_calls.__setitem__("count", audit_calls["count"] + 1))

    result = _call_task()

    assert calls["get"] == 1
    assert calls["insert"] == 1
    assert calls["insert_payload"]["order_status"] == "submitted"
    assert audit_calls["count"] == 1
    assert "Success" in result and "oid-6" in result
