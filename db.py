import json
from datetime import datetime, time, timezone
from typing import Any, Dict, Iterable, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import func

import config
from models import db, Trade, Scan


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
        outcome=trade_data.get('outcome'),
        notes=trade_data.get('notes'),
        raw_json=json.dumps(trade_data.get('raw_json', {})),
    )
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
