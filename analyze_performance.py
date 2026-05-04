import json
import math
from pathlib import Path

import pandas as pd

from models import db, Trade, User, Scan

REPORT_PATH = Path("static/performance_report.json")

def _is_win(outcome: str) -> bool:
    """
    Shared helper used by update_weights.py.

    This does not change trade logic. It only classifies already-recorded
    outcomes for feedback reporting.
    """
    normalized = str(outcome or "").strip().lower()
    return normalized in {
        "win",
        "target_hit",
        "target1_hit",
        "target2_hit",
        "partial_win",
        "breakeven_or_small_win",
        "closed",
    }


def _load_rows():
    """
    Load trade and scan rows for catalyst feedback generation.

    Returns:
        trades: list of dicts with symbol/outcome/scan_id
        scans: dict keyed by scan id with decoded scan payload
    """
    trades = []
    for trade in Trade.query.all():
        trades.append({
            "symbol": trade.symbol,
            "outcome": trade.outcome or trade.status or "",
            "scan_id": trade.scan_id or 0,
        })
    scans = {}
    for scan in Scan.query.all():
        try:
            scans[scan.id] = json.loads(scan.payload_json or "{}")
        except json.JSONDecodeError:
            scans[scan.id] = {}
    return trades, scans


def calculate_metrics(trades_df):
    """Calculates institutional-grade backtest metrics."""
    if trades_df.empty:
        return {}

    # Separate winning and losing trades
    winning_trades = trades_df[trades_df["pnl"] > 0]
    losing_trades = trades_df[trades_df["pnl"] < 0]

    gross_profit = winning_trades["pnl"].sum()
    gross_loss = abs(losing_trades["pnl"].sum())

    # Core Metrics
    total_trades = len(trades_df)
    win_rate = round((len(winning_trades) / total_trades) * 100, 2)
    profit_factor = round(gross_profit / gross_loss, 2) if gross_loss != 0 else float("inf")

    # Calculate Equity Curve and Drawdown
    trades_df["cumulative_pnl"] = trades_df["pnl"].cumsum()
    trades_df["high_water_mark"] = trades_df["cumulative_pnl"].cummax()
    trades_df["drawdown"] = trades_df["cumulative_pnl"] - trades_df["high_water_mark"]
    max_drawdown = round(abs(trades_df["drawdown"].min()), 2)

    return {
        "total_trades": total_trades,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "max_drawdown_dollars": max_drawdown,
        "net_profit": round(trades_df["cumulative_pnl"].iloc[-1], 2),
        "equity_curve_labels": trades_df["date"].tolist(),
        "equity_curve_data": trades_df["cumulative_pnl"].tolist(),
    }


def generate_report():
    """
    Generate performance report from real saved Trade.pnl values only.

    No simulated PnL.
    No random performance.
    No fake win rate.
    """
    trades = (
        Trade.query
        .filter(Trade.pnl.isnot(None))
        .order_by(Trade.closed_at.asc(), Trade.updated_at.asc(), Trade.created_at.asc())
        .all()
    )
    rows = []
    for trade in trades:
        dt = trade.closed_at or trade.updated_at or trade.created_at
        rows.append({
            "date": dt.strftime("%Y-%m-%d") if dt else "",
            "pnl": float(trade.pnl or 0.0),
        })

    if not rows:
        metrics = {
            "total_trades": 0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "max_drawdown_dollars": 0.0,
            "net_profit": 0.0,
            "equity_curve_labels": [],
            "equity_curve_data": [],
            "source": "real_database_pnl_column",
            "message": "No completed trades with saved realized P&L were found.",
        }
    else:
        df = pd.DataFrame(rows)
        metrics = calculate_metrics(df)
        metrics["source"] = "real_database_pnl_column"
        metrics["message"] = "Performance report generated from saved realized P&L only."

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2)
    return metrics


def create_report_app():
    import os
    from flask import Flask
    import config

    app = Flask(__name__)
    app.config["SECRET_KEY"] = config.SECRET_KEY
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{os.path.abspath(config.DB_PATH)}"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    return app


def _extract_trade_pnl(trade):
    """
    Best-effort extraction of a realized trade PnL from the Trade model.
    Prefers a direct `pnl` attribute when present, then falls back to JSON payload keys.
    """
    direct_pnl = getattr(trade, "pnl", None)
    if direct_pnl is not None:
        return float(direct_pnl)

    raw_payload = getattr(trade, "raw_json", None)
    if not raw_payload:
        return None

    try:
        payload = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
    except (TypeError, ValueError):
        return None

    for key in ("pnl", "realized_pnl", "realized_pl"):
        value = payload.get(key) if isinstance(payload, dict) else None
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    return None


def calculate_user_kelly_fraction(user_id):
    """
    Calculate a user's Half-Kelly risk fraction from realized trade history.
    Returns:
      - None: insufficient sample (< 10 realized trades) or unusable trade data
      - 0: negative expected value
      - float in [0.005, 0.05]: bounded Half-Kelly fraction
    """
    user = User.query.filter_by(id=user_id).first()
    if not user:
        return None

    realized_trades = Trade.query.filter(
        Trade.user_id == user_id,
        Trade.status.in_(["filled", "closed"]),
    ).all()

    if len(realized_trades) < 10:
        return None

    trade_pnls = []
    for trade in realized_trades:
        pnl = _extract_trade_pnl(trade)
        if pnl is None or not math.isfinite(pnl):
            continue
        trade_pnls.append(pnl)

    if len(trade_pnls) < 10:
        return None

    winners = [pnl for pnl in trade_pnls if pnl > 0]
    losers = [pnl for pnl in trade_pnls if pnl <= 0]

    total_trades = len(trade_pnls)
    if total_trades == 0 or not winners or not losers:
        return 0

    win_rate = len(winners) / total_trades
    avg_win = sum(winners) / len(winners)
    avg_loss = sum(losers) / len(losers)  # Negative or zero

    if avg_loss == 0:
        return 0.05

    win_loss_ratio = avg_win / abs(avg_loss)
    if win_loss_ratio <= 0:
        return 0

    kelly_raw = win_rate - ((1 - win_rate) / win_loss_ratio)
    if kelly_raw < 0:
        return 0

    half_kelly = kelly_raw / 2
    return max(0.005, min(0.05, half_kelly))


if __name__ == "__main__":
    app = create_report_app()

    with app.app_context():
        report = generate_report()

    print(f"Performance report generated successfully at {REPORT_PATH}")
    print(f"Total trades: {report.get('total_trades')}")
    print(f"Net profit: {report.get('net_profit')}")
    print(f"Win rate: {report.get('win_rate')}%")
