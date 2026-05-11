from datetime import datetime, timezone
from types import SimpleNamespace

import order_reconciliation as recon


def _ctx(monkeypatch):
    monkeypatch.setattr(recon, "app", SimpleNamespace(app_context=lambda: __import__("contextlib").nullcontext()))


def _trade(order_id, user_id=7, symbol="NVDA", status="filled", raw_json=None, qty="10"):
    return {"id": int(order_id.split('-')[-1]), "user_id": user_id, "symbol": symbol, "order_id": order_id,
            "status": status, "order_status": status, "outcome": None, "qty": qty,
            "created_at": datetime.now(timezone.utc).isoformat(), "raw_json": raw_json or {}}


def test_grouped_single_emergency_exit_and_capped_qty(monkeypatch):
    _ctx(monkeypatch)
    trades = [_trade("oid-1", qty="10"), _trade("oid-2", qty="20")]
    monkeypatch.setattr(recon.db, "get_active_trades", lambda **kwargs: trades)
    monkeypatch.setattr(recon.db.db.session, "get", lambda model, uid: SimpleNamespace(alpaca_access_token="t", trading_mode="paper"))
    monkeypatch.setattr(recon, "get_order", lambda *args, **kwargs: {"status": "filled", "filled_qty": "10"})
    monkeypatch.setattr(recon, "get_open_position", lambda *args, **kwargs: {"symbol": "NVDA", "qty": "5"})
    placed = []
    monkeypatch.setattr(recon, "place_emergency_exit_order", lambda s, q, u, **k: placed.append((s, q)) or {"id": "e1", "status": "new"})
    updates = []
    monkeypatch.setattr(recon.db, "update_trade_status", lambda *args, **kwargs: None)
    monkeypatch.setattr(recon.db, "update_trades_for_user_symbol", lambda *args, **kwargs: updates.append(kwargs) or 2)
    out = recon.reconcile_active_trade_orders()
    assert len(placed) == 1 and placed[0] == ("NVDA", 5)
    assert out["emergency_exit_submitted_count"] == 1
    assert any(u.get("raw_patch", {}).get("emergency_exit_order", {}).get("id") == "e1" for u in updates)


def test_existing_pending_prevents_duplicate(monkeypatch):
    _ctx(monkeypatch)
    trades = [_trade("oid-1", raw_json={"emergency_exit_order": {"id": "e1", "status": "accepted"}})]
    monkeypatch.setattr(recon.db, "get_active_trades", lambda **kwargs: trades)
    monkeypatch.setattr(recon.db.db.session, "get", lambda *a, **k: SimpleNamespace(alpaca_access_token="t", trading_mode="paper"))
    monkeypatch.setattr(recon, "get_order", lambda oid, **k: {"status": "filled", "filled_qty": "1"} if oid == "oid-1" else {"id": "e1", "status": "accepted"})
    monkeypatch.setattr(recon, "get_open_position", lambda *a, **k: {"qty": "1"})
    monkeypatch.setattr(recon.db, "update_trade_status", lambda *a, **k: None)
    monkeypatch.setattr(recon.db, "update_trades_for_user_symbol", lambda *a, **k: 1)
    called = []
    monkeypatch.setattr(recon, "place_emergency_exit_order", lambda *a, **k: called.append(True))
    out = recon.reconcile_active_trade_orders()
    assert not called
    assert out["emergency_exit_skipped_existing_count"] == 1


def test_existing_filled_closes_group(monkeypatch):
    _ctx(monkeypatch)
    trades = [_trade("oid-1", raw_json={"emergency_exit_order": {"id": "e1"}})]
    monkeypatch.setattr(recon.db, "get_active_trades", lambda **kwargs: trades)
    monkeypatch.setattr(recon.db.db.session, "get", lambda *a, **k: SimpleNamespace(alpaca_access_token="t", trading_mode="paper"))
    monkeypatch.setattr(recon, "get_order", lambda oid, **k: {"status": "filled", "filled_qty": "1"} if oid == "oid-1" else {"id": "e1", "status": "filled", "filled_avg_price": "123.4"})
    monkeypatch.setattr(recon, "get_open_position", lambda *a, **k: {"qty": "1"})
    monkeypatch.setattr(recon.db, "update_trade_status", lambda *a, **k: None)
    calls = []
    monkeypatch.setattr(recon.db, "update_trades_for_user_symbol", lambda *a, **k: calls.append(k) or 1)
    recon.reconcile_active_trade_orders()
    assert any(c["updates"].get("status") == "closed" for c in calls)


def test_rejected_retry_gate_and_403_paths(monkeypatch):
    _ctx(monkeypatch)
    base = _trade("oid-1", raw_json={"emergency_exit_order": {"id": "e1"}})
    monkeypatch.setattr(recon.db, "get_active_trades", lambda **kwargs: [base])
    monkeypatch.setattr(recon.db.db.session, "get", lambda *a, **k: SimpleNamespace(alpaca_access_token="t", trading_mode="paper"))
    monkeypatch.setattr(recon, "get_order", lambda oid, **k: {"status": "filled", "filled_qty": "1"} if oid == "oid-1" else {"id": "e1", "status": "rejected"})
    monkeypatch.setattr(recon, "get_open_position", lambda *a, **k: {"qty": "2"})
    monkeypatch.setattr(recon.db, "update_trade_status", lambda *a, **k: None)
    calls = []
    monkeypatch.setattr(recon.db, "update_trades_for_user_symbol", lambda *a, **k: calls.append(k) or 1)
    monkeypatch.setattr(recon.config, "EMERGENCY_EXIT_RETRY_FAILED_ENABLED", False)
    out = recon.reconcile_active_trade_orders()
    assert out["emergency_exit_retry_blocked_count"] == 1

    monkeypatch.setattr(recon.config, "EMERGENCY_EXIT_RETRY_FAILED_ENABLED", True)
    monkeypatch.setattr(recon, "place_emergency_exit_order", lambda *a, **k: (_ for _ in ()).throw(recon.BrokerError("403 forbidden")))
    seq = iter([None, {"qty": "2"}])
    monkeypatch.setattr(recon, "get_open_position", lambda *a, **k: next(seq))
    recon.reconcile_active_trade_orders()

