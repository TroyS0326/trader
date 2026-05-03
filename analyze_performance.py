import json
import math
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from flask import Flask

import config
from models import db, Trade


BASE_DIR = Path(__file__).resolve().parent
REPORT_PATH = BASE_DIR / "static" / "performance_report.json"


REALIZED_OUTCOMES = {
    "win",
    "partial_win",
    "breakeven_or_small_win",
    "loss",
    "stopped_out",
    "target_hit",
    "target1_hit",
    "target2_hit",
    "closed",
}

SKIP_OUTCOMES = {
    "open",
    "pending",
    "working",
    "working_or_filled",
    "rejected",
    "failed",
    "canceled",
    "cancelled",
    "expired",
}


PNL_KEYS = {
    "pnl",
    "realized_pnl",
    "realized_pl",
    "realized_profit",
    "profit_loss",
    "net_pnl",
    "net_profit",
    "pl",
}


EXIT_PRICE_KEYS = {
    "exit_price",
    "close_price",
    "closed_price",
    "average_exit_price",
    "filled_exit_price",
    "sell_price",
}


def create_report_app() -> Flask:
    """
    Creates a minimal Flask app only for database access.

    This avoids importing the full app.py, which can load routes, Redis, Stripe,
    websockets, scanner modules, and other runtime-only services.
    """
    app = Flask(__name__)
    app.config["SECRET_KEY"] = config.SECRET_KEY
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{os.path.abspath(config.DB_PATH)}"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    return app


def safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None

    try:
        result = float(value)
    except (TypeError, ValueError):
        return None

    if not math.isfinite(result):
        return None

    return result


def safe_int(value: Any) -> Optional[int]:
    result = safe_float(value)
    if result is None:
        return None
    return int(result)


def load_json(value: Any) -> Any:
    if not value:
        return {}

    if isinstance(value, dict):
        return value

    if isinstance(value, list):
        return value

    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {}

    return {}


def find_numeric_key(obj: Any, keys: set) -> Optional[float]:
    """
    Recursively searches raw_json for the first numeric value matching one of the
    requested keys.
    """
    if isinstance(obj, dict):
        for key, value in obj.items():
            if str(key).lower() in keys:
                numeric_value = safe_float(value)
                if numeric_value is not None:
                    return numeric_value

        for value in obj.values():
            found = find_numeric_key(value, keys)
            if found is not None:
                return found

    elif isinstance(obj, list):
        for item in obj:
            found = find_numeric_key(item, keys)
            if found is not None:
                return found

    return None


def trade_date(trade: Trade) -> str:
    dt = getattr(trade, "updated_at", None) or getattr(trade, "created_at", None)

    if not dt:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if isinstance(dt, str):
        try:
            return datetime.fromisoformat(dt).strftime("%Y-%m-%d")
        except ValueError:
            return dt[:10]

    return dt.strftime("%Y-%m-%d")


def trade_sort_key(trade: Trade):
    return (
        getattr(trade, "updated_at", None)
        or getattr(trade, "created_at", None)
        or datetime.min
    )


def derive_pnl_from_trade_plan(trade: Trade, raw_payload: Any) -> Tuple[Optional[float], str]:
    """
    Best-effort P&L estimation when no realized P&L is saved.

    Priority:
    1. If raw_json has actual exit price, use it.
    2. If outcome is loss/stopped_out, use stop_price.
    3. If outcome is win/target2_hit, use target_2 if present, else target_1.
    4. If outcome is partial_win/target1_hit, use target_1.
    5. If outcome is breakeven_or_small_win, estimate half position at target_1
       and half at entry.

    If the trade is not clearly closed or realized, return None.
    """
    outcome = (getattr(trade, "outcome", None) or "").strip().lower()
    status = (getattr(trade, "status", None) or "").strip().lower()
    order_status = (getattr(trade, "order_status", None) or "").strip().lower()

    state = outcome or status or order_status

    if state in SKIP_OUTCOMES:
        return None, f"skipped_non_realized_state:{state}"

    qty = (
        safe_float(getattr(trade, "filled_qty", None))
        or safe_float(getattr(trade, "qty", None))
    )

    entry_price = (
        safe_float(getattr(trade, "filled_avg_price", None))
        or safe_float(getattr(trade, "entry_price", None))
    )

    if qty is None or qty <= 0:
        return None, "missing_qty"

    if entry_price is None or entry_price <= 0:
        return None, "missing_entry_price"

    side = (getattr(trade, "side", None) or "buy").strip().lower()
    direction = -1 if side in {"sell", "short"} else 1

    raw_exit_price = find_numeric_key(raw_payload, EXIT_PRICE_KEYS)

    if raw_exit_price is not None and raw_exit_price > 0:
        pnl = (raw_exit_price - entry_price) * qty * direction
        return round(pnl, 2), "derived_from_raw_exit_price"

    stop_price = safe_float(getattr(trade, "stop_price", None))
    target_1 = safe_float(getattr(trade, "target_1", None))
    target_2 = safe_float(getattr(trade, "target_2", None))

    if state in {"loss", "stopped_out"}:
        if stop_price is None:
            return None, "missing_stop_price_for_loss"

        pnl = (stop_price - entry_price) * qty * direction
        return round(pnl, 2), "estimated_from_stop_price"

    if state in {"win", "target_hit", "target2_hit"}:
        exit_price = target_2 or target_1

        if exit_price is None:
            return None, "missing_target_price_for_win"

        pnl = (exit_price - entry_price) * qty * direction
        return round(pnl, 2), "estimated_from_target_price"

    if state in {"partial_win", "target1_hit"}:
        if target_1 is None:
            return None, "missing_target_1_for_partial_win"

        pnl = (target_1 - entry_price) * qty * direction
        return round(pnl, 2), "estimated_from_target_1"

    if state == "breakeven_or_small_win":
        if target_1 is None:
            return 0.0, "estimated_breakeven_no_target_1"

        half_qty = qty / 2
        pnl = (target_1 - entry_price) * half_qty * direction
        return round(pnl, 2), "estimated_half_target_1_half_breakeven"

    return None, f"skipped_unknown_realized_state:{state or 'blank'}"


def extract_trade_pnl(trade: Trade) -> Tuple[Optional[float], str]:
    """
    Extracts real P&L when available.

    First tries:
    - direct trade.pnl attribute, if added later
    - raw_json fields like pnl, realized_pnl, realized_pl, net_pnl

    Then falls back to estimated P&L from the saved trade plan only when the
    trade outcome clearly shows the trade is complete.
    """
    direct_pnl = safe_float(getattr(trade, "pnl", None))

    if direct_pnl is not None:
        return round(direct_pnl, 2), "direct_trade_pnl"

    raw_payload = load_json(getattr(trade, "raw_json", None))
    raw_pnl = find_numeric_key(raw_payload, PNL_KEYS)

    if raw_pnl is not None:
        return round(raw_pnl, 2), "raw_json_realized_pnl"

    return derive_pnl_from_trade_plan(trade, raw_payload)


def empty_report(message: str, total_db_trades: int = 0) -> Dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()

    return {
        "generated_at": now,
        "source": "real_database",
        "message": message,
        "total_db_trades": total_db_trades,
        "total_trades": 0,
        "included_realized_trades": 0,
        "skipped_trades": total_db_trades,
        "estimated_trade_count": 0,
        "raw_realized_pnl_trade_count": 0,
        "win_rate": 0.0,
        "profit_factor": 0.0,
        "max_drawdown_dollars": 0.0,
        "net_profit": 0.0,
        "gross_profit": 0.0,
        "gross_loss": 0.0,
        "average_win": 0.0,
        "average_loss": 0.0,
        "largest_win": 0.0,
        "largest_loss": 0.0,
        "equity_curve_labels": [],
        "equity_curve_data": [],
        "outcome_counts": {},
        "symbol_counts": {},
        "pnl_source_counts": {},
        "skip_reason_counts": {},
    }


def calculate_metrics(rows: List[Dict[str, Any]], total_db_trades: int, skip_reasons: Counter) -> Dict[str, Any]:
    if not rows:
        return empty_report(
            "No completed trades with usable realized or estimated P&L were found.",
            total_db_trades=total_db_trades,
        )

    rows = sorted(rows, key=lambda item: item["sort_value"])

    pnls = [row["pnl"] for row in rows]
    winners = [pnl for pnl in pnls if pnl > 0]
    losers = [pnl for pnl in pnls if pnl < 0]

    gross_profit = round(sum(winners), 2)
    gross_loss = round(abs(sum(losers)), 2)
    net_profit = round(sum(pnls), 2)

    total_trades = len(rows)
    win_rate = round((len(winners) / total_trades) * 100, 2) if total_trades else 0.0

    if gross_loss > 0:
        profit_factor = round(gross_profit / gross_loss, 2)
    elif gross_profit > 0:
        profit_factor = 999.0
    else:
        profit_factor = 0.0

    cumulative = 0.0
    high_water_mark = 0.0
    max_drawdown = 0.0
    equity_labels = []
    equity_data = []

    for row in rows:
        cumulative += row["pnl"]
        high_water_mark = max(high_water_mark, cumulative)
        drawdown = high_water_mark - cumulative
        max_drawdown = max(max_drawdown, drawdown)

        equity_labels.append(row["date"])
        equity_data.append(round(cumulative, 2))

    outcome_counts = Counter(row["outcome"] for row in rows)
    symbol_counts = Counter(row["symbol"] for row in rows)
    pnl_source_counts = Counter(row["pnl_source"] for row in rows)

    raw_realized_count = sum(
        count
        for source, count in pnl_source_counts.items()
        if source in {"direct_trade_pnl", "raw_json_realized_pnl"}
    )

    estimated_count = total_trades - raw_realized_count

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "real_database",
        "message": "Performance report generated from completed trades in the database.",
        "total_db_trades": total_db_trades,
        "total_trades": total_trades,
        "included_realized_trades": total_trades,
        "skipped_trades": total_db_trades - total_trades,
        "estimated_trade_count": estimated_count,
        "raw_realized_pnl_trade_count": raw_realized_count,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "max_drawdown_dollars": round(max_drawdown, 2),
        "net_profit": net_profit,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "average_win": round(sum(winners) / len(winners), 2) if winners else 0.0,
        "average_loss": round(sum(losers) / len(losers), 2) if losers else 0.0,
        "largest_win": round(max(winners), 2) if winners else 0.0,
        "largest_loss": round(min(losers), 2) if losers else 0.0,
        "equity_curve_labels": equity_labels,
        "equity_curve_data": equity_data,
        "outcome_counts": dict(outcome_counts),
        "symbol_counts": dict(symbol_counts),
        "pnl_source_counts": dict(pnl_source_counts),
        "skip_reason_counts": dict(skip_reasons),
    }


def generate_report() -> Dict[str, Any]:
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)

    trades = Trade.query.order_by(Trade.id.asc()).all()
    total_db_trades = len(trades)

    if total_db_trades == 0:
        report = empty_report(
            "No trades exist in the database yet.",
            total_db_trades=0,
        )

        write_report(report)
        return report

    rows = []
    skip_reasons = Counter()

    for trade in sorted(trades, key=trade_sort_key):
        pnl, pnl_source = extract_trade_pnl(trade)

        if pnl is None:
            skip_reasons[pnl_source] += 1
            continue

        symbol = (getattr(trade, "symbol", None) or "UNKNOWN").upper()
        outcome = (getattr(trade, "outcome", None) or getattr(trade, "status", None) or "unknown").lower()

        rows.append(
            {
                "id": getattr(trade, "id", None),
                "date": trade_date(trade),
                "sort_value": trade_sort_key(trade),
                "symbol": symbol,
                "outcome": outcome,
                "pnl": pnl,
                "pnl_source": pnl_source,
            }
        )

    report = calculate_metrics(rows, total_db_trades, skip_reasons)
    write_report(report)
    return report


def write_report(report: Dict[str, Any]) -> None:
    temp_path = REPORT_PATH.with_suffix(".json.tmp")

    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    os.replace(temp_path, REPORT_PATH)


def main() -> None:
    app = create_report_app()

    with app.app_context():
        report = generate_report()

    print(f"Performance report generated successfully at {REPORT_PATH}")
    print(f"Total DB trades: {report.get('total_db_trades')}")
    print(f"Included realized trades: {report.get('included_realized_trades')}")
    print(f"Skipped trades: {report.get('skipped_trades')}")
    print(f"Net profit: {report.get('net_profit')}")
    print(f"Win rate: {report.get('win_rate')}%")


if __name__ == "__main__":
    main()
