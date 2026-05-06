import json
import os
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))
for k in ["SECRET_KEY","TOKEN_ENCRYPTION_KEY","ALPACA_CLIENT_ID","ALPACA_CLIENT_SECRET","ALPACA_REDIRECT_URI","FINNHUB_API_KEY","GEMINI_API_KEY"]:
    os.environ.setdefault(k, "test")

import app as app_module
from models import db, Trade
from db import get_trade_by_target1_id, insert_trade_audit_log, ensure_trade_audit_table, get_recent_trade_audit_logs


def test_get_trade_by_target1_id_sqlite():
    with app_module.app.app_context():
        t = Trade(user_id=1, symbol='AAPL', side='buy', qty=1, entry_price=10, stop_price=9, target_1=11, target_2=12, raw_json=json.dumps({'order_bundle': {'target_1_order_id': 't1-123'}}))
        db.session.add(t)
        db.session.commit()
        got = get_trade_by_target1_id('t1-123')
        assert got is not None
        assert got['id'] == t.id


def test_trade_audit_logs_sqlite_insert():
    with app_module.app.app_context():
        ensure_trade_audit_table()
        new_id = insert_trade_audit_log({'symbol': 'AAPL', 'raw_json': {'ok': True}})
        assert isinstance(new_id, int)
        logs = list(get_recent_trade_audit_logs(limit=5))
        assert any(row['id'] == new_id for row in logs)
