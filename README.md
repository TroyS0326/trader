# XeanVI — AI-Powered Trading Execution Platform

XeanVI is a localized, AI-powered execution engine and trading automation platform built for retail day traders who want discipline, structure, and rule-based execution instead of emotion-driven trading.

The platform acts as a trading command center. Users define their own Trading Playbook, connect a supported broker API, test in paper mode, and use XeanVI to scan, validate, and execute managed trades based on predefined rules.

> Important: XeanVI is a trading automation and decision-support platform. It does not guarantee profits, eliminate risk, or replace the user’s responsibility to understand trading risk.

---

## Core Features

- User account signup and login
- Secure forgot-password and reset-password flow
- Brevo transactional password reset emails
- Waitlist capture with Brevo contact sync
- Trading dashboard for authenticated users
- Trading Playbook page and strategy workflow
- Alpaca broker OAuth connection
- Paper/live trading mode support
- Bankroll and risk parameter management
- Market scanner and watchlist tools
- WebSocket watchlist updates
- Stripe subscription checkout
- Stripe webhook handling for PRO subscriptions
- Gemini-powered catalyst analysis
- Finnhub-powered market/news data
- SQLite database storage through SQLAlchemy
- CSRF protection through Flask-WTF
- Rate limiting through Flask-Limiter
- Secure password hashing through Werkzeug

---

## Tech Stack

- Python 3.11+
- Flask
- Flask-Login
- Flask-SQLAlchemy
- Flask-WTF / CSRFProtect
- Flask-Limiter
- Flask-Sock
- SQLite
- Redis
- Stripe API
- Brevo API
- Alpaca OAuth/API
- Gemini API
- Finnhub API
- Gunicorn/Nginx recommended for production

---

## Main Project Structure

```txt
trader/
├── app.py                         # Main Flask application and routes
├── config.py                      # Environment-based configuration
├── models.py                      # SQLAlchemy models
├── db.py                          # Trading database helpers
├── scanner.py                     # Market scanner logic
├── broker.py                      # Broker order helpers
├── execution.py                   # Trading execution engine
├── onboarding.py                  # Broker/account onboarding helpers
├── watchlist.py                   # Watchlist manager
├── explainability.py              # AI trade thesis generation
├── execution_guard.py             # Trade safety validation/audit helpers
├── requirements.txt               # Python dependencies
├── templates/
│   ├── login.html                 # Login page
│   ├── signup.html                # Signup page
│   ├── forgot_password.html       # Forgot-password request page
│   ├── reset_password.html        # New password form
│   ├── dashboard.html             # Trading dashboard
│   ├── settings.html              # User settings/risk controls
│   ├── onboarding.html            # User onboarding
│   ├── upgrade.html               # Stripe upgrade page
│   ├── playbook.html              # Public Trading Playbook page
│   ├── features.html              # Public features page
│   ├── broker_integration.html    # Broker integration page
│   ├── privacy.html               # Privacy policy
│   ├── terms.html                 # Terms page
│   └── nav.html                   # Shared navigation
└── static/
    └── style.css                  # Main styling
```

---

## Authentication Flow

### Signup

Users create an account through `/signup`.

The app stores:

- Email
- Full name
- Hashed password
- Default subscription status
- Default trading mode and risk settings

Passwords are hashed with Werkzeug using `pbkdf2:sha256`.

### Login

Users log in through `/login`.

The login route validates the email and password hash, then uses Flask-Login to create the user session.

### Logout

Authenticated users can log out through `/logout`.

---

## Forgot Password / Reset Password Flow

The forgot-password system uses signed, expiring reset tokens instead of storing reset tokens in the database.

### User Flow

1. User clicks **Forgot password?** on `/login`.
2. User is sent to `/forgot-password`.
3. User enters their email address.
4. If the email exists, the app generates a secure reset URL.
5. The app sends a Brevo transactional email using a saved Brevo template.
6. User clicks the reset link.
7. User lands on `/reset-password/<token>`.
8. User enters and confirms a new password.
9. The password hash is updated.
10. The token becomes invalid because the password hash fingerprint changes.
11. User is redirected back to `/login`.

### Security Notes

- The forgot-password route does not reveal whether an email exists.
- Reset links expire based on `PASSWORD_RESET_TOKEN_MAX_AGE_SECONDS`.
- Reset tokens include a fingerprint of the current password hash.
- Once the user changes their password, old reset links stop working.
- Passwords must be at least 8 characters.
- CSRF protection is active on the forms.
- Rate limiting is applied to password reset routes.

### Password Reset Routes

```txt
GET  /forgot-password
POST /forgot-password
GET  /reset_password
POST /reset_password
GET  /reset-password/<token>
POST /reset-password/<token>
```

`/reset_password` is kept as a backward-compatible alias so old hardcoded links still work.

---

## Brevo Password Reset Email Setup

The password reset flow uses Brevo transactional email.

Create a transactional email template in Brevo with this name:

```txt
XeanVI Password Reset
```

Recommended subject:

```txt
Reset your XeanVI password
```

Recommended sender:

```txt
XeanVI Security <support@xeanvi.com>
```

The Brevo template should use these dynamic params:

```txt
{{params.first_name}}
{{params.reset_url}}
{{params.expires_minutes}}
{{params.support_email}}
```

After creating the template, copy the numeric template ID from Brevo and add it to your environment file:

```env
BREVO_RESET_PASSWORD_TEMPLATE_ID=12
```

Replace `12` with your actual Brevo template ID.

---

## Required Environment Variables

The production app loads environment variables from:

```txt
/etc/xeanvi/xeanvi.env
```

For local development, the app falls back to:

```txt
.env
```

Never commit your real `.env` file or production secrets to GitHub.

### Core App Settings

```env
SECRET_KEY=replace_with_a_long_random_secret
TOKEN_ENCRYPTION_KEY=replace_with_a_long_random_secret
FLASK_DEBUG=0
HOST=0.0.0.0
PORT=5000
APP_BASE_URL=https://xeanvi.com
```

### Session / CSRF Settings

```env
SESSION_COOKIE_DOMAIN=.xeanvi.com
SESSION_COOKIE_SAMESITE=Lax
SESSION_COOKIE_SECURE=1
WTF_CSRF_TRUSTED_ORIGINS=https://xeanvi.com,https://www.xeanvi.com
```

### Alpaca Settings

```env
ALPACA_CLIENT_ID=your_alpaca_client_id
ALPACA_CLIENT_SECRET=your_alpaca_client_secret
ALPACA_REDIRECT_URI=https://xeanvi.com/alpaca/callback
ALPACA_API_KEY=your_optional_alpaca_api_key
ALPACA_API_SECRET=your_optional_alpaca_api_secret
```

### Finnhub / Gemini Settings

```env
FINNHUB_API_KEY=your_finnhub_api_key
GEMINI_API_KEY=your_gemini_api_key
GEMINI_MODEL=gemini-2.5-flash
```

### Stripe Settings

```env
STRIPE_PUBLIC_KEY=pk_live_or_test_key
STRIPE_SECRET_KEY=sk_live_or_test_key
STRIPE_WEBHOOK_SECRET=whsec_your_webhook_secret
STRIPE_PRICE_ID_MONTHLY=price_monthly_id
STRIPE_PRICE_ID_ANNUAL=price_annual_id
```

### Brevo Settings

```env
BREVO_API_KEY=your_brevo_api_key
BREVO_LIST_ID=5
BREVO_RESET_PASSWORD_TEMPLATE_ID=your_template_id_number
BREVO_SENDER_EMAIL=support@xeanvi.com
BREVO_SENDER_NAME=XeanVI Security
PASSWORD_RESET_TOKEN_MAX_AGE_SECONDS=3600
```

### Scanner / Risk Settings

```env
SCAN_CANDIDATE_LIMIT=20
WATCHLIST_SIZE=3
MAX_BUY_SHARES=999
DEFAULT_RISK_CAPITAL=300
CURRENT_BANKROLL=300.0
KELLY_FRACTION=0.25
MAX_PORTFOLIO_HEAT=0.06
VIX_PENALTY_MULTIPLIER=0.5
MAX_DOLLAR_LOSS_PER_TRADE=5
MAX_FAILED_TRADES_PER_DAY=2
WATCHLIST_PUSH_SECONDS=4
ORDER_STATUS_POLL_SECONDS=8
MIN_SCORE_TO_EXECUTE=25
```

### Market Filter Settings

```env
MIN_CATALYST_SCORE=2
NO_BUY_BEFORE_ET=09:45
OPENING_RANGE_START_ET=09:30
OPENING_RANGE_END_ET=09:45
MAX_SPREAD_PCT=0.003
MAX_ENTRY_EXTENSION_PCT=0.01
OR_BREAKOUT_BUFFER_PCT=0.0015
PULLBACK_MAX_RETRACE_PCT=0.45
ENTRY_ORDER_TIMEOUT_SECONDS=15
ENTRY_ORDER_POLL_SECONDS=1
TARGET2_TRAILING_STOP_PCT=5
MARKET_INTERNALS_BLOCK_ENABLED=1
MARKET_INTERNALS_TICK_SYMBOL=TICK
MARKET_INTERNALS_ADD_SYMBOL=ADD
CRYPTO_SCAN_ENABLED=1
CRYPTO_SYMBOLS=BTC/USD,ETH/USD,SOL/USD,XRP/USD,DOGE/USD
MIN_PREMARKET_GAP_PCT=2.0
MIN_PREMARKET_DOLLAR_VOL=2000000
MIN_SECTOR_SYMPATHY_SCORE=1
MIN_RVOL=1.5
MAX_FLOAT=2000000000
A_PLUS_SCORE=34
A_SCORE=30
LUNCH_BLOCK_START=11:30
LUNCH_BLOCK_END=13:00
VA_PERCENT=0.70
ATR_STOP_MULT=2.0
RS_SECTOR_MULT=1.5
VIX_SYMBOL=VIXY
VIX_CIRCUIT_BREAKER_PCT=5.0
```

### Database Fallback Setting

```env
DB_FALLBACK_DIR=/tmp
```

If SQLite cannot write to the default database path, the app can fall back to this directory.

---

## Local Development Setup

### 1. Clone the Repository

```bash
git clone https://github.com/TroyS0326/trader.git
cd trader
```

### 2. Create a Virtual Environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

On Windows PowerShell:

```powershell
python -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

### 3. Install Requirements

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

### 4. Create a Local `.env`

```bash
cp .env.example .env
nano .env
```

If `.env.example` does not exist yet, create `.env` manually using the environment variable sections above.

### 5. Run the App

```bash
python app.py
```

Open:

```txt
http://127.0.0.1:5000
```

---

## Production Setup Notes

Recommended production stack:

- Ubuntu VPS
- Python virtual environment
- Gunicorn
- Nginx reverse proxy
- Systemd service
- Redis
- HTTPS through Certbot / Let’s Encrypt

Production environment file:

```txt
/etc/xeanvi/xeanvi.env
```

After changing environment variables, restart the app service.

Example:

```bash
sudo systemctl restart xeanvi
```

Check status:

```bash
sudo systemctl status xeanvi --no-pager
```

View logs:

```bash
journalctl -u xeanvi -n 100 --no-pager
```

---

## Nginx WebSocket Proxy

The watchlist uses a websocket route. If running behind Nginx, include websocket upgrade headers.

Example:

```nginx
location /ws/ {
    proxy_pass http://127.0.0.1:5000;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
}
```

---

## Stripe Setup

The app supports subscription checkout through Stripe.

Important routes:

```txt
/api/create-checkout-session
/checkout-redirect
/api/stripe-webhook
```

The webhook should point to:

```txt
https://xeanvi.com/api/stripe-webhook
```

The app currently handles these Stripe events:

```txt
checkout.session.completed
customer.subscription.deleted
```

When a checkout session completes, the user is upgraded to `pro`.

When a subscription is deleted, the user is downgraded to `free`.

---

## Alpaca Broker Setup

Important routes:

```txt
/alpaca/login
/alpaca/callback
/v1/oauth/callback
/alpaca/logout
```

Recommended Alpaca redirect URI:

```txt
https://xeanvi.com/alpaca/callback
```

The `/v1/oauth/callback` route exists as an alias for compatibility.

---

## Important Public Pages

```txt
/                         Home or waitlist depending on access/session
/features                 Features page
/playbook                 Trading Playbook page
/broker-integration       Broker integration page
/pricing                  Pricing page
/signup                   Account creation
/login                    Login
/forgot-password          Password reset request
/terms                    Terms
/privacy                  Privacy
/faq                      FAQ
/sitemap.xml              XML sitemap
/robots.txt               Robots file
```

---

## Important Authenticated Pages

```txt
/dashboard                Main trading dashboard
/onboarding               User onboarding
/settings                 User settings and risk controls
/upgrade                  Subscription upgrade page
/learn                    Learning area
/transparency             Transparency/performance area
```

---

## Testing Checklist

After each deployment, test these flows:

### Basic Site

```txt
/ loads correctly
/features loads correctly
/playbook loads correctly
/login loads correctly
/signup loads correctly
```

### Auth

```txt
Create a test account
Log out
Log back in
Visit dashboard
Visit settings
Log out again
```

### Forgot Password

```txt
Open /forgot-password
Submit a real test account email
Confirm Brevo sends the reset email
Click reset link
Enter a new password
Confirm redirect to /login
Log in with the new password
Confirm old reset link no longer works after password change
```

### Brevo

```txt
Confirm BREVO_API_KEY is present
Confirm BREVO_RESET_PASSWORD_TEMPLATE_ID is present
Confirm sender email is verified in Brevo
Confirm dynamic params render correctly
Confirm reset link points to https://xeanvi.com/reset-password/<token>
```

### Stripe

```txt
Confirm checkout session creates correctly
Confirm success redirects correctly
Confirm webhook receives checkout.session.completed
Confirm user subscription_status changes to pro
```

### Alpaca

```txt
Start broker connection flow
Confirm OAuth state validation works
Confirm Alpaca callback stores account access correctly
Confirm paper/live account detection works
```

---

## Code Quality Checks

Before deploying, run:

```bash
python -m py_compile app.py config.py models.py
```

Optional dependency check:

```bash
pip check
```

---

## Security Rules

- Never commit `.env` files.
- Never commit API keys.
- Never expose Stripe secret keys in frontend code.
- Never expose Brevo API keys in frontend code.
- Use HTTPS in production.
- Keep `SESSION_COOKIE_SECURE=1` in production.
- Keep CSRF protection enabled.
- Keep rate limiting enabled on auth routes.
- Use strong random values for `SECRET_KEY` and `TOKEN_ENCRYPTION_KEY`.
- Rotate API keys immediately if they are accidentally exposed.

Generate strong secrets with:

```bash
python - <<'PY'
import secrets
print(secrets.token_urlsafe(64))
PY
```

---

## Troubleshooting

### Forgot password email does not send

Check:

```txt
BREVO_API_KEY
BREVO_RESET_PASSWORD_TEMPLATE_ID
BREVO_SENDER_EMAIL
Brevo sender verification
Brevo transactional email logs
App logs through journalctl
```

Useful command:

```bash
journalctl -u xeanvi -n 100 --no-pager
```

### Reset link says invalid or expired

Possible causes:

```txt
The link is older than PASSWORD_RESET_TOKEN_MAX_AGE_SECONDS
The user already changed their password
SECRET_KEY changed after the email was sent
The URL was copied incorrectly
APP_BASE_URL is wrong
```

### Login fails after password reset

Check:

```txt
The password was entered correctly
The user exists in the same database being used by the app
The app is not falling back to /tmp/veteran_trades.db unexpectedly
```

### SQLite permission issues

Check file ownership and write permissions for the app directory.

If needed, set:

```env
DB_FALLBACK_DIR=/tmp
```

### Websocket errors behind Nginx

Confirm the `/ws/` Nginx block includes:

```nginx
proxy_http_version 1.1;
proxy_set_header Upgrade $http_upgrade;
proxy_set_header Connection "upgrade";
```

---

## Deployment Reminder

When changing code or environment variables:

```bash
git pull origin main
source .venv/bin/activate
pip install -r requirements.txt
python -m py_compile app.py config.py
sudo systemctl restart xeanvi
sudo systemctl status xeanvi --no-pager
```

---

## Disclaimer

XeanVI is software for trading workflow automation, scanning, execution assistance, and risk-rule enforcement. Trading involves substantial risk. Past performance, scanner rankings, AI-generated analysis, and automated execution rules do not guarantee future results.
