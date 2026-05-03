import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from flask import Flask

import config
from models import db, Trade


BASE_DIR = Path(__file__).resolve().parent
REPORT_PATH = BASE_DIR / "static" / "performance_report.json"
MIN_PUBLIC_PERFORMANCE_TRADES = int(os.getenv("MIN_PUBLIC_PERFORMANCE_TRADES", "25"))


def create_report_app() -> Flask:
    """
    Creates a minimal Flask app only for database access.

    This avoids importing full app.py and accidentally loading routes,
    Redis, Stripe, websockets, scanner modules, or other runtime services.
    """
    app = Flask(__name__)
    app.config["SECRET_KEY"] = config.SECRET_KEY
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{os.path.abspath(config.DB_PATH)}"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    return app


def empty_report(message: str, total_db_trades: int = 0) -> Dict[str, Any]:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "real_database_pnl_column",
        "message": message,
        "public_report_ready": False,
        "minimum_public_trades": MIN_PUBLIC_PERFORMANCE_TRADES,
        "public_status": "building_sample_size",
        "total_db_trades": total_db_trades,
        "total_trades": 0,
        "included_realized_trades": 0,
        "skipped_trades": total_db_trades,
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
    }


def trade_date(trade: Trade) -> str:
    dt = (
        getattr(trade, "closed_at", None)
        or getattr(trade, "updated_at", None)
        or getattr(trade, "created_at", None)
    )

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
        getattr(trade, "closed_at", None)
        or getattr(trade, "updated_at", None)
        or getattr(trade, "created_at", None)
        or datetime.min
    )


def calculate_metrics(trades: List[Trade], total_db_trades: int) -> Dict[str, Any]:
    if not trades:
        return empty_report(
            "No completed trades with saved realized P&L were found.",
            total_db_trades=total_db_trades,
        )

    trades = sorted(trades, key=trade_sort_key)

    rows = []
    for trade in trades:
        pnl = float(trade.pnl)

        rows.append(
            {
                "id": trade.id,
                "date": trade_date(trade),
                "symbol": (trade.symbol or "UNKNOWN").upper(),
                "outcome": (trade.outcome or trade.status or "unknown").lower(),
                "pnl": round(pnl, 2),
                "pnl_source": trade.pnl_source or "saved_trade_pnl",
            }
        )

    pnls = [row["pnl"] for row in rows]
    winners = [pnl for pnl in pnls if pnl > 0]
    losers = [pnl for pnl in pnls if pnl < 0]

    gross_profit = round(sum(winners), 2)
    gross_loss = round(abs(sum(losers)), 2)
    net_profit = round(sum(pnls), 2)

    total_trades = len(rows)
    public_report_ready = total_trades >= MIN_PUBLIC_PERFORMANCE_TRADES
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

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "real_database_pnl_column",
        "message": "Performance report generated from saved realized P&L in the trades table.",
        "public_report_ready": public_report_ready,
        "minimum_public_trades": MIN_PUBLIC_PERFORMANCE_TRADES,
        "public_status": "ready" if public_report_ready else "building_sample_size",
        "total_db_trades": total_db_trades,
        "total_trades": total_trades,
        "included_realized_trades": total_trades,
        "skipped_trades": total_db_trades - total_trades,
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
    }


def write_report(report: Dict[str, Any]) -> None:
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)

    temp_path = REPORT_PATH.with_suffix(".json.tmp")

    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    os.replace(temp_path, REPORT_PATH)


def generate_report() -> Dict[str, Any]:
    total_db_trades = Trade.query.count()

    completed_trades = (
        Trade.query
        .filter(Trade.pnl.isnot(None))
        .order_by(Trade.id.asc())
        .all()
    )

    report = calculate_metrics(completed_trades, total_db_trades)
    write_report(report)
    return report


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
