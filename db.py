import json
import math
from datetime import datetime, time, timezone
from typing import Any, Dict, Iterable, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import func, text

import config
from models import db, Trade, Scan, MarketRegime


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def init_db() -> None:
    # No longer needed!
    # SQLAlchemy's db.create_all() inside app.py handles table creation.
    pass


def _model_to_dict(obj) -> Dict[str, Any]:
    """Helper to maintain backward compatibility with existing dictionary-based code."""
    data = {}
    for column in obj.__table__.columns:
        val = getattr(obj, column.name)
        # Convert datetimes to ISO format strings so JSON serialization doesn't break
        if isinstance(val, datetime):
            data[column.name] = val.isoformat()
        else:
            data[column.name] = val
    return data


CLOSED_TRADE_OUTCOMES = {
    'win',
    'loss',
    'stopped_out',
    'target_hit',
    'target1_hit',
    'target2_hit',
    'closed',
    'breakeven_or_small_win',
}

NON_REALIZED_TRADE_STATES = {
    'open',
    'pending',
    'working',
    'working_or_filled',
    'new',
    'accepted',
    'partially_filled',
    'rejected',
    'failed',
    'canceled',
    'cancelled',
    'expired',
    'done_for_day',
}

PNL_KEYS = {
    'pnl',
    'realized_pnl',
    'realized_pl',
    'realized_profit',
    'profit_loss',
    'net_pnl',
    'net_profit',
    'pl',
}

EXIT_PRICE_KEYS = {
    'exit_price',
    'close_price',
    'closed_price',
    'average_exit_price',
    'filled_exit_price',
    'sell_price',
}


def _safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None

    try:
        result = float(value)
    except (TypeError, ValueError):
        return None

    if not math.isfinite(result):
        return None

    return result


def _load_json_payload(value: Any) -> Any:
    if not value:
        return {}

    if isinstance(value, (dict, list)):
        return value

    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {}

    return {}


def _find_numeric_key(obj: Any, keys: set[str]) -> Optional[float]:
    """
    Recursively searches nested raw_json for a numeric value matching one of the requested keys.
    """
    if isinstance(obj, dict):
        for key, value in obj.items():
            if str(key).lower() in keys:
                numeric = _safe_float(value)
                if numeric is not None:
                    return numeric

        for value in obj.values():
            found = _find_numeric_key(value, keys)
            if found is not None:
                return found

    elif isinstance(obj, list):
        for item in obj:
            found = _find_numeric_key(item, keys)
            if found is not None:
                return found

    return None


def _trade_state(trade: Trade) -> str:
    return (
        getattr(trade, 'outcome', None)
        or getattr(trade, 'status', None)
        or getattr(trade, 'order_status', None)
        or ''
    ).strip().lower()


def calculate_realized_trade_pnl(trade: Trade) -> Optional[Dict[str, Any]]:
    """
    Calculates realized P&L for a completed trade.

    Priority:
    1. Use direct raw_json realized P&L if present.
    2. Use raw_json exit price if present.
    3. Fall back to stop/target based on completed outcome.

    Returns None when the trade is not complete or does not have enough data.
    """
    raw_payload = _load_json_payload(getattr(trade, 'raw_json', None))

    raw_pnl = _find_numeric_key(raw_payload, PNL_KEYS)
    raw_exit_price = _find_numeric_key(raw_payload, EXIT_PRICE_KEYS)

    if raw_pnl is not None:
        return {
            'pnl': round(raw_pnl, 2),
            'exit_price': raw_exit_price,
            'pnl_source': 'raw_json_realized_pnl',
            'closed_at': utc_now(),
        }

    state = _trade_state(trade)

    if not state or state in NON_REALIZED_TRADE_STATES:
        return None

    if state not in CLOSED_TRADE_OUTCOMES:
        return None

    qty = _safe_float(getattr(trade, 'filled_qty', None)) or _safe_float(getattr(trade, 'qty', None))
    entry_price = _safe_float(getattr(trade, 'filled_avg_price', None)) or _safe_float(getattr(trade, 'entry_price', None))

    if qty is None or qty <= 0:
        return None

    if entry_price is None or entry_price <= 0:
        return None

    side = (getattr(trade, 'side', None) or 'buy').strip().lower()
    direction = -1 if side in {'sell', 'short'} else 1

    exit_price = raw_exit_price
    pnl_source = 'estimated_from_exit_price'

    if exit_price is None:
        if state in {'loss', 'stopped_out'}:
            exit_price = _safe_float(getattr(trade, 'stop_price', None))
            pnl_source = 'estimated_from_stop_price'

        elif state in {'win', 'target_hit', 'target2_hit', 'closed'}:
            exit_price = (
                _safe_float(getattr(trade, 'target_2', None))
                or _safe_float(getattr(trade, 'target_1', None))
            )
            pnl_source = 'estimated_from_target_price'

        elif state in {'target1_hit'}:
            exit_price = _safe_float(getattr(trade, 'target_1', None))
            pnl_source = 'estimated_from_target_1'

        elif state == 'breakeven_or_small_win':
            target_1 = _safe_float(getattr(trade, 'target_1', None))

            if target_1 is None:
                return {
                    'pnl': 0.0,
                    'exit_price': entry_price,
                    'pnl_source': 'estimated_breakeven',
                    'closed_at': utc_now(),
                }

            half_qty = qty / 2
            pnl = (target_1 - entry_price) * half_qty * direction

            return {
                'pnl': round(pnl, 2),
                'exit_price': entry_price,
                'pnl_source': 'estimated_half_target_1_half_breakeven',
                'closed_at': utc_now(),
            }

    if exit_price is None or exit_price <= 0:
        return None

    pnl = (exit_price - entry_price) * qty * direction

    return {
        'pnl': round(pnl, 2),
        'exit_price': round(exit_price, 4),
        'pnl_source': pnl_source,
        'closed_at': utc_now(),
    }


def maybe_store_realized_pnl(trade: Trade) -> None:
    """
    Stores realized P&L once, when enough completed trade information exists.
    """
    if getattr(trade, 'pnl', None) is not None:
        return

    pnl_data = calculate_realized_trade_pnl(trade)

    if not pnl_data:
        return

    trade.pnl = pnl_data.get('pnl')
    trade.exit_price = pnl_data.get('exit_price')
    trade.pnl_source = pnl_data.get('pnl_source')
    trade.closed_at = pnl_data.get('closed_at') or utc_now()


def insert_scan(payload: Dict[str, Any]) -> int:
    best = payload.get('best_pick', {})
    scan = Scan(
        created_at=utc_now(),
        market_day=payload.get('day_of_week'),
        best_symbol=best.get('symbol'),
        best_decision=best.get('decision'),
        best_score=best.get('score_total'),
        payload_json=json.dumps(payload),
    )
    db.session.add(scan)
    db.session.commit()
    return scan.id


def insert_trade(trade_data: Dict[str, Any]) -> int:
    if 'user_id' not in trade_data:
        raise KeyError('trade_data["user_id"] is required')

    trade = Trade(
        user_id=trade_data['user_id'],
        created_at=utc_now(),
        updated_at=utc_now(),
        scan_id=trade_data.get('scan_id'),
        symbol=trade_data['symbol'],
        side=trade_data.get('side', 'buy'),
        decision=trade_data.get('decision', 'BUY NOW'),
        status=trade_data.get('status') or trade_data.get('order_status') or 'pending',
        score_total=trade_data.get('score_total'),
        current_price=trade_data.get('current_price'),
        entry_price=trade_data['entry_price'],
        buy_lower=trade_data.get('buy_lower'),
        buy_upper=trade_data.get('buy_upper'),
        stop_price=trade_data['stop_price'],
        target_1=trade_data['target_1'],
        target_2=trade_data['target_2'],
        qty=trade_data.get('qty'),
        risk_per_share=trade_data.get('risk_per_share'),
        reward_to_target_1=trade_data.get('reward_to_target_1'),
        reward_to_target_2=trade_data.get('reward_to_target_2'),
        rr_ratio_1=trade_data.get('rr_ratio_1'),
        rr_ratio_2=trade_data.get('rr_ratio_2'),
        order_id=trade_data.get('order_id'),
        order_status=trade_data.get('order_status'),
        filled_avg_price=trade_data.get('filled_avg_price'),
        filled_qty=trade_data.get('filled_qty'),
        exit_price=trade_data.get('exit_price'),
        pnl=trade_data.get('pnl'),
        pnl_source=trade_data.get('pnl_source'),
        closed_at=trade_data.get('closed_at'),
        outcome=trade_data.get('outcome'),
        notes=trade_data.get('notes'),
        raw_json=json.dumps(trade_data.get('raw_json', {})),
    )
    maybe_store_realized_pnl(trade)

    db.session.add(trade)
    db.session.commit()
    return trade.id


def update_trade_status(order_id: str, updates: Dict[str, Any]) -> None:
    trade = Trade.query.filter_by(order_id=order_id).first()
    if not trade:
        return

    allowed = {
        'order_status',
        'status',
        'filled_avg_price',
        'filled_qty',
        'exit_price',
        'pnl',
        'pnl_source',
        'closed_at',
        'outcome',
        'notes',
        'raw_json',
        'current_price',
        'entry_price',
        'stop_price',
        'target_1',
        'target_2',
        'qty',
    }

    for key, value in updates.items():
        if key not in allowed:
            continue
        if key == 'raw_json':
            value = json.dumps(value) if isinstance(value, dict) else value
        setattr(trade, key, value)

    maybe_store_realized_pnl(trade)

    trade.updated_at = utc_now()
    db.session.commit()


def get_recent_scans(limit: int = 10) -> Iterable[Dict[str, Any]]:
    scans = Scan.query.order_by(Scan.id.desc()).limit(limit).all()
    return [_model_to_dict(s) for s in scans]


def get_recent_trades(limit: int = 20) -> Iterable[Dict[str, Any]]:
    trades = Trade.query.order_by(Trade.id.desc()).limit(limit).all()
    return [_model_to_dict(t) for t in trades]


def get_trade_by_order_id(order_id: str) -> Optional[Dict[str, Any]]:
    trade = Trade.query.filter_by(order_id=order_id).order_by(Trade.id.desc()).first()
    return _model_to_dict(trade) if trade else None


def get_failed_trades_today() -> int:
    et_zone = ZoneInfo(config.TIMEZONE_LABEL)
    now_et = datetime.now(et_zone)
    start_et = datetime.combine(now_et.date(), time.min, tzinfo=et_zone)
    end_et = datetime.combine(now_et.date(), time.max, tzinfo=et_zone)
    start_utc = start_et.astimezone(timezone.utc)
    end_utc = end_et.astimezone(timezone.utc)

    count = Trade.query.filter(
        Trade.created_at >= start_utc,
        Trade.created_at <= end_utc,
        Trade.outcome.in_(['loss', 'stopped_out', 'rejected', 'failed']),
    ).count()
    return count


def get_trade_by_target1_id(target_1_id: str, user_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    # Use SQLAlchemy's func.json_extract to query inside the JSON column natively
    query = Trade.query.filter(
        func.json_extract(Trade.raw_json, '$.order_bundle.target_1_order_id') == target_1_id
    )
    if user_id is not None:
        query = query.filter(Trade.user_id == user_id)

    trade = query.order_by(Trade.id.desc()).first()
    return _model_to_dict(trade) if trade else None




def ensure_trade_audit_table() -> None:
    db.session.execute(text("""
        CREATE TABLE IF NOT EXISTS trade_audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            user_id INTEGER,
            email TEXT,
            trading_mode TEXT,
            subscription_status TEXT,
            symbol TEXT,
            scan_id INTEGER,
            qty REAL,
            entry_price REAL,
            stop_price REAL,
            target_1 REAL,
            target_2 REAL,
            order_id TEXT,
            order_status TEXT,
            raw_json TEXT
        )
    """))
    db.session.commit()


def insert_trade_audit_log(payload: Dict[str, Any]) -> int:
    ensure_trade_audit_table()
    raw_json = payload.get('raw_json', {})
    raw_json_str = raw_json if isinstance(raw_json, str) else json.dumps(raw_json)

    result = db.session.execute(
        text("""
            INSERT INTO trade_audit_logs (
                created_at, user_id, email, trading_mode, subscription_status,
                symbol, scan_id, qty, entry_price, stop_price, target_1, target_2,
                order_id, order_status, raw_json
            )
            VALUES (
                :created_at, :user_id, :email, :trading_mode, :subscription_status,
                :symbol, :scan_id, :qty, :entry_price, :stop_price, :target_1, :target_2,
                :order_id, :order_status, :raw_json
            )
        """),
        {
            'created_at': payload.get('created_at') or utc_now().isoformat(),
            'user_id': payload.get('user_id'),
            'email': payload.get('email'),
            'trading_mode': payload.get('trading_mode'),
            'subscription_status': payload.get('subscription_status'),
            'symbol': payload.get('symbol'),
            'scan_id': payload.get('scan_id'),
            'qty': payload.get('qty'),
            'entry_price': payload.get('entry_price'),
            'stop_price': payload.get('stop_price'),
            'target_1': payload.get('target_1'),
            'target_2': payload.get('target_2'),
            'order_id': payload.get('order_id'),
            'order_status': payload.get('order_status'),
            'raw_json': raw_json_str,
        },
    )
    db.session.commit()
    return int(result.lastrowid)


def get_recent_trade_audit_logs(limit: int = 50) -> Iterable[Dict[str, Any]]:
    ensure_trade_audit_table()
    rows = db.session.execute(
        text("""
            SELECT id, created_at, user_id, email, trading_mode, subscription_status,
                   symbol, scan_id, qty, entry_price, stop_price, target_1, target_2,
                   order_id, order_status, raw_json
            FROM trade_audit_logs
            ORDER BY id DESC
            LIMIT :limit
        """),
        {'limit': limit},
    ).mappings().all()

    logs = []
    for row in rows:
        item = dict(row)
        raw = item.get('raw_json')
        if isinstance(raw, str):
            try:
                item['raw_json'] = json.loads(raw)
            except json.JSONDecodeError:
                pass
        logs.append(item)
    return logs

def get_current_market_regime() -> Optional[Dict[str, Any]]:
    regime = MarketRegime.query.order_by(MarketRegime.updated_at.desc(), MarketRegime.id.desc()).first()
    return _model_to_dict(regime) if regime else None
