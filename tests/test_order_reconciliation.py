from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import order_reconciliation as recon


def _trade(status="pending_new", created_at=None):
    return {
        "id": 1,
        "user_id": 7,
        "symbol": "AAPL",
        "order_id": "oid-1",
        "status": status,
        "order_status": status,
        "outcome": None,
        "created_at": (created_at or datetime.now(timezone.utc)).isoformat(),
    }


def test_terminal_statuses_update(monkeypatch):
    monkeypatch.setattr(recon, "app", SimpleNamespace(app_context=lambda: __import__("contextlib").nullcontext()))
    monkeypatch.setattr(recon.db, "get_active_trades", lambda **kwargs: [_trade()])
    monkeypatch.setattr(recon.db.db.session, "get", lambda model, user_id: SimpleNamespace(alpaca_access_token="t"))
    updates = []
    monkeypatch.setattr(recon.db, "update_trade_status", lambda order_id, payload: updates.append(payload))
    monkeypatch.setattr(recon, "get_order", lambda *args, **kwargs: {"status": "canceled", "filled_avg_price": None, "filled_qty": None})
    out = recon.reconcile_active_trade_orders()
    assert out["updated_count"] == 1
    assert updates[0]["status"] == "canceled"


def test_not_found_old_marks_stale(monkeypatch):
    old = datetime.now(timezone.utc) - timedelta(minutes=120)
    monkeypatch.setattr(recon, "app", SimpleNamespace(app_context=lambda: __import__("contextlib").nullcontext()))
    monkeypatch.setattr(recon.db, "get_active_trades", lambda **kwargs: [_trade(created_at=old)])
    monkeypatch.setattr(recon.db.db.session, "get", lambda model, user_id: SimpleNamespace(alpaca_access_token="t"))
    monkeypatch.setattr(recon, "get_order", lambda *args, **kwargs: (_ for _ in ()).throw(recon.BrokerError("404 not found")))
    calls = []
    monkeypatch.setattr(recon.db, "mark_stale_active_trade", lambda oid, reason, raw_update=None: calls.append((oid, reason)))
    out = recon.reconcile_active_trade_orders()
    assert out["marked_stale_count"] == 1
    assert calls and calls[0][1] == "broker_order_not_found"


def test_transient_error_skips(monkeypatch):
    monkeypatch.setattr(recon, "app", SimpleNamespace(app_context=lambda: __import__("contextlib").nullcontext()))
    monkeypatch.setattr(recon.db, "get_active_trades", lambda **kwargs: [_trade()])
    monkeypatch.setattr(recon.db.db.session, "get", lambda model, user_id: SimpleNamespace(alpaca_access_token="t"))
    monkeypatch.setattr(recon, "get_order", lambda *args, **kwargs: (_ for _ in ()).throw(recon.BrokerError("timeout")))
    out = recon.reconcile_active_trade_orders()
    assert out["error_count"] == 1
    assert out["skipped_count"] == 1
