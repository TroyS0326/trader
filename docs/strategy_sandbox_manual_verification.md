# Strategy Sandbox Manual Verification Checklist

This checklist is for validating Strategy Sandbox behavior end-to-end (API + frontend) without adding a full automated test suite.

## Scope covered

1. `/api/strategy-sandbox` requires login.
2. Invalid inputs are clamped or rejected safely.
3. Missing `historical_data.csv` returns a clean user-safe error.
4. Missing optional CSV columns produce warnings, not crashes.
5. Empty matching results return zero metrics and a warning.
6. Valid CSV with `date` and `pnl` returns metrics.
7. The frontend displays sandbox results.
8. The frontend does not show fake data when the API fails.
9. Existing `/api/transparency/stats` still works.
10. Existing static performance chart still works.

---

## Prerequisites

- Run from repo root (`/workspace/trader`).
- Start the app in one terminal:

```bash
python app.py
```

- In another terminal, set variables for API testing:

```bash
export BASE_URL="http://127.0.0.1:5000"
export COOKIE_JAR="/tmp/trader.cookies"
```

- If your local app is running on a different host/port, update `BASE_URL`.

---

## A) Authentication checks

### A1. Missing auth should be blocked (required)

```bash
curl -i -sS \
  -X POST "$BASE_URL/api/strategy-sandbox" \
  -H 'Content-Type: application/json' \
  -d '{"days":30}'
```

Expected:
- HTTP status is a redirect to login (`302`) or unauthorized (`401`), depending on auth middleware configuration.
- Request should **not** return sandbox metrics for anonymous access.

### A2. Authenticated session (if possible)

Use a valid local user from your dev DB:

```bash
export EMAIL="your-test-user@example.com"
export PASSWORD="your-password"
```

```bash
curl -i -sS \
  -c "$COOKIE_JAR" \
  -X POST "$BASE_URL/login" \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  --data-urlencode "email=$EMAIL" \
  --data-urlencode "password=$PASSWORD"
```

Expected:
- Login returns `302` to `/dashboard` on success.
- `COOKIE_JAR` file is created with a session cookie.

---

## B) Strategy sandbox API checks

### B1. Invalid params are safely handled (clamp/reject path)

```bash
curl -i -sS \
  -b "$COOKIE_JAR" \
  -X POST "$BASE_URL/api/strategy-sandbox" \
  -H 'Content-Type: application/json' \
  -d '{
    "min_score_to_execute": -999,
    "target2_trailing_stop_pct": "not-a-number",
    "min_catalyst_score": 999,
    "min_rvol": -10,
    "max_spread_pct": 999,
    "days": 1000
  }'
```

Expected:
- Endpoint does not crash.
- Response is structured JSON (`ok`, `data` or `error`).
- Either:
  - values are clamped to allowed bounds and simulation returns safely, or
  - a safe validation-style error is returned.

### B2. Normal params should return data safely

```bash
curl -i -sS \
  -b "$COOKIE_JAR" \
  -X POST "$BASE_URL/api/strategy-sandbox" \
  -H 'Content-Type: application/json' \
  -d '{
    "min_score_to_execute": 25,
    "target2_trailing_stop_pct": 5,
    "min_catalyst_score": 2,
    "min_rvol": 1.5,
    "max_spread_pct": 0.10,
    "days": 30
  }'
```

Expected:
- HTTP `200` with `{"ok": true, "data": ...}` when dataset is valid.
- `data` includes metrics fields such as `total_trades`, `win_rate`, `net_profit`, `equity_curve_labels`, and `equity_curve_data`.

### B3. Missing `historical_data.csv` returns clean user-safe error

Temporarily move CSV out of the way:

```bash
mv historical_data.csv historical_data.csv.bak
```

Call API:

```bash
curl -i -sS \
  -b "$COOKIE_JAR" \
  -X POST "$BASE_URL/api/strategy-sandbox" \
  -H 'Content-Type: application/json' \
  -d '{"days":30}'
```

Restore CSV:

```bash
mv historical_data.csv.bak historical_data.csv
```

Expected:
- API returns a user-safe error JSON body (no traceback/internal details).
- Frontend-safe error message is suitable for end users.

### B4. Missing optional columns should warn (no crash)

Create a temporary CSV with only required columns:

```bash
cat > /tmp/sandbox_minimal.csv <<'CSV'
date,pnl
2026-03-01,100
2026-03-02,-50
2026-03-03,75
CSV
```

Use the temporary CSV:

```bash
HISTORICAL_DATA_PATH=/tmp/sandbox_minimal.csv \
curl -i -sS \
  -b "$COOKIE_JAR" \
  -X POST "$BASE_URL/api/strategy-sandbox" \
  -H 'Content-Type: application/json' \
  -d '{"days":30,"min_score_to_execute":0,"min_catalyst_score":0,"min_rvol":0,"max_spread_pct":0.1}'
```

Expected:
- API does not crash.
- Response includes warning entries about skipped optional columns.

### B5. Empty matching result returns zero metrics + warning

```bash
curl -i -sS \
  -b "$COOKIE_JAR" \
  -X POST "$BASE_URL/api/strategy-sandbox" \
  -H 'Content-Type: application/json' \
  -d '{"min_score_to_execute":100,"min_catalyst_score":10,"min_rvol":20,"max_spread_pct":0,"days":1}'
```

Expected:
- API returns success payload with `total_trades` = `0` (and other metrics zeroed/defaulted).
- Warning includes a "no trades matched" style message.

### B6. Valid CSV with `date` + `pnl` returns metrics

Using `/tmp/sandbox_minimal.csv` from B4:

```bash
HISTORICAL_DATA_PATH=/tmp/sandbox_minimal.csv \
curl -i -sS \
  -b "$COOKIE_JAR" \
  -X POST "$BASE_URL/api/strategy-sandbox" \
  -H 'Content-Type: application/json' \
  -d '{"days":30,"min_score_to_execute":0,"min_catalyst_score":0,"min_rvol":0,"max_spread_pct":0.1}'
```

Expected:
- Non-empty metrics are returned.
- `equity_curve_labels` include parsed dates and `equity_curve_data` includes cumulative PnL values.

---

## C) Existing transparency API/chart regression checks

### C1. `/api/transparency/stats` still works

```bash
curl -i -sS "$BASE_URL/api/transparency/stats"
```

Expected:
- HTTP `200` with `ok: true` and data fields used by the transparency page stats.

### C2. Static performance chart still renders

Manual browser check:
1. Open `http://127.0.0.1:5000/transparency`.
2. Confirm top metrics are populated (win rate, profit factor, total trades, max drawdown).
3. Confirm the static execution equity chart is visible (not blank / no JS crash in console).

---

## D) Frontend sandbox behavior checks

### D1. Frontend displays sandbox results

1. While logged in, open `/transparency`.
2. In "Sandbox Strategy What-If Engine", click **Run What-If Test** with defaults.
3. Confirm:
   - Results card becomes visible.
   - Metrics fields are populated.
   - Sandbox equity curve chart updates.

### D2. Frontend does not show fake data on API failure

1. Force API failure (for example, temporarily move `historical_data.csv` as in B3).
2. Click **Run What-If Test**.
3. Confirm:
   - An error message is displayed in sandbox status.
   - UI does **not** fabricate random/demo metrics.
   - No JavaScript exception breaks the page.

---

## Optional cleanup

```bash
rm -f "$COOKIE_JAR" /tmp/sandbox_minimal.csv
```
