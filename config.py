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
FLASK_ENV = os.getenv('FLASK_ENV', '').strip().lower()
IS_TESTING = FLASK_ENV == 'testing' or os.getenv('TESTING', '0') == '1' or bool(os.getenv('PYTEST_CURRENT_TEST'))
IS_DEVELOPMENT = FLASK_ENV in {'development', 'local'} or DEBUG
IS_PRODUCTION = FLASK_ENV == 'production' or (not DEBUG and not IS_TESTING and not IS_DEVELOPMENT)
HOST = os.getenv('HOST', '0.0.0.0')
PORT = int(os.getenv('PORT', '5000'))
STRICT_PRODUCTION_SCANNER = os.getenv('STRICT_PRODUCTION_SCANNER', '1') == '1'

REDIS_URL = os.getenv('REDIS_URL', 'redis://localhost:6379/0').strip() or 'redis://localhost:6379/0'
RATELIMIT_STORAGE_URI = os.getenv('RATELIMIT_STORAGE_URI', REDIS_URL).strip() or REDIS_URL


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
ALPACA_TRADING_BASE = os.getenv('ALPACA_TRADING_BASE', ALPACA_PAPER_BASE).strip() or ALPACA_PAPER_BASE
ALPACA_ASSETS_BASE = os.getenv('ALPACA_ASSETS_BASE', ALPACA_TRADING_BASE).strip() or ALPACA_TRADING_BASE
ALPACA_DATA_BASE = 'https://data.alpaca.markets'
ALPACA_DATA_FEED = os.getenv('ALPACA_DATA_FEED', 'iex').strip().lower()
if ALPACA_DATA_FEED not in {'iex', 'sip'}:
    ALPACA_DATA_FEED = 'iex'
FINNHUB_API_KEY = require_env('FINNHUB_API_KEY').strip()
GEMINI_API_KEY = require_env('GEMINI_API_KEY').strip()
GEMINI_MODEL = os.getenv('GEMINI_MODEL', 'gemini-2.5-flash').strip() or 'gemini-2.5-flash'
STRIPE_PUBLIC_KEY = os.getenv('STRIPE_PUBLIC_KEY')
STRIPE_SECRET_KEY = os.getenv('STRIPE_SECRET_KEY')
STRIPE_WEBHOOK_SECRET = os.getenv('STRIPE_WEBHOOK_SECRET')
STRIPE_PRICE_ID_MONTHLY = os.getenv('STRIPE_PRICE_ID_MONTHLY')
STRIPE_PRICE_ID_ANNUAL = os.getenv('STRIPE_PRICE_ID_ANNUAL')
STRIPE_CUSTOMER_PORTAL_RETURN_PATH = os.getenv('STRIPE_CUSTOMER_PORTAL_RETURN_PATH', '/billing')
LAUNCH_PROMO_ENABLED = os.getenv('LAUNCH_PROMO_ENABLED', '0') == '1'
LAUNCH_PROMO_STRIPE_COUPON_ID = os.getenv('LAUNCH_PROMO_STRIPE_COUPON_ID', '').strip()

BREVO_API_KEY = os.getenv('BREVO_API_KEY')
BREVO_LIST_ID = int(os.getenv('BREVO_LIST_ID', '5'))
BREVO_SIGNUP_LIST_ID = int(os.getenv('BREVO_SIGNUP_LIST_ID', '0'))
BREVO_SIGNUP_SYNC_OPTIONAL = os.getenv('BREVO_SIGNUP_SYNC_OPTIONAL', '0') == '1'
BREVO_WELCOME_TEMPLATE_ENABLED = os.getenv('BREVO_WELCOME_TEMPLATE_ENABLED', '1') == '1'
META_PIXEL_ID = os.getenv('META_PIXEL_ID', '').strip()
GOOGLE_ADS_ID = os.getenv('GOOGLE_ADS_ID', '').strip()
GOOGLE_ADS_CONVERSION_SIGNUP_LABEL = os.getenv('GOOGLE_ADS_CONVERSION_SIGNUP_LABEL', '').strip()
GOOGLE_ADS_CONVERSION_CHECKOUT_LABEL = os.getenv('GOOGLE_ADS_CONVERSION_CHECKOUT_LABEL', '').strip()
GOOGLE_ADS_CONVERSION_PURCHASE_LABEL = os.getenv('GOOGLE_ADS_CONVERSION_PURCHASE_LABEL', '').strip()
GOOGLE_ADS_CONVERSION_CONTACT_EMAIL_LABEL = os.getenv('GOOGLE_ADS_CONVERSION_CONTACT_EMAIL_LABEL', '').strip()
GOOGLE_ADS_CONVERSION_CONTACT_PHONE_LABEL = os.getenv('GOOGLE_ADS_CONVERSION_CONTACT_PHONE_LABEL', '').strip()

# Password reset email settings
APP_BASE_URL = os.getenv('APP_BASE_URL', 'https://xeanvi.com').rstrip('/')
BREVO_RESET_PASSWORD_TEMPLATE_ID = os.getenv('BREVO_RESET_PASSWORD_TEMPLATE_ID')
BREVO_SENDER_EMAIL = os.getenv('BREVO_SENDER_EMAIL', 'support@xeanvi.com')
BREVO_SENDER_NAME = os.getenv('BREVO_SENDER_NAME', 'XeanVI Security')

BREVO_DAILY_REPORT_TEMPLATE_ID = os.getenv('BREVO_DAILY_REPORT_TEMPLATE_ID')

ADMIN_DAILY_DIGEST_ENABLED = os.getenv('ADMIN_DAILY_DIGEST_ENABLED', '0') == '1'
ADMIN_DAILY_DIGEST_TEMPLATE_ID = os.getenv('ADMIN_DAILY_DIGEST_TEMPLATE_ID', '').strip()
ADMIN_DAILY_DIGEST_RECIPIENT = os.getenv('ADMIN_DAILY_DIGEST_RECIPIENT', '').strip()
ADMIN_DAILY_DIGEST_DRY_RUN = os.getenv('ADMIN_DAILY_DIGEST_DRY_RUN', '0') == '1'
ADMIN_DAILY_DIGEST_SEND_HOUR_ET = int(os.getenv('ADMIN_DAILY_DIGEST_SEND_HOUR_ET', '18'))
ADMIN_DAILY_DIGEST_SKIP_WEEKENDS = os.getenv('ADMIN_DAILY_DIGEST_SKIP_WEEKENDS', '0') == '1'
DAILY_REPORT_EMAIL_ENABLED = os.getenv('DAILY_REPORT_EMAIL_ENABLED', '0') == '1'
DAILY_REPORT_SEND_TO_FREE_USERS = os.getenv('DAILY_REPORT_SEND_TO_FREE_USERS', '0') == '1'
DAILY_REPORT_SEND_TO_PRO_USERS = os.getenv('DAILY_REPORT_SEND_TO_PRO_USERS', '1') == '1'
DAILY_REPORT_SKIP_WEEKENDS = os.getenv('DAILY_REPORT_SKIP_WEEKENDS', '1') == '1'
DAILY_REPORT_REQUIRE_ACTIVITY = os.getenv('DAILY_REPORT_REQUIRE_ACTIVITY', '0') == '1'
DAILY_REPORT_TEST_RECIPIENT = os.getenv('DAILY_REPORT_TEST_RECIPIENT', '').strip()
DAILY_REPORT_DRY_RUN = os.getenv('DAILY_REPORT_DRY_RUN', '0') == '1'
PASSWORD_RESET_TOKEN_MAX_AGE_SECONDS = int(os.getenv('PASSWORD_RESET_TOKEN_MAX_AGE_SECONDS', '3600'))
BLOG_IMAGE_UPLOAD_DIR = os.getenv(
    "BLOG_IMAGE_UPLOAD_DIR",
    os.path.join(BASE_DIR, "static", "blog", "uploads")
)
BLOG_IMAGE_URL_PREFIX = os.getenv(
    "BLOG_IMAGE_URL_PREFIX",
    "/static/blog/uploads"
)
BLOG_IMAGE_MAX_BYTES = int(os.getenv("BLOG_IMAGE_MAX_BYTES", str(3 * 1024 * 1024)))


def _missing_values(values: dict[str, object]) -> list[str]:
    missing: list[str] = []
    for key, value in values.items():
        if value is None:
            missing.append(key)
            continue
        if isinstance(value, str) and not value.strip():
            missing.append(key)
            continue
        if isinstance(value, int) and value <= 0:
            missing.append(key)
    return missing


def validate_stripe_config() -> list[str]:
    return _missing_values({
        'STRIPE_PUBLIC_KEY': STRIPE_PUBLIC_KEY,
        'STRIPE_SECRET_KEY': STRIPE_SECRET_KEY,
        'STRIPE_WEBHOOK_SECRET': STRIPE_WEBHOOK_SECRET,
        'STRIPE_PRICE_ID_MONTHLY': STRIPE_PRICE_ID_MONTHLY,
        'STRIPE_PRICE_ID_ANNUAL': STRIPE_PRICE_ID_ANNUAL,
    })


def validate_brevo_config() -> list[str]:
    return _missing_values({
        'BREVO_API_KEY': BREVO_API_KEY,
        'BREVO_LIST_ID': BREVO_LIST_ID,
        'BREVO_SIGNUP_LIST_ID': BREVO_SIGNUP_LIST_ID,
        'BREVO_RESET_PASSWORD_TEMPLATE_ID': BREVO_RESET_PASSWORD_TEMPLATE_ID,
        'BREVO_SENDER_EMAIL': BREVO_SENDER_EMAIL,
    })

def validate_admin_daily_digest_config() -> list[str]:
    missing = _missing_values({
        'BREVO_API_KEY': BREVO_API_KEY,
        'BREVO_SENDER_EMAIL': BREVO_SENDER_EMAIL,
    })
    if not (ADMIN_DAILY_DIGEST_RECIPIENT or os.getenv('ADMIN_EMAIL', '').strip()):
        missing.append('ADMIN_EMAIL_OR_ADMIN_DAILY_DIGEST_RECIPIENT')
    if ADMIN_DAILY_DIGEST_TEMPLATE_ID and not ADMIN_DAILY_DIGEST_TEMPLATE_ID.isdigit():
        missing.append('ADMIN_DAILY_DIGEST_TEMPLATE_ID')
    return missing

DB_PATH = os.getenv('DB_PATH') or str(BASE_DIR / 'app_local.db')
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

PROD_DB_ERROR = "Production must use Postgres; veteran_trades.db is decommissioned."


def normalize_database_url(raw_url: str) -> str:
    url = (raw_url or '').strip()
    if url.startswith('postgres://'):
        return 'postgresql+psycopg://' + url[len('postgres://'):]
    if url.startswith('postgresql://'):
        return 'postgresql+psycopg://' + url[len('postgresql://'):]
    return url


def build_database_uri() -> str:
    uri = normalize_database_url(DATABASE_URL)

    if IS_PRODUCTION:
        if not uri:
            raise ValueError(PROD_DB_ERROR)
        lowered = uri.lower()
        if not uri.startswith("postgresql+psycopg://"):
            raise ValueError(PROD_DB_ERROR)
        if lowered.startswith('sqlite'):
            raise ValueError(PROD_DB_ERROR)
        if ':memory:' in lowered:
            raise ValueError(PROD_DB_ERROR)
        if 'veteran_trades.db' in lowered:
            raise ValueError(PROD_DB_ERROR)
        return uri

    if not uri:
        return f"sqlite:///{os.path.abspath(DB_PATH)}"

    return uri


SQLALCHEMY_DATABASE_URI = build_database_uri()
SQLALCHEMY_ENGINE_OPTIONS = {
    "pool_pre_ping": True,
    "pool_recycle": 300,
}
ALLOW_DB_FALLBACK = os.getenv('ALLOW_DB_FALLBACK', '0') == '1'
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
RECENT_SCAN_WINDOW_MINUTES_15 = int(os.getenv('RECENT_SCAN_WINDOW_MINUTES_15', '15'))
RECENT_SCAN_WINDOW_MINUTES_60 = int(os.getenv('RECENT_SCAN_WINDOW_MINUTES_60', '60'))

# --- CALIBRATED ENGINE SETTINGS (LOOSENED FOR MORE ACTION) ---
MIN_CATALYST_SCORE = int(os.getenv('MIN_CATALYST_SCORE', '2'))
DATA_FRESHNESS_MAX_AGE_SECONDS = float(os.getenv('DATA_FRESHNESS_MAX_AGE_SECONDS', '1.5'))
NO_BUY_BEFORE_ET = os.getenv('NO_BUY_BEFORE_ET', '09:45').strip() or '09:45'
DYNAMIC_ORB_NORMAL_START_ET = os.getenv('DYNAMIC_ORB_NORMAL_START_ET', '09:35').strip() or '09:35'
DYNAMIC_ORB_DELAYED_START_ET = os.getenv('DYNAMIC_ORB_DELAYED_START_ET', '09:45').strip() or '09:45'
DYNAMIC_ORB_EXTREME_START_ET = os.getenv('DYNAMIC_ORB_EXTREME_START_ET', '10:00').strip() or '10:00'

DYNAMIC_ORB_RVOL_DELAY_THRESHOLD = float(os.getenv('DYNAMIC_ORB_RVOL_DELAY_THRESHOLD', '3.0'))
DYNAMIC_ORB_RVOL_EXTREME_THRESHOLD = float(os.getenv('DYNAMIC_ORB_RVOL_EXTREME_THRESHOLD', '5.0'))
DYNAMIC_ORB_ATR_EXPANSION_DELAY_THRESHOLD = float(os.getenv('DYNAMIC_ORB_ATR_EXPANSION_DELAY_THRESHOLD', '1.5'))
DYNAMIC_ORB_ATR_EXPANSION_EXTREME_THRESHOLD = float(os.getenv('DYNAMIC_ORB_ATR_EXPANSION_EXTREME_THRESHOLD', '2.0'))

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

# Momentum breakout mode
MOMENTUM_BREAKOUT_MODE_ENABLED = os.getenv('MOMENTUM_BREAKOUT_MODE_ENABLED', '1') == '1'
MOMENTUM_SCAN_CANDIDATE_LIMIT = int(os.getenv('MOMENTUM_SCAN_CANDIDATE_LIMIT', '100'))
MOMENTUM_WATCHLIST_SIZE = int(os.getenv('MOMENTUM_WATCHLIST_SIZE', '10'))
MOMENTUM_MIN_DAY_CHANGE_PCT = float(os.getenv('MOMENTUM_MIN_DAY_CHANGE_PCT', '40.0'))
MOMENTUM_EXTREME_DAY_CHANGE_PCT = float(os.getenv('MOMENTUM_EXTREME_DAY_CHANGE_PCT', '100.0'))
MOMENTUM_MIN_RVOL = float(os.getenv('MOMENTUM_MIN_RVOL', '3.0'))
MOMENTUM_EXTREME_RVOL = float(os.getenv('MOMENTUM_EXTREME_RVOL', '7.0'))
MOMENTUM_MIN_DOLLAR_VOLUME = float(os.getenv('MOMENTUM_MIN_DOLLAR_VOLUME', '1000000'))
MOMENTUM_MAX_SPREAD_PCT = float(os.getenv('MOMENTUM_MAX_SPREAD_PCT', '0.025'))
MOMENTUM_MIN_PRICE = float(os.getenv('MOMENTUM_MIN_PRICE', '0.20'))
MOMENTUM_MAX_PRICE = float(os.getenv('MOMENTUM_MAX_PRICE', '25.0'))
MOMENTUM_NO_BUY_AFTER_ET = os.getenv('MOMENTUM_NO_BUY_AFTER_ET', '15:30').strip() or '15:30'
MOMENTUM_MAX_ENTRY_EXTENSION_PCT = float(os.getenv('MOMENTUM_MAX_ENTRY_EXTENSION_PCT', '0.08'))
MOMENTUM_MIN_PULLBACK_RECLAIM_SCORE = int(os.getenv('MOMENTUM_MIN_PULLBACK_RECLAIM_SCORE', '3'))
MOMENTUM_AUTO_EXECUTE_ENABLED = os.getenv('MOMENTUM_AUTO_EXECUTE_ENABLED', '0') == '1'
WATCH_RECHECK_ENABLED = os.getenv('WATCH_RECHECK_ENABLED', '1') == '1'
WATCH_CANDIDATE_TTL_MINUTES = int(os.getenv('WATCH_CANDIDATE_TTL_MINUTES', '90'))
WATCH_RECHECK_LIMIT = int(os.getenv('WATCH_RECHECK_LIMIT', '25'))
MOMENTUM_DEBUG_REJECTIONS_LIMIT = int(os.getenv('MOMENTUM_DEBUG_REJECTIONS_LIMIT', '50'))

MOMENTUM_ALLOW_PENNY_STOCKS = os.getenv('MOMENTUM_ALLOW_PENNY_STOCKS', '1') == '1'
BIOTECH_TRADING_ENABLED = os.getenv('BIOTECH_TRADING_ENABLED', '1') == '1'
ETF_TRADING_ENABLED = os.getenv('ETF_TRADING_ENABLED', '1') == '1'
LEVERAGED_ETF_TRADING_ENABLED = os.getenv('LEVERAGED_ETF_TRADING_ENABLED', '0') == '1'
INVERSE_ETF_TRADING_ENABLED = os.getenv('INVERSE_ETF_TRADING_ENABLED', '0') == '1'
CRYPTO_ETF_TRADING_ENABLED = os.getenv('CRYPTO_ETF_TRADING_ENABLED', '1') == '1'
OPTIONS_TRADING_ENABLED = os.getenv('OPTIONS_TRADING_ENABLED', '0') == '1'

PLACEHOLDER_VALUES = {'change-me', 'placeholder', 'replace-me', 'your_value_here', 'test', 'example'}


def _is_placeholder(value: str | None) -> bool:
    if value is None:
        return True
    return value.strip().lower() in PLACEHOLDER_VALUES or not value.strip()


def validate_required_production_config(strict: bool = False) -> list[str]:
    errors: list[str] = []
    if not strict:
        return errors
    checks = {
        'SECRET_KEY': SECRET_KEY,
        'TOKEN_ENCRYPTION_KEY': TOKEN_ENCRYPTION_KEY,
        'REDIS_URL': REDIS_URL,
        'RATELIMIT_STORAGE_URI': RATELIMIT_STORAGE_URI,
        'STRIPE_PUBLIC_KEY': STRIPE_PUBLIC_KEY,
        'STRIPE_SECRET_KEY': STRIPE_SECRET_KEY,
        'STRIPE_WEBHOOK_SECRET': STRIPE_WEBHOOK_SECRET,
        'STRIPE_PRICE_ID_MONTHLY': STRIPE_PRICE_ID_MONTHLY,
        'STRIPE_PRICE_ID_ANNUAL': STRIPE_PRICE_ID_ANNUAL,
        'BREVO_API_KEY': BREVO_API_KEY,
        'BREVO_SENDER_EMAIL': BREVO_SENDER_EMAIL,
        'ALPACA_CLIENT_ID': ALPACA_CLIENT_ID,
        'ALPACA_CLIENT_SECRET': ALPACA_CLIENT_SECRET,
        'ALPACA_REDIRECT_URI': ALPACA_REDIRECT_URI,
        'FINNHUB_API_KEY': FINNHUB_API_KEY,
        'GEMINI_API_KEY': GEMINI_API_KEY,
    }
    for k, v in checks.items():
        if _is_placeholder(str(v) if v is not None else None):
            errors.append(f'{k} is missing or placeholder.')
    normalized_db_url = normalize_database_url(DATABASE_URL)
    if _is_placeholder(normalized_db_url):
        errors.append('DATABASE_URL is missing or placeholder.')
    elif not normalized_db_url.startswith('postgresql+psycopg://'):
        errors.append('DATABASE_URL must be a PostgreSQL connection URL.')
    if os.getenv('FLASK_DEBUG', '0') != '0':
        errors.append('FLASK_DEBUG must be 0 in production.')
    if FLASK_ENV != 'production':
        errors.append('FLASK_ENV must be production.')
    if not APP_BASE_URL.startswith('https://'):
        errors.append('APP_BASE_URL must start with https://.')
    if not SESSION_COOKIE_SECURE:
        errors.append('SESSION_COOKIE_SECURE must be 1.')
    if not SESSION_COOKIE_SAMESITE:
        errors.append('SESSION_COOKIE_SAMESITE must be set.')
    if not ALPACA_REDIRECT_URI.startswith('https://'):
        errors.append('ALPACA_REDIRECT_URI must use https.')
    if 'https://xeanvi.com' not in WTF_CSRF_TRUSTED_ORIGINS or 'https://www.xeanvi.com' not in WTF_CSRF_TRUSTED_ORIGINS:
        errors.append('WTF_CSRF_TRUSTED_ORIGINS must include https://xeanvi.com and https://www.xeanvi.com.')
    if BREVO_RESET_PASSWORD_TEMPLATE_ID and not str(BREVO_RESET_PASSWORD_TEMPLATE_ID).isdigit():
        errors.append('BREVO_RESET_PASSWORD_TEMPLATE_ID must be numeric.')
    if not BREVO_SIGNUP_SYNC_OPTIONAL and BREVO_SIGNUP_LIST_ID <= 0:
        errors.append('BREVO_SIGNUP_LIST_ID must be a positive integer unless BREVO_SIGNUP_SYNC_OPTIONAL=1.')
    if STRIPE_PRICE_ID_MONTHLY and STRIPE_PRICE_ID_MONTHLY == STRIPE_PRICE_ID_ANNUAL:
        errors.append('STRIPE_PRICE_ID_MONTHLY and STRIPE_PRICE_ID_ANNUAL must be different.')
    return errors
