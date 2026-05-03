import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent

# Check the VPS production path first, then fallback to local directory .env
PROD_ENV_PATH = '/etc/xeanvi/xeanvi.env'
if os.path.exists(PROD_ENV_PATH):
    load_dotenv(PROD_ENV_PATH)
else:
    load_dotenv(BASE_DIR / '.env')


def require_env(var_name: str) -> str:
    value = os.getenv(var_name)
    if not value:
        raise ValueError(f"CRITICAL SECURITY ERROR: Environment variable '{var_name}' is missing.")
    return value


APP_TITLE = 'XeanVI'
SECRET_KEY = require_env('SECRET_KEY')
TOKEN_ENCRYPTION_KEY = require_env('TOKEN_ENCRYPTION_KEY')
DEBUG = os.getenv('FLASK_DEBUG', '0') == '1'
HOST = os.getenv('HOST', '0.0.0.0')
PORT = int(os.getenv('PORT', '5000'))


SESSION_COOKIE_DOMAIN = os.getenv('SESSION_COOKIE_DOMAIN', '').strip() or None
SESSION_COOKIE_SAMESITE = os.getenv('SESSION_COOKIE_SAMESITE', 'Lax').strip() or 'Lax'
SESSION_COOKIE_SECURE = os.getenv('SESSION_COOKIE_SECURE', '1') == '1'
WTF_CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        'WTF_CSRF_TRUSTED_ORIGINS',
        'https://xeanvi.com,https://www.xeanvi.com',
    ).split(',')
    if origin.strip()
]

ALPACA_CLIENT_ID = require_env('ALPACA_CLIENT_ID')
ALPACA_CLIENT_SECRET = require_env('ALPACA_CLIENT_SECRET')
ALPACA_REDIRECT_URI = require_env('ALPACA_REDIRECT_URI').strip()
ALPACA_API_KEY = os.getenv('ALPACA_API_KEY', '').strip()
ALPACA_API_SECRET = os.getenv('ALPACA_API_SECRET', '').strip()
ALPACA_PAPER_BASE = 'https://paper-api.alpaca.markets'
ALPACA_DATA_BASE = 'https://data.alpaca.markets'
FINNHUB_API_KEY = require_env('FINNHUB_API_KEY').strip()
GEMINI_API_KEY = require_env('GEMINI_API_KEY').strip()
GEMINI_MODEL = os.getenv('GEMINI_MODEL', 'gemini-2.5-flash').strip() or 'gemini-2.5-flash'
STRIPE_PUBLIC_KEY = os.getenv('STRIPE_PUBLIC_KEY')
STRIPE_SECRET_KEY = os.getenv('STRIPE_SECRET_KEY')
STRIPE_WEBHOOK_SECRET = os.getenv('STRIPE_WEBHOOK_SECRET')
STRIPE_PRICE_ID_MONTHLY = os.getenv('STRIPE_PRICE_ID_MONTHLY')
STRIPE_PRICE_ID_ANNUAL = os.getenv('STRIPE_PRICE_ID_ANNUAL')
BREVO_API_KEY = os.getenv('BREVO_API_KEY')
BREVO_LIST_ID = int(os.getenv('BREVO_LIST_ID', '5'))

# Password reset email settings
APP_BASE_URL = os.getenv('APP_BASE_URL', 'https://xeanvi.com').rstrip('/')
BREVO_RESET_PASSWORD_TEMPLATE_ID = os.getenv('BREVO_RESET_PASSWORD_TEMPLATE_ID')
BREVO_SENDER_EMAIL = os.getenv('BREVO_SENDER_EMAIL', 'support@xeanvi.com')
BREVO_SENDER_NAME = os.getenv('BREVO_SENDER_NAME', 'XeanVI Security')
PASSWORD_RESET_TOKEN_MAX_AGE_SECONDS = int(os.getenv('PASSWORD_RESET_TOKEN_MAX_AGE_SECONDS', '3600'))

DB_PATH = str(BASE_DIR / 'veteran_trades.db')
SCAN_CANDIDATE_LIMIT = int(os.getenv('SCAN_CANDIDATE_LIMIT', '20'))
WATCHLIST_SIZE = int(os.getenv('WATCHLIST_SIZE', '3'))
MAX_BUY_SHARES = int(os.getenv('MAX_BUY_SHARES', '999'))
DEFAULT_RISK_CAPITAL = float(os.getenv('DEFAULT_RISK_CAPITAL', '300'))
CURRENT_BANKROLL = float(os.getenv('CURRENT_BANKROLL', '300.0'))

# --- Dynamic Risk Sizing Parameters (Replaces RISK_PCT_PER_TRADE) ---
RISK_PCT_PER_TRADE = 0.02
KELLY_FRACTION = float(os.getenv('KELLY_FRACTION', '0.25'))  # We will risk 25% of the mathematically optimal Full Kelly size
MAX_PORTFOLIO_HEAT = float(os.getenv('MAX_PORTFOLIO_HEAT', '0.06'))  # Hard cap single-trade risk at 6% of portfolio equity
VIX_PENALTY_MULTIPLIER = float(os.getenv('VIX_PENALTY_MULTIPLIER', '0.5'))  # Cut Kelly sizing in half if VIX circuit breaker triggers

# Kept as a fallback for any legacy references.
MAX_DOLLAR_LOSS_PER_TRADE = float(os.getenv('MAX_DOLLAR_LOSS_PER_TRADE', '5'))
MAX_FAILED_TRADES_PER_DAY = int(os.getenv('MAX_FAILED_TRADES_PER_DAY', '2'))
WATCHLIST_PUSH_SECONDS = float(os.getenv('WATCHLIST_PUSH_SECONDS', '4'))
ORDER_STATUS_POLL_SECONDS = float(os.getenv('ORDER_STATUS_POLL_SECONDS', '8'))
MIN_SCORE_TO_EXECUTE = int(os.getenv('MIN_SCORE_TO_EXECUTE', '25'))

# --- CALIBRATED ENGINE SETTINGS (LOOSENED FOR MORE ACTION) ---
MIN_CATALYST_SCORE = int(os.getenv('MIN_CATALYST_SCORE', '2'))
NO_BUY_BEFORE_ET = os.getenv('NO_BUY_BEFORE_ET', '09:45').strip() or '09:45'
OPENING_RANGE_START_ET = os.getenv('OPENING_RANGE_START_ET', '09:30').strip() or '09:30'
OPENING_RANGE_END_ET = os.getenv('OPENING_RANGE_END_ET', '09:45').strip() or '09:45'
MAX_SPREAD_PCT = float(os.getenv('MAX_SPREAD_PCT', '0.003'))

MAX_ENTRY_EXTENSION_PCT = float(os.getenv('MAX_ENTRY_EXTENSION_PCT', '0.01'))
OR_BREAKOUT_BUFFER_PCT = float(os.getenv('OR_BREAKOUT_BUFFER_PCT', '0.0015'))
PULLBACK_MAX_RETRACE_PCT = float(os.getenv('PULLBACK_MAX_RETRACE_PCT', '0.45'))
ENTRY_ORDER_TIMEOUT_SECONDS = float(os.getenv('ENTRY_ORDER_TIMEOUT_SECONDS', '15'))
ENTRY_ORDER_POLL_SECONDS = float(os.getenv('ENTRY_ORDER_POLL_SECONDS', '1'))
TARGET2_TRAILING_STOP_PCT = float(os.getenv('TARGET2_TRAILING_STOP_PCT', '5'))
MARKET_INTERNALS_BLOCK_ENABLED = os.getenv('MARKET_INTERNALS_BLOCK_ENABLED', '1') == '1'
MARKET_INTERNALS_TICK_SYMBOL = os.getenv('MARKET_INTERNALS_TICK_SYMBOL', 'TICK').strip().upper() or 'TICK'
MARKET_INTERNALS_ADD_SYMBOL = os.getenv('MARKET_INTERNALS_ADD_SYMBOL', 'ADD').strip().upper() or 'ADD'
CRYPTO_SCAN_ENABLED = os.getenv('CRYPTO_SCAN_ENABLED', '1') == '1'
CRYPTO_SYMBOLS = [s.strip().upper() for s in os.getenv('CRYPTO_SYMBOLS', 'BTC/USD,ETH/USD,SOL/USD,XRP/USD,DOGE/USD').split(',') if s.strip()]

# --- BROADER MARKET CAPS AND GAPS ---
MIN_PREMARKET_GAP_PCT = float(os.getenv('MIN_PREMARKET_GAP_PCT', '2.0'))
MIN_PREMARKET_DOLLAR_VOL = float(os.getenv('MIN_PREMARKET_DOLLAR_VOL', '2000000'))
MIN_SECTOR_SYMPATHY_SCORE = int(os.getenv('MIN_SECTOR_SYMPATHY_SCORE', '1'))
MIN_RVOL = float(os.getenv('MIN_RVOL', '1.5'))
MAX_FLOAT = int(os.getenv('MAX_FLOAT', '2000000000'))

A_PLUS_SCORE = int(os.getenv('A_PLUS_SCORE', '34'))
A_SCORE = int(os.getenv('A_SCORE', '30'))
TIMEZONE_LABEL = 'America/New_York'
LUNCH_BLOCK_START = os.getenv('LUNCH_BLOCK_START', '11:30').strip() or '11:30'
LUNCH_BLOCK_END = os.getenv('LUNCH_BLOCK_END', '13:00').strip() or '13:00'
VA_PERCENT = float(os.getenv('VA_PERCENT', '0.70'))
ATR_STOP_MULT = float(os.getenv('ATR_STOP_MULT', '2.0'))
RS_SECTOR_MULT = float(os.getenv('RS_SECTOR_MULT', '1.5'))
VIX_SYMBOL = os.getenv('VIX_SYMBOL', 'VIXY').strip().upper() or 'VIXY'
VIX_CIRCUIT_BREAKER_PCT = float(os.getenv('VIX_CIRCUIT_BREAKER_PCT', '5.0'))
