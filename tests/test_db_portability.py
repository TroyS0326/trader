import json
import os
from pathlib import Path
import sys

sys.path.append(str(Path(__file__).resolve().parents[1]))

for k in ["SECRET_KEY","TOKEN_ENCRYPTION_KEY","ALPACA_CLIENT_ID","ALPACA_CLIENT_SECRET","ALPACA_REDIRECT_URI","FINNHUB_API_KEY","GEMINI_API_KEY"]:
    os.environ.setdefault(k, "test")
os.environ.setdefault('FLASK_ENV', 'testing')
os.environ.setdefault('FLASK_DEBUG', '1')

from flask import Flask
from models import db, Trade
from db import get_trade_by_target1_id, insert_trade_audit_log, ensure_trade_audit_table, get_recent_trade_audit_logs, get_active_trade_for_user_symbol, get_active_trades, mark_stale_active_trade, ACTIVE_TRADE_STATUSES


def _make_test_app(db_path: Path):
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = f"sqlite:///{db_path}"
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)
    return app


def test_get_trade_by_target1_id_sqlite(tmp_path):
    app = _make_test_app(tmp_path / 'portability1.db')
    with app.app_context():
        db.drop_all()
        db.create_all()
        t = Trade(user_id=1, symbol='AAPL', side='buy', qty=1, entry_price=10, stop_price=9, target_1=11, target_2=12, raw_json=json.dumps({'order_bundle': {'target_1_order_id': 't1-123'}}))
        db.session.add(t)
        db.session.commit()
        got = get_trade_by_target1_id('t1-123')
        assert got is not None
        assert got['id'] == t.id
        db.drop_all()


def test_trade_audit_logs_sqlite_insert(tmp_path):
    app = _make_test_app(tmp_path / 'portability2.db')
    with app.app_context():
        db.drop_all()
        db.create_all()
        ensure_trade_audit_table()
        new_id = insert_trade_audit_log({'symbol': 'AAPL', 'raw_json': {'ok': True}})
        assert isinstance(new_id, int)
        logs = list(get_recent_trade_audit_logs(limit=5))
        assert any(row['id'] == new_id for row in logs)
        db.drop_all()


def test_trade_audit_logs_postgres_path_uses_scalar_one_not_lastrowid(monkeypatch, tmp_path):
    app = _make_test_app(tmp_path / 'portability3.db')
    with app.app_context():
        db.drop_all()
        db.create_all()
        ensure_trade_audit_table()

        class _Dialect:
            name = "postgresql"

        class _Bind:
            dialect = _Dialect()

        class _Result:
            @property
            def lastrowid(self):
                raise AssertionError("lastrowid should not be used for postgresql path")

            def scalar_one(self):
                return 987

        monkeypatch.setattr(db.session, "get_bind", lambda *args, **kwargs: _Bind())
        monkeypatch.setattr(db.session, "execute", lambda *args, **kwargs: _Result())
        monkeypatch.setattr(db.session, "commit", lambda: None)

        new_id = insert_trade_audit_log({'symbol': 'MSFT', 'raw_json': {'ok': True}})
        assert new_id == 987

        db.drop_all()


def test_get_active_trade_for_user_symbol_sqlite(tmp_path):
    app = _make_test_app(tmp_path / 'portability4.db')
    with app.app_context():
        db.drop_all()
        db.create_all()
        rows = [
            Trade(user_id=1, symbol='AAPL', status='pending_new', entry_price=10, stop_price=9, target_1=11, target_2=12),
            Trade(user_id=1, symbol='AAPL', status='filled', entry_price=10, stop_price=9, target_1=11, target_2=12),
            Trade(user_id=1, symbol='AAPL', status='partially_filled', entry_price=10, stop_price=9, target_1=11, target_2=12),
            Trade(user_id=1, symbol='AAPL', status='closed', entry_price=10, stop_price=9, target_1=11, target_2=12),
            Trade(user_id=1, symbol='AAPL', order_status='canceled', entry_price=10, stop_price=9, target_1=11, target_2=12),
            Trade(user_id=1, symbol='AAPL', outcome='rejected', entry_price=10, stop_price=9, target_1=11, target_2=12),
            Trade(user_id=1, symbol='AAPL', status='expired', entry_price=10, stop_price=9, target_1=11, target_2=12),
            Trade(user_id=1, symbol='MSFT', status='pending_new', entry_price=10, stop_price=9, target_1=11, target_2=12),
            Trade(user_id=2, symbol='AAPL', status='pending_new', entry_price=10, stop_price=9, target_1=11, target_2=12),
        ]
        db.session.add_all(rows)
        db.session.commit()
        got = get_active_trade_for_user_symbol(1, 'aapl')
        assert got is not None
        assert got['symbol'] == 'AAPL'
        assert got['user_id'] == 1
        assert got['status'] in {'pending_new', 'filled', 'partially_filled'}
        db.drop_all()


def test_stale_not_active_status():
    assert "stale" not in ACTIVE_TRADE_STATUSES


def test_get_active_trades_and_mark_stale(tmp_path):
    app = _make_test_app(tmp_path / "portability5.db")
    with app.app_context():
        db.drop_all(); db.create_all()
        rows = [
            Trade(user_id=1, symbol="AAPL", order_id="o1", status="pending_new", entry_price=10, stop_price=9, target_1=11, target_2=12),
            Trade(user_id=1, symbol="AAPL", order_id="o2", status="accepted", entry_price=10, stop_price=9, target_1=11, target_2=12),
            Trade(user_id=1, symbol="AAPL", order_id="o3", status="filled", entry_price=10, stop_price=9, target_1=11, target_2=12),
            Trade(user_id=1, symbol="AAPL", order_id="o4", status="rejected", entry_price=10, stop_price=9, target_1=11, target_2=12),
            Trade(user_id=1, symbol="AAPL", order_id="o5", status="stale", entry_price=10, stop_price=9, target_1=11, target_2=12),
        ]
        db.session.add_all(rows); db.session.commit()
        active = get_active_trades(limit=10, user_id=1)
        assert {r["order_id"] for r in active} == {"o1", "o2", "o3"}
        mark_stale_active_trade("o1", "test_reason", {"foo": "bar"})
        t = Trade.query.filter_by(order_id="o1").first()
        assert t.status == "stale" and t.order_status == "stale" and t.outcome == "stale"
        assert "test_reason" in (t.notes or "")
        db.drop_all()
