import json
import pandas as pd
import numpy as np

# We output to a static JSON file so the web server doesn't have to recalculate this on every page load
REPORT_PATH = "static/performance_report.json"


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
    # In production, this would pull from your SQLite DB or backtest CSVs.
    # For now, we simulate a realistic dataset based on the AI's expected performance.
    dates = pd.date_range(start="2026-01-01", periods=100, freq="B").strftime("%Y-%m-%d")

    # Simulate a strategy with a 62% win rate and a 1.5 profit factor
    np.random.seed(42)
    pnls = np.where(
        np.random.rand(100) < 0.62,
        np.random.normal(150, 50, 100),
        np.random.normal(-100, 20, 100),
    )

    df = pd.DataFrame({"date": dates, "pnl": pnls})

    metrics = calculate_metrics(df)

    with open(REPORT_PATH, "w") as f:
        json.dump(metrics, f)

    print(f"Performance report generated successfully at {REPORT_PATH}")


if __name__ == "__main__":
    generate_report()
