import json
from typing import Any, Dict, List

import numpy as np
import pandas as pd

# We output to a static JSON file so the web server doesn't have to recalculate this on every page load
REPORT_PATH = "static/performance_report.json"
SCHEMA_DOC_PATH = "docs/strategy_sandbox_historical_data_schema.md"
DISCLOSURE_TEXT = (
    "Sandbox results are based on historical or paper-mode data and do not guarantee "
    "future live trading results."
)

BASELINE_SANDBOX_PARAMS = {
    "min_score_to_execute": 0,
    "target2_trailing_stop_pct": 0,
    "min_catalyst_score": 0,
    "min_rvol": 0,
    "max_spread_pct": 100,
}

FILTER_COLUMN_CANDIDATES = {
    "min_score_to_execute": ["score_total", "score_to_execute", "score", "execution_score", "min_score_to_execute"],
    "target2_trailing_stop_pct": ["target2_trailing_stop_pct"],
    "min_catalyst_score": ["catalyst_score", "min_catalyst_score"],
    "min_rvol": ["rvol", "relative_volume", "min_rvol"],
    "max_spread_pct": ["spread_pct", "spread", "max_spread_pct"],
}

DATE_COLUMN_CANDIDATES = ["date", "trade_date", "timestamp", "datetime"]
PNL_COLUMN_CANDIDATES = ["pnl", "net_pnl", "profit_loss"]
OPTIONAL_FILTER_COLUMNS = [
    "score_total",
    "setup_grade",
    "catalyst_score",
    "rvol",
    "relative_volume",
    "spread_pct",
    "target2_trailing_stop_pct",
    "decision",
    "symbol",
]


def _zero_metrics() -> Dict[str, Any]:
    return {
        "total_trades": 0,
        "win_rate": 0,
        "profit_factor": 0,
        "max_drawdown_dollars": 0,
        "net_profit": 0,
        "equity_curve_labels": [],
        "equity_curve_data": [],
    }


def _sanitize_json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _sanitize_json_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_json_value(v) for v in value]
    if isinstance(value, tuple):
        return [_sanitize_json_value(v) for v in value]

    if isinstance(value, (pd.Timestamp, np.datetime64)):
        return pd.Timestamp(value).strftime("%Y-%m-%d")

    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        if np.isnan(value) or np.isinf(value):
            return 0
        return float(value)

    if pd.isna(value):
        return None

    return value


def _find_first_existing_column(df: pd.DataFrame, candidates: List[str]) -> str:
    for col in candidates:
        if col in df.columns:
            return col
    return ""


def load_historical_data(csv_path: str = "historical_data.csv") -> pd.DataFrame:
    """Load historical data for report generation and sandbox simulations."""
    return pd.read_csv(csv_path)


def calculate_metrics(trades_df):
    """Calculates institutional-grade backtest metrics."""
    if trades_df.empty:
        return {}

    df = trades_df.copy()

    pnl_column = "pnl" if "pnl" in df.columns else _find_first_existing_column(df, PNL_COLUMN_CANDIDATES)
    if not pnl_column:
        return _zero_metrics()

    df[pnl_column] = pd.to_numeric(df[pnl_column], errors="coerce").fillna(0.0)

    # Separate winning and losing trades
    winning_trades = df[df[pnl_column] > 0]
    losing_trades = df[df[pnl_column] < 0]

    gross_profit = winning_trades[pnl_column].sum()
    gross_loss = abs(losing_trades[pnl_column].sum())

    # Core Metrics
    total_trades = len(df)
    win_rate = round((len(winning_trades) / total_trades) * 100, 2) if total_trades else 0
    profit_factor = round(gross_profit / gross_loss, 2) if gross_loss != 0 else float("inf")

    # Calculate Equity Curve and Drawdown
    df["cumulative_pnl"] = df[pnl_column].cumsum()
    df["high_water_mark"] = df["cumulative_pnl"].cummax()
    df["drawdown"] = df["cumulative_pnl"] - df["high_water_mark"]
    max_drawdown = round(abs(df["drawdown"].min()), 2) if not df.empty else 0

    date_column = "date" if "date" in df.columns else _find_first_existing_column(df, DATE_COLUMN_CANDIDATES)
    labels = (
        pd.to_datetime(df[date_column], errors="coerce").dt.strftime("%Y-%m-%d").fillna("").tolist()
        if date_column
        else [str(i) for i in range(1, len(df) + 1)]
    )

    return {
        "total_trades": total_trades,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "max_drawdown_dollars": max_drawdown,
        "net_profit": round(df["cumulative_pnl"].iloc[-1], 2),
        "equity_curve_labels": labels,
        "equity_curve_data": df["cumulative_pnl"].tolist(),
    }


def run_strategy_simulation(
    params: Dict[str, Any], days: int = 30, csv_path: str = "historical_data.csv"
) -> Dict[str, Any]:
    warnings: List[str] = []
    safe_days = max(int(days), 1)

    try:
        historical_df = load_historical_data(csv_path)
    except FileNotFoundError:
        return {
            "ok": False,
            "error": (
                "Historical data is not available yet. Run paper/live collection first and follow the schema at "
                f"{SCHEMA_DOC_PATH}."
            ),
            "params": _sanitize_json_value(params or {}),
            "baseline_params": _sanitize_json_value(BASELINE_SANDBOX_PARAMS.copy()),
            "metrics": _zero_metrics(),
            "warnings": [f"CSV not found: {csv_path}"],
            "data_window": {"start": None, "end": None, "days": safe_days},
            "disclosure": DISCLOSURE_TEXT,
        }

    baseline_params = BASELINE_SANDBOX_PARAMS.copy()
    user_params = params.copy() if isinstance(params, dict) else {}
    merged_params = {**baseline_params, **user_params}

    working_df = historical_df.copy()
    required_columns = {"date", "pnl"}
    missing_required = sorted(col for col in required_columns if col not in working_df.columns)
    if missing_required:
        return {
            "ok": False,
            "error": (
                f"historical_data.csv is missing required columns: {', '.join(missing_required)}. "
                f"See {SCHEMA_DOC_PATH}."
            ),
            "params": _sanitize_json_value(merged_params),
            "baseline_params": _sanitize_json_value(baseline_params),
            "metrics": _zero_metrics(),
            "warnings": [],
            "data_window": {"start": None, "end": None, "days": safe_days},
            "disclosure": DISCLOSURE_TEXT,
        }

    missing_optional = [col for col in OPTIONAL_FILTER_COLUMNS if col not in working_df.columns]
    if missing_optional:
        warnings.append(
            "Missing optional columns were skipped: "
            + ", ".join(missing_optional)
            + f". See {SCHEMA_DOC_PATH}."
        )

    working_df["date"] = pd.to_datetime(working_df["date"], errors="coerce")
    invalid_date_rows = int(working_df["date"].isna().sum())
    if invalid_date_rows:
        warnings.append(
            f"Dropped {invalid_date_rows} row(s) with unparsable date values. "
            "Expected YYYY-MM-DD or timestamp format."
        )
    working_df = working_df.dropna(subset=["date"])

    working_df["pnl"] = pd.to_numeric(working_df["pnl"], errors="coerce")
    invalid_pnl_rows = int(working_df["pnl"].isna().sum())
    if invalid_pnl_rows:
        warnings.append(f"Dropped {invalid_pnl_rows} row(s) with non-numeric pnl values.")
    working_df = working_df.dropna(subset=["pnl"])

    if "spread_pct" in working_df.columns:
        spread_series = pd.to_numeric(working_df["spread_pct"], errors="coerce")
        if (spread_series > 1).any():
            warnings.append(
                "spread_pct should be in decimal form (e.g., 0.003 for 0.3%). "
                f"Review values in historical_data.csv and schema guidance at {SCHEMA_DOC_PATH}."
            )

    date_column = _find_first_existing_column(working_df, DATE_COLUMN_CANDIDATES)
    window_start = None
    window_end = None

    if date_column:
        working_df[date_column] = pd.to_datetime(working_df[date_column], errors="coerce")
        working_df = working_df.dropna(subset=[date_column]).sort_values(date_column)
        if not working_df.empty:
            window_end_ts = working_df[date_column].max()
            window_start_ts = (window_end_ts - pd.Timedelta(days=safe_days - 1)).normalize()
            mask = working_df[date_column] >= window_start_ts
            working_df = working_df.loc[mask]
            window_start = window_start_ts.strftime("%Y-%m-%d")
            window_end = window_end_ts.strftime("%Y-%m-%d")
    else:
        warnings.append("No date column found; unable to filter to the last calendar days.")

    for param_name, candidates in FILTER_COLUMN_CANDIDATES.items():
        column = _find_first_existing_column(working_df, candidates)
        value = merged_params.get(param_name)

        if column == "":
            warnings.append(f"Skipped filter '{param_name}' because no matching column exists in historical_data.csv.")
            continue

        numeric_value = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
        if pd.isna(numeric_value):
            warnings.append(f"Skipped filter '{param_name}' because provided value '{value}' is not numeric.")
            continue

        working_df[column] = pd.to_numeric(working_df[column], errors="coerce")
        if param_name == "max_spread_pct":
            working_df = working_df[working_df[column] <= float(numeric_value)]
        else:
            working_df = working_df[working_df[column] >= float(numeric_value)]

    pnl_column = _find_first_existing_column(working_df, PNL_COLUMN_CANDIDATES)
    if pnl_column and pnl_column != "pnl":
        working_df = working_df.rename(columns={pnl_column: "pnl"})

    if date_column and date_column != "date" and date_column in working_df.columns:
        working_df = working_df.rename(columns={date_column: "date"})

    if working_df.empty:
        warnings.append("No trades matched the selected rules.")
        metrics = _zero_metrics()
    else:
        metrics = calculate_metrics(working_df)
        if not metrics:
            metrics = _zero_metrics()

    result = {
        "ok": True,
        "params": merged_params,
        "baseline_params": baseline_params,
        "metrics": metrics,
        "warnings": warnings,
        "data_window": {"start": window_start, "end": window_end, "days": safe_days},
        "disclosure": DISCLOSURE_TEXT,
    }

    return _sanitize_json_value(result)


def generate_report():
    try:
        df = load_historical_data("historical_data.csv")
        metrics = _sanitize_json_value(calculate_metrics(df))
    except FileNotFoundError:
        metrics = _sanitize_json_value(_zero_metrics())

    with open(REPORT_PATH, "w") as f:
        json.dump(metrics, f)

    print(f"Performance report generated successfully at {REPORT_PATH}")


if __name__ == "__main__":
    generate_report()
