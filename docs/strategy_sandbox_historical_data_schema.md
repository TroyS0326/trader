# Strategy Sandbox `historical_data.csv` Schema

This document defines the expected CSV schema used by the Strategy Sandbox backtest/simulation flow.

## Required minimum columns

- `date`
- `pnl`

## Optional columns used for filtering when available

- `score_total`
- `setup_grade`
- `catalyst_score`
- `rvol`
- `relative_volume`
- `spread_pct`
- `target2_trailing_stop_pct`
- `decision`
- `symbol`

## Field expectations

1. `date` should be parseable as `YYYY-MM-DD` or a timestamp.
2. `pnl` should be numeric.
3. `score_total` is used for `min_score_to_execute` filtering.
4. `catalyst_score` is used for catalyst filtering.
5. `rvol` or `relative_volume` is used for volume filtering.
6. `spread_pct` is expected in decimal form (for example, `0.003` for `0.3%`).
7. Missing optional columns are skipped and returned as warnings.
8. Historical/paper-mode data is not a promise of future live results.
