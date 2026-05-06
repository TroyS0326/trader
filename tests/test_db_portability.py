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
from db import get_trade_by_target1_id, insert_trade_audit_log, ensure_trade_audit_table, get_recent_trade_audit_logs


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
