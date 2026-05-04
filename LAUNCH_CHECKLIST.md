# Final Launch Checklist

This checklist is for final production launch validation after production hardening.

## 1) Required VPS Commands

Run in order from the project root:

```bash
git pull origin main
source venv/bin/activate
pip install -r requirements.txt
pip check
python -m py_compile app.py config.py models.py db.py scanner.py broker.py execution.py execution_guard.py tasks.py analyze_performance.py update_weights.py ai_catalyst.py watchlist.py
python -c "import update_weights; print('update_weights import OK')"
python - <<'PY'
from analyze_performance import generate_report
report = generate_report()
print('analyze_performance report generated; total_trades =', report.get('total_trades'))
PY
sudo systemctl restart xeanvi
sudo systemctl status xeanvi --no-pager
journalctl -u xeanvi -n 100 --no-pager
```

## 2) Required Production ENV Checklist

Populate all required values from `.env.example` before launch.

### Core Security (Critical)
- [ ] **SECRET_KEY** (critical)
- [ ] **TOKEN_ENCRYPTION_KEY** (critical)
- [ ] FLASK_DEBUG (set to `0` in production)
- [ ] FLASK_ENV (set to `production`)

### Host / Session / CSRF (Critical)
- [ ] HOST
- [ ] PORT
- [ ] **SESSION_COOKIE_DOMAIN** (critical)
- [ ] **SESSION_COOKIE_SAMESITE** (critical)
- [ ] **SESSION_COOKIE_SECURE** (critical)
- [ ] **WTF_CSRF_TRUSTED_ORIGINS** (critical)

### Redis / Rate Limiting (Critical)
- [ ] **REDIS_URL** (critical)
- [ ] RATELIMIT_STORAGE_URI

### Alpaca (Critical)
- [ ] **ALPACA_CLIENT_ID** (critical)
- [ ] **ALPACA_CLIENT_SECRET** (critical)
- [ ] **ALPACA_REDIRECT_URI** (critical)
- [ ] **ALPACA_API_KEY** (critical)
- [ ] **ALPACA_API_SECRET** (critical)

### Stripe (Critical)
- [ ] STRIPE_PUBLIC_KEY
- [ ] **STRIPE_SECRET_KEY** (critical)
- [ ] **STRIPE_WEBHOOK_SECRET** (critical)
- [ ] **STRIPE_PRICE_ID_MONTHLY** (critical)
- [ ] **STRIPE_PRICE_ID_ANNUAL** (critical)
- [ ] STRIPE_CUSTOMER_PORTAL_RETURN_PATH

### Brevo (Critical)
- [ ] **BREVO_API_KEY** (critical)
- [ ] BREVO_LIST_ID
- [ ] BREVO_SIGNUP_LIST_ID
- [ ] BREVO_WELCOME_TEMPLATE_ENABLED
- [ ] BREVO_RESET_PASSWORD_TEMPLATE_ID
- [ ] BREVO_SENDER_EMAIL
- [ ] BREVO_SENDER_NAME

### Market Data / AI (Critical)
- [ ] **FINNHUB_API_KEY** (critical)
- [ ] **GEMINI_API_KEY** (critical)
- [ ] GEMINI_MODEL

### App URL / Password Reset / Admin
- [ ] **APP_BASE_URL** (critical)
- [ ] META_PIXEL_ID
- [ ] PASSWORD_RESET_TOKEN_MAX_AGE_SECONDS
- [ ] DEV_BYPASS_TOKEN (must be unset in production unless explicitly needed)
- [ ] ADMIN_EMAIL

### Storage / DB
- [ ] DB_FALLBACK_DIR

### Trading / Risk Controls
- [ ] SCAN_CANDIDATE_LIMIT
- [ ] WATCHLIST_SIZE
- [ ] MAX_BUY_SHARES
- [ ] DEFAULT_RISK_CAPITAL
- [ ] CURRENT_BANKROLL
- [ ] KELLY_FRACTION
- [ ] MAX_PORTFOLIO_HEAT
- [ ] MAX_DOLLAR_LOSS_PER_TRADE
- [ ] MAX_FAILED_TRADES_PER_DAY
- [ ] WATCHLIST_PUSH_SECONDS
- [ ] ORDER_STATUS_POLL_SECONDS
- [ ] MIN_SCORE_TO_EXECUTE
- [ ] MIN_CATALYST_SCORE
- [ ] NO_BUY_BEFORE_ET
- [ ] OPENING_RANGE_START_ET
- [ ] OPENING_RANGE_END_ET
- [ ] MAX_SPREAD_PCT
- [ ] MAX_ENTRY_EXTENSION_PCT
- [ ] OR_BREAKOUT_BUFFER_PCT
- [ ] PULLBACK_MAX_RETRACE_PCT
- [ ] ENTRY_ORDER_TIMEOUT_SECONDS
- [ ] ENTRY_ORDER_POLL_SECONDS
- [ ] TARGET2_TRAILING_STOP_PCT
- [ ] MARKET_INTERNALS_BLOCK_ENABLED
- [ ] MARKET_INTERNALS_TICK_SYMBOL
- [ ] MARKET_INTERNALS_ADD_SYMBOL
- [ ] CRYPTO_SCAN_ENABLED
- [ ] CRYPTO_SYMBOLS
- [ ] MIN_PREMARKET_GAP_PCT
- [ ] MIN_PREMARKET_DOLLAR_VOL
- [ ] MIN_SECTOR_SYMPATHY_SCORE
- [ ] MIN_RVOL
- [ ] MAX_FLOAT
- [ ] A_PLUS_SCORE
- [ ] A_SCORE
- [ ] LUNCH_BLOCK_START
- [ ] LUNCH_BLOCK_END
- [ ] VA_PERCENT
- [ ] ATR_STOP_MULT
- [ ] RS_SECTOR_MULT
- [ ] VIX_SYMBOL
- [ ] VIX_CIRCUIT_BREAKER_PCT
- [ ] VIX_PENALTY_MULTIPLIER

## 3) Browser Smoke Test Checklist

- [ ] Home page loads
- [ ] Waitlist page and submit flow works
- [ ] Signup page and submit flow works
- [ ] Login page and login flow works
- [ ] Forgot password request flow works
- [ ] Dashboard loads after login
- [ ] Scan button action works and returns expected UI state
- [ ] Settings page loads and saves
- [ ] Onboarding flow loads and progresses
- [ ] Setup checklist page loads and updates state
- [ ] Pricing / upgrade page loads and checkout button works
- [ ] Privacy policy page loads
- [ ] Terms page loads
- [ ] `/sitemap.xml` resolves
- [ ] `/robots.txt` resolves

## 4) Trading Safety Smoke Test

- [ ] Confirm paper mode defaults to Alpaca paper endpoint/account
- [ ] Confirm live mode requires PRO subscription and approved scan
- [ ] Confirm `/api/execute` enforces `validate_execution_against_approved_scan`
- [ ] Confirm no raw Alpaca tokens are printed in logs
- [ ] Confirm performance report uses only real `Trade.pnl` rows
- [ ] Confirm no fake/random performance generation remains

## 5) Stripe / Brevo / Alpaca Smoke Test

- [ ] Stripe checkout session is created successfully
- [ ] Stripe webhook is received and processed successfully
- [ ] Brevo waitlist subscription flow succeeds
- [ ] Brevo password reset email flow succeeds
- [ ] Alpaca OAuth paper connection succeeds
- [ ] Alpaca tokens are stored encrypted at rest

## 6) Rollback Plan

```bash
git log --oneline -5
git reset --hard <previous_commit>
sudo systemctl restart xeanvi
journalctl -u xeanvi -n 100 --no-pager
```

## 7) Known Non-Errors

- `performance report total_trades: 0` means no closed P&L rows exist yet.
- Codex sandbox missing `flask_login` is an environment-only issue if VPS checks pass.
