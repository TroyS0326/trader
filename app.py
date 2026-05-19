import hashlib
import json
import logging
import os
import re
import redis
import requests
import secrets
import stripe
from urllib.parse import urlencode, urlparse
from werkzeug.middleware.proxy_fix import ProxyFix

from datetime import datetime, timezone
from sqlalchemy import inspect, text, or_
from sqlalchemy.exc import IntegrityError
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, make_response, render_template, request, redirect, session, url_for, flash, abort
from flask_login import login_user, logout_user, current_user, login_required
from flask_login import LoginManager
from flask_sock import Sock
from flask_talisman import Talisman
from flask_wtf.csrf import CSRFError, CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.security import generate_password_hash, check_password_hash
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature

import config
from sentry_setup import init_sentry

init_sentry("xeanvi-web")

import scanner as scanner_module
from broker import BrokerError, get_order, maybe_activate_runner_trailing, place_managed_entry_order
import db as trade_db
from db import get_failed_trades_today, get_recent_scans, get_recent_trades, get_trade_by_order_id, init_db, insert_scan, insert_trade, update_trade_status
from execution import start_engine
from models import db
from models import BlogPost, BlogPublishingPlan, User, UserEvent, StripeEvent, Trade, DailyReportEmailLog, WatchCandidate, AdminDailyDigestEmailLog
from onboarding import fetch_and_sync_bankroll, verify_alpaca_data_feed, detect_and_store_alpaca_connection
from scanner import ScanError, buy_window_open, get_stock_chart_pack, now_et, run_scan, get_momentum_breakout_universe, get_snapshots, get_latest_quotes, resolve_data_feed
from scanner import get_bars, analyze_symbol, get_company_profile, get_alpaca_asset
from asset_classifier import classify_asset
from dynamic_orb import get_latest_dynamic_orb_state
from watchlist import watchlist_manager
from scan_contract import validate_scan_payload_contract
from explainability import generate_trade_thesis
from blog_ai import generate_blog_draft
from blog_ai_fixes import apply_ai_seo_cleanup
from blog_seo import analyze_blog_post_seo
from blog_seo_fixes import apply_safe_seo_fixes
from blog_internal_links import suggest_internal_links
from blog_human_quality import analyze_human_quality
from blog_images import save_blog_featured_image
from blog_image_seo import generate_image_alt_caption
from scanner_effectiveness import build_scanner_effectiveness_report
from db_safety import validate_runtime_database_safety, assert_not_empty_production_database, assert_existing_production_database_has_users
from execution_guard import (
    approve_scan_for_user,
    validate_execution_against_approved_scan,
    audit_trade_log,
)

app = Flask(__name__)

# UPDATED: Standard single-proxy setup (e.g., Nginx only)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=0, x_prefix=0)

# 2. Enable Global CSRF Protection
csrf = CSRFProtect(app)

# 2. Setup the Rate Limiter
# Note: "memory://" works great for a single server. If you scale to multiple
# servers later, switch this to a shared store such as Redis.
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["500 per day", "100 per hour"],
    storage_uri=config.RATELIMIT_STORAGE_URI,
)

# Enforce HTTPS, HSTS, and strict Content Security Policies
if config.IS_PRODUCTION:
    csp = {
        'default-src': [
            "'self'",
        ],
        'script-src': [
            "'self'",
            "'unsafe-inline'",
            'https://js.stripe.com',
            'https://connect.facebook.net',
            'https://unpkg.com',
            'https://cdnjs.cloudflare.com',
            # Google Ads / Google tag destinations required for AW-18144975964.
            'https://www.googletagmanager.com',
            'https://www.googleadservices.com',
            'https://www.google.com',
            'https://pagead2.googlesyndication.com',
            'https://googleads.g.doubleclick.net',
        ],
        'style-src': [
            "'self'",
            "'unsafe-inline'",
            'https://fonts.googleapis.com',
            'https://cdnjs.cloudflare.com',
        ],
        'font-src': [
            "'self'",
            'data:',
            'https://fonts.gstatic.com',
            'https://cdnjs.cloudflare.com',
        ],
        'connect-src': [
            "'self'",
            'https://www.facebook.com',
            'https://connect.facebook.net',
            'https://api.stripe.com',
            'https://www.googletagmanager.com',
            'https://pagead2.googlesyndication.com',
            'https://www.googleadservices.com',
            'https://googleads.g.doubleclick.net',
            'https://ad.doubleclick.net',
            'https://www.google.com',
            'https://google.com',
        ],
        'img-src': [
            "'self'",
            'data:',
            'https://www.facebook.com',
            'https://www.googletagmanager.com',
            'https://googleads.g.doubleclick.net',
            'https://www.google.com',
            'https://pagead2.googlesyndication.com',
            'https://www.googleadservices.com',
            'https://google.com',
        ],
        'frame-src': [
            "'self'",
            'https://js.stripe.com',
            'https://hooks.stripe.com',
            'https://www.googletagmanager.com',
        ],
    }
    Talisman(app, content_security_policy=csp, force_https=True, strict_transport_security=True)

# THE FIX: Allow login even if the host/referrer strings have a proxy-induced mismatch
app.config['WTF_CSRF_SSL_STRICT'] = True

# Ensure these remain bulletproof
app.config['SESSION_COOKIE_DOMAIN'] = config.SESSION_COOKIE_DOMAIN
app.config['SESSION_COOKIE_SECURE'] = config.SESSION_COOKIE_SECURE
app.config['SESSION_COOKIE_SAMESITE'] = config.SESSION_COOKIE_SAMESITE
app.config['WTF_CSRF_TRUSTED_ORIGINS'] = config.WTF_CSRF_TRUSTED_ORIGINS

app.config['SECRET_KEY'] = config.SECRET_KEY
app.config['SQLALCHEMY_DATABASE_URI'] = config.SQLALCHEMY_DATABASE_URI
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = config.SQLALCHEMY_ENGINE_OPTIONS
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['ALPACA_CLIENT_ID'] = config.ALPACA_CLIENT_ID
app.config['ALPACA_CLIENT_SECRET'] = config.ALPACA_CLIENT_SECRET
app.config['ALPACA_REDIRECT_URI'] = config.ALPACA_REDIRECT_URI
db.init_app(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
sock = Sock(app)
logger = logging.getLogger(__name__)
stripe.api_key = config.STRIPE_SECRET_KEY
redis_client = redis.Redis.from_url(config.REDIS_URL, decode_responses=True)
ADMIN_EMAIL = os.getenv('ADMIN_EMAIL', '').strip().lower()
brevo_missing = config.validate_brevo_config()
if brevo_missing:
    logger.warning('Brevo configuration missing keys: %s', ",".join(brevo_missing))


def _brief_error_text(text: str, max_len: int = 180) -> str:
    cleaned = (text or '').replace('\n', ' ').replace('\r', ' ').strip()
    return cleaned[:max_len]


@app.context_processor
def inject_template_flags():
    return {
        'meta_pixel_id': getattr(config, 'META_PIXEL_ID', ''),
        'google_ads_id': getattr(config, 'GOOGLE_ADS_ID', ''),
        'launch_promo_active': bool(config.LAUNCH_PROMO_ENABLED and config.LAUNCH_PROMO_STRIPE_COUPON_ID),
        'google_ads_conversion_labels': {
            'signup': config.GOOGLE_ADS_CONVERSION_SIGNUP_LABEL,
            'checkout': config.GOOGLE_ADS_CONVERSION_CHECKOUT_LABEL,
            'purchase': config.GOOGLE_ADS_CONVERSION_PURCHASE_LABEL,
            'contact_email': config.GOOGLE_ADS_CONVERSION_CONTACT_EMAIL_LABEL,
            'contact_phone': config.GOOGLE_ADS_CONVERSION_CONTACT_PHONE_LABEL,
        },
    }

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


def ensure_db_initialized() -> None:
    try:
        init_db()
    except Exception as exc:
        logger.exception('Database initialization failed for URI %s', config.SQLALCHEMY_DATABASE_URI)
        raise RuntimeError(f'Database initialization failed: {exc}') from exc


VALID_REFRESH_INTERVALS = {10000, 30000, 60000}

BLOG_ALLOWED_TAGS = ['h2', 'h3', 'h4', 'p', 'br', 'strong', 'em', 'ul', 'ol', 'li', 'a', 'blockquote', 'code', 'pre']
BLOG_ALLOWED_ATTRIBUTES = {'a': ['href', 'title', 'rel', 'target']}
BLOG_ALLOWED_STATUSES = {'draft', 'published'}
BLOG_PLAN_ALLOWED_STATUSES = {'idea', 'queued', 'drafting', 'drafted', 'needs_review', 'ready_to_publish', 'published', 'archived'}


def parse_int(value, default=3, min_value=1, max_value=5):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(min_value, min(max_value, parsed))


def parse_date(value):
    raw = (value or '').strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, '%Y-%m-%d').date()
    except ValueError:
        return None


def is_admin_user() -> bool:
    return bool(ADMIN_EMAIL) and current_user.is_authenticated and ((current_user.email or '').strip().lower() == ADMIN_EMAIL)




def mask_identifier(value: str, visible: int = 6) -> str:
    text = (value or '').strip()
    if not text:
        return ''
    if len(text) <= visible:
        return '*' * len(text)
    return ('*' * (len(text) - visible)) + text[-visible:]


def safe_user_summary(user: User) -> dict:
    return {
        'id': user.id,
        'email': user.email,
        'full_name': user.full_name,
        'subscription_status': user.subscription_status,
        'subscription_plan': user.subscription_plan,
        'stripe_customer_id_masked': mask_identifier(user.stripe_customer_id),
        'stripe_subscription_id_masked': mask_identifier(user.stripe_subscription_id),
        'trading_mode': user.trading_mode,
        'alpaca_paper_account_id': user.alpaca_paper_account_id,
        'alpaca_live_account_id': user.alpaca_live_account_id,
        'onboarding_completed': user.onboarding_completed,
        'paper_bankroll_set': user.paper_bankroll_set,
        'first_scan_completed': user.first_scan_completed,
        'scan_preview_completed': user.scan_preview_completed,
        'playbook_reviewed': user.playbook_reviewed,
        'transparency_reviewed': user.transparency_reviewed,
        'broker_connection_started': user.broker_connection_started,
        'paper_bankroll': user.paper_bankroll,
        'live_bankroll': user.live_bankroll,
        'bankroll': user.bankroll,
    }


def log_admin_recovery_action(action: str, target_user_id: int, metadata: dict | None = None) -> None:
    context = dict(metadata or {})
    context.update({'action': action, 'target_user_id': target_user_id})
    try:
        track_user_event(f'admin_user_recovery.{action}', user=current_user, context=context)
    except Exception as exc:
        logger.warning('Failed to log admin recovery action %s: %s', action, exc)

def slugify_blog_title(title: str) -> str:
    base = re.sub(r'[^a-z0-9\s-]', '', (title or '').strip().lower())
    base = re.sub(r'[\s\-]+', '-', base).strip('-')
    return base or 'post'


def build_blog_canonical_url(slug: str) -> str:
    base_url = (getattr(config, 'APP_BASE_URL', '') or 'https://xeanvi.com').strip().rstrip('/')
    clean_slug = (slug or '').strip().strip('/')
    return f"{base_url}/blog/{clean_slug}" if clean_slug else ""


def get_public_base_url() -> str:
    return (getattr(config, 'APP_BASE_URL', '') or 'https://xeanvi.com').strip().rstrip('/')


def unique_blog_slug(title: str, existing_post_id: int = None) -> str:
    base_slug = slugify_blog_title(title)
    candidate = base_slug
    counter = 2
    while True:
        query = BlogPost.query.filter_by(slug=candidate)
        if existing_post_id is not None:
            query = query.filter(BlogPost.id != existing_post_id)
        if query.first() is None:
            return candidate
        candidate = f"{base_slug}-{counter}"
        counter += 1


def sanitize_blog_html(raw_html: str) -> str:
    cleaned = raw_html or ''
    cleaned = re.sub(r'(?is)<\s*(script|style|iframe).*?>.*?<\s*/\s*\1\s*>', '', cleaned)
    cleaned = re.sub(r'(?i)\son\w+\s*=\s*(\"[^\"]*\"|\'[^\']*\'|[^\s>]+)', '', cleaned)
    cleaned = re.sub(r'(?i)href\s*=\s*[\"\']\s*javascript:[^\"\']*[\"\']', 'href="#"', cleaned)
    cleaned = re.sub(r'(?i)src\s*=\s*[\"\']\s*javascript:[^\"\']*[\"\']', '', cleaned)
    cleaned = re.sub(r'(?i)<(?!/?(?:h2|h3|h4|p|br|strong|em|ul|ol|li|a|blockquote|code|pre)\b)[^>]*>', '', cleaned)
    cleaned = re.sub(r'(?i)<a([^>]*)href=\"(https?://[^\"]+)\"([^>]*)>', r'<a\1href="\2" target="_blank" rel="noopener noreferrer"\3>', cleaned)
    return cleaned


def ok(data=None, **kwargs):
    payload = {'ok': True}
    if data is not None:
        payload['data'] = data
    payload.update(kwargs)
    return jsonify(payload)


def fail(message: str, status: int = 400, **extras):
    payload = {'ok': False, 'error': message}
    payload.update(extras)
    return jsonify(payload), status


PASSWORD_RESET_SALT = 'xeanvi-password-reset'

def get_dynamic_orb_metadata_fallback(reason: str = "Dynamic ORB state unavailable; existing static rules remained active.") -> dict:
    return {
        "mode": "unknown",
        "start_time_et": config.NO_BUY_BEFORE_ET,
        "preferred_setup": "unknown",
        "reason": reason,
    }


def get_dynamic_orb_metadata() -> dict:
    try:
        return get_latest_dynamic_orb_state()
    except Exception as exc:
        logger.warning("Dynamic ORB state unavailable for metadata: %s", exc)
        return get_dynamic_orb_metadata_fallback()


def is_valid_email(email):
    email = (email or '').strip().lower()
    if not email:
        return False
    if ' ' in email:
        return False
    if '@' not in email:
        return False
    local, domain = email.rsplit('@', 1)
    if not local or not domain:
        return False
    if '.' not in domain:
        return False
    if domain.startswith('.') or domain.endswith('.'):
        return False
    return True


def get_password_reset_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(app.config['SECRET_KEY'])


def password_hash_fingerprint(user: User) -> str:
    """
    Makes password reset tokens automatically invalid after the password changes.
    This avoids needing a separate password_reset_tokens database table.
    """
    return hashlib.sha256(user.password_hash.encode('utf-8')).hexdigest()[:24]


def generate_password_reset_token(user: User) -> str:
    serializer = get_password_reset_serializer()

    payload = {
        'user_id': user.id,
        'email': user.email,
        'pwd': password_hash_fingerprint(user),
    }

    return serializer.dumps(payload, salt=PASSWORD_RESET_SALT)


def verify_password_reset_token(token: str):
    serializer = get_password_reset_serializer()
    max_age = getattr(config, 'PASSWORD_RESET_TOKEN_MAX_AGE_SECONDS', 3600)

    try:
        data = serializer.loads(
            token,
            salt=PASSWORD_RESET_SALT,
            max_age=max_age,
        )
    except SignatureExpired:
        logger.warning('Expired password reset token used.')
        return None
    except BadSignature:
        logger.warning('Invalid password reset token used.')
        return None
    except Exception as exc:
        logger.error('Password reset token verification failed: %s', exc)
        return None

    user_id = data.get('user_id')
    email = data.get('email')
    pwd_fingerprint = data.get('pwd')

    if not user_id or not email or not pwd_fingerprint:
        return None

    user = db.session.get(User, int(user_id))

    if not user:
        return None

    if user.email != email:
        return None

    if password_hash_fingerprint(user) != pwd_fingerprint:
        return None

    return user


def build_password_reset_url(user: User) -> str:
    token = generate_password_reset_token(user)
    base_url = getattr(config, 'APP_BASE_URL', 'https://xeanvi.com').rstrip('/')
    return f'{base_url}{url_for("reset_password_with_token", token=token)}'


def send_password_reset_email(user: User, reset_url: str) -> bool:
    """
    Sends a Brevo transactional password reset email using a saved Brevo template.
    Required env vars:
      BREVO_API_KEY
      BREVO_RESET_PASSWORD_TEMPLATE_ID
      BREVO_SENDER_EMAIL
      BREVO_SENDER_NAME
    """
    api_key = getattr(config, 'BREVO_API_KEY', None) or os.getenv('BREVO_API_KEY')
    template_id = getattr(config, 'BREVO_RESET_PASSWORD_TEMPLATE_ID', None) or os.getenv('BREVO_RESET_PASSWORD_TEMPLATE_ID')

    if not api_key:
        logger.error('BREVO_API_KEY is missing. Password reset email not sent.')
        return False

    if not template_id:
        logger.error('BREVO_RESET_PASSWORD_TEMPLATE_ID is missing. Password reset email not sent.')
        return False

    try:
        template_id = int(template_id)
    except ValueError:
        logger.error('BREVO_RESET_PASSWORD_TEMPLATE_ID must be a number.')
        return False

    full_name = (user.full_name or '').strip()
    first_name = full_name.split(' ')[0] if full_name else 'there'

    payload = {
        'sender': {
            'name': getattr(config, 'BREVO_SENDER_NAME', 'XeanVI Security'),
            'email': getattr(config, 'BREVO_SENDER_EMAIL', 'support@xeanvi.com'),
        },
        'to': [
            {
                'email': user.email,
                'name': full_name or user.email,
            }
        ],
        'templateId': template_id,
        'params': {
            'first_name': first_name,
            'reset_url': reset_url,
            'expires_minutes': int(getattr(config, 'PASSWORD_RESET_TOKEN_MAX_AGE_SECONDS', 3600) / 60),
            'support_email': getattr(config, 'BREVO_SENDER_EMAIL', 'support@xeanvi.com'),
        },
        'tags': ['password-reset'],
    }

    headers = {
        'accept': 'application/json',
        'content-type': 'application/json',
        'api-key': api_key,
    }

    try:
        response = requests.post(
            'https://api.brevo.com/v3/smtp/email',
            json=payload,
            headers=headers,
            timeout=15,
        )

        if response.status_code in [200, 201, 202]:
            logger.info('Password reset email sent to user_id=%s', user.id)
            return True

        logger.error('Brevo password reset email failed: %s %s', response.status_code, _brief_error_text(response.text))
        return False

    except Exception as exc:
        logger.error('Brevo password reset email exception: %s', exc)
        return False


def add_signup_user_to_brevo(user):
    api_key = getattr(config, 'BREVO_API_KEY', None) or os.getenv('BREVO_API_KEY')
    list_id = getattr(config, 'BREVO_SIGNUP_LIST_ID', 0)
    signup_sync_optional = bool(getattr(config, 'BREVO_SIGNUP_SYNC_OPTIONAL', False))

    if not api_key:
        logger.error('Brevo signup automation skipped: missing BREVO_API_KEY for user_id=%s', user.id)
        return False

    if not list_id:
        if signup_sync_optional:
            logger.info('Brevo signup automation skipped (optional): missing BREVO_SIGNUP_LIST_ID for user_id=%s', user.id)
        else:
            logger.error('Brevo signup automation skipped: missing BREVO_SIGNUP_LIST_ID for user_id=%s', user.id)
        return False

    full_name = (user.full_name or '').strip()
    first_name = full_name.split(' ')[0] if full_name else ''

    payload = {
        'email': user.email,
        'attributes': {
            'FIRSTNAME': first_name,
            'FULLNAME': user.full_name or '',
            'SUBSCRIPTION_STATUS': user.subscription_status or 'free',
            'SIGNUP_SOURCE': 'xeanvi_signup',
        },
        'listIds': [list_id],
        'updateEnabled': True,
    }

    headers = {
        'accept': 'application/json',
        'content-type': 'application/json',
        'api-key': api_key,
    }

    try:
        response = requests.post('https://api.brevo.com/v3/contacts', json=payload, headers=headers, timeout=15)

        if response.status_code in [200, 201, 204]:
            logger.info('Brevo signup automation success for user_id=%s email=%s list_id=%s', user.id, user.email, list_id)
            return True

        logger.error('Brevo signup automation failed for user_id=%s status=%s response=%s', user.id, response.status_code, _brief_error_text(response.text))
        return False
    except Exception as exc:
        logger.error('Brevo signup automation exception for user_id=%s: %s', user.id, exc)
        return False


def update_brevo_contact_attributes(user, attributes: dict) -> bool:
    """
    Updates existing Brevo contact attributes for funnel automation.
    This should never break the app if Brevo fails.
    """
    api_key = getattr(config, 'BREVO_API_KEY', None) or os.getenv('BREVO_API_KEY')

    if not api_key:
        logger.error('Brevo attribute sync skipped: missing BREVO_API_KEY for user_id=%s', getattr(user, 'id', None))
        return False

    if not user or not getattr(user, 'email', None):
        logger.error('Brevo attribute sync skipped: missing user/email.')
        return False

    email = (user.email or '').strip().lower()

    if not is_valid_email(email):
        logger.error('Brevo attribute sync skipped: invalid email for user_id=%s email=%s', getattr(user, 'id', None), email)
        return False

    safe_attributes = {}
    for key, value in (attributes or {}).items():
        if not key:
            continue
        safe_attributes[str(key).strip().upper()] = value

    if not safe_attributes:
        return False

    url = f"https://api.brevo.com/v3/contacts/{email}"

    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "api-key": api_key,
    }

    payload = {
        "attributes": safe_attributes,
    }

    try:
        response = requests.put(url, json=payload, headers=headers, timeout=15)

        if response.status_code in [200, 201, 202, 204]:
            logger.info(
                "Brevo attributes synced for user_id=%s attrs=%s",
                getattr(user, 'id', None),
                ",".join(sorted(safe_attributes.keys())),
            )
            return True

        if response.status_code == 404:
            create_payload = {
                "email": email,
                "attributes": safe_attributes,
                "updateEnabled": True,
            }

            signup_list_id = getattr(config, 'BREVO_SIGNUP_LIST_ID', 0)
            if signup_list_id:
                create_payload["listIds"] = [signup_list_id]

            create_response = requests.post(
                "https://api.brevo.com/v3/contacts",
                json=create_payload,
                headers=headers,
                timeout=15,
            )

            if create_response.status_code in [200, 201, 202, 204]:
                logger.info(
                    "Brevo contact created during attribute sync for user_id=%s attrs=%s",
                    getattr(user, 'id', None),
                    ",".join(sorted(safe_attributes.keys())),
                )
                return True

            logger.error(
                "Brevo contact create during sync failed for user_id=%s status=%s response=%s",
                getattr(user, 'id', None),
                create_response.status_code,
                _brief_error_text(create_response.text),
            )
            return False

        logger.error(
            "Brevo attribute sync failed for user_id=%s status=%s response=%s",
            getattr(user, 'id', None),
            response.status_code,
            response.text,
        )
        return False

    except Exception as exc:
        logger.error(
            "Brevo attribute sync exception for user_id=%s: %s",
            getattr(user, 'id', None),
            exc,
        )
        return False


def get_user_brevo_funnel_attributes(user) -> dict:
    is_pro = (getattr(user, 'subscription_status', '') or '').lower() == 'pro'

    alpaca_paper_connected = bool(
        getattr(user, 'alpaca_paper_account_id', None)
        or getattr(user, 'alpaca_paper_access_token', None)
    )

    setup_complete = bool(
        getattr(user, 'onboarding_completed', False)
        and getattr(user, 'paper_bankroll_set', False)
        and getattr(user, 'playbook_reviewed', False)
        and getattr(user, 'first_scan_completed', False)
        and getattr(user, 'transparency_reviewed', False)
    )

    full_name = (getattr(user, 'full_name', '') or '').strip()
    first_name = full_name.split(' ')[0] if full_name else ''

    return {
        "FIRSTNAME": first_name,
        "FULLNAME": full_name,
        "SUBSCRIPTION_STATUS": getattr(user, 'subscription_status', None) or "free",
        "ALPACA_PAPER_CONNECTED": alpaca_paper_connected,
        "FIRST_SCAN_COMPLETED": bool(getattr(user, 'first_scan_completed', False)),
        "SCAN_PREVIEW_COMPLETED": bool(getattr(user, 'scan_preview_completed', False) or getattr(user, 'first_scan_completed', False)),
        "SETUP_CHECKLIST_COMPLETED": setup_complete,
        "IS_PRO": is_pro,
        "SIGNUP_SOURCE": "xeanvi_signup",
    }


def ensure_schema_migrations() -> None:
    """Safely backfill schema missing from older SQLite DBs using the existing SQLAlchemy pool."""
    def current_db_dialect() -> str:
        return db.engine.dialect.name

    def bool_default(value: bool) -> str:
        if current_db_dialect() == 'postgresql':
            return 'TRUE' if value else 'FALSE'
        return '1' if value else '0'

    def datetime_type() -> str:
        return 'TIMESTAMP' if current_db_dialect() == 'postgresql' else 'DATETIME'

    def quote_table_name(name: str) -> str:
        return db.engine.dialect.identifier_preparer.quote(name)

    inspector = inspect(db.engine)
    table_names = inspector.get_table_names()
    BlogPost.__table__.create(bind=db.engine, checkfirst=True)
    BlogPublishingPlan.__table__.create(bind=db.engine, checkfirst=True)
    StripeEvent.__table__.create(bind=db.engine, checkfirst=True)
    DailyReportEmailLog.__table__.create(bind=db.engine, checkfirst=True)
    AdminDailyDigestEmailLog.__table__.create(bind=db.engine, checkfirst=True)
    WatchCandidate.__table__.create(bind=db.engine, checkfirst=True)

    with db.engine.begin() as conn:
        if 'user' in table_names:
            user_table = quote_table_name('user')
            existing_columns = {col['name'] for col in inspector.get_columns('user')}

            if 'refresh_interval' not in existing_columns:
                conn.execute(text("ALTER TABLE \"user\" ADD COLUMN refresh_interval INTEGER NOT NULL DEFAULT 30000"))

            if 'show_news' not in existing_columns:
                conn.execute(text(f"ALTER TABLE {user_table} ADD COLUMN show_news BOOLEAN NOT NULL DEFAULT {bool_default(True)}"))
                conn.execute(text(f"ALTER TABLE {user_table} ADD COLUMN show_watchlist BOOLEAN NOT NULL DEFAULT {bool_default(True)}"))
                conn.execute(text(f"ALTER TABLE {user_table} ADD COLUMN show_terminal BOOLEAN NOT NULL DEFAULT {bool_default(True)}"))

            if 'esg_fossil_fuels' not in existing_columns:
                conn.execute(text(f"ALTER TABLE {user_table} ADD COLUMN esg_fossil_fuels BOOLEAN NOT NULL DEFAULT {bool_default(False)}"))
                conn.execute(text(f"ALTER TABLE {user_table} ADD COLUMN esg_weapons BOOLEAN NOT NULL DEFAULT {bool_default(False)}"))
                conn.execute(text(f"ALTER TABLE {user_table} ADD COLUMN esg_tobacco BOOLEAN NOT NULL DEFAULT {bool_default(False)}"))
                conn.execute(text(f"ALTER TABLE {user_table} ADD COLUMN exclude_penny_stocks BOOLEAN NOT NULL DEFAULT {bool_default(True)}"))
                conn.execute(text(f"ALTER TABLE {user_table} ADD COLUMN exclude_biotech BOOLEAN NOT NULL DEFAULT {bool_default(False)}"))

            if 'trading_mode' not in existing_columns:
                conn.execute(text("ALTER TABLE \"user\" ADD COLUMN trading_mode VARCHAR(20) NOT NULL DEFAULT 'paper'"))

            if 'alpaca_data_feed' not in existing_columns:
                conn.execute(text("ALTER TABLE \"user\" ADD COLUMN alpaca_data_feed VARCHAR(10) NOT NULL DEFAULT 'iex'"))

            if 'alpaca_paper_access_token' not in existing_columns:
                conn.execute(text("ALTER TABLE \"user\" ADD COLUMN alpaca_paper_access_token TEXT"))

            if 'alpaca_live_access_token' not in existing_columns:
                conn.execute(text("ALTER TABLE \"user\" ADD COLUMN alpaca_live_access_token TEXT"))

            if 'alpaca_paper_account_id' not in existing_columns:
                conn.execute(text("ALTER TABLE \"user\" ADD COLUMN alpaca_paper_account_id VARCHAR(100)"))

            if 'alpaca_live_account_id' not in existing_columns:
                conn.execute(text("ALTER TABLE \"user\" ADD COLUMN alpaca_live_account_id VARCHAR(100)"))

            if 'paper_bankroll' not in existing_columns:
                conn.execute(text("ALTER TABLE \"user\" ADD COLUMN paper_bankroll FLOAT NOT NULL DEFAULT 0.0"))

            if 'live_bankroll' not in existing_columns:
                conn.execute(text("ALTER TABLE \"user\" ADD COLUMN live_bankroll FLOAT NOT NULL DEFAULT 0.0"))

            if 'onboarding_completed' not in existing_columns:
                conn.execute(text(f"ALTER TABLE {user_table} ADD COLUMN onboarding_completed BOOLEAN NOT NULL DEFAULT {bool_default(False)}"))

            if 'paper_bankroll_set' not in existing_columns:
                conn.execute(text(f"ALTER TABLE {user_table} ADD COLUMN paper_bankroll_set BOOLEAN NOT NULL DEFAULT {bool_default(False)}"))

            if 'first_scan_completed' not in existing_columns:
                conn.execute(text(f"ALTER TABLE {user_table} ADD COLUMN first_scan_completed BOOLEAN NOT NULL DEFAULT {bool_default(False)}"))

            if 'scan_preview_completed' not in existing_columns:
                conn.execute(text(f"ALTER TABLE {user_table} ADD COLUMN scan_preview_completed BOOLEAN NOT NULL DEFAULT {bool_default(False)}"))

            if 'playbook_reviewed' not in existing_columns:
                conn.execute(text(f"ALTER TABLE {user_table} ADD COLUMN playbook_reviewed BOOLEAN NOT NULL DEFAULT {bool_default(False)}"))

            if 'transparency_reviewed' not in existing_columns:
                conn.execute(text(f"ALTER TABLE {user_table} ADD COLUMN transparency_reviewed BOOLEAN NOT NULL DEFAULT {bool_default(False)}"))

            if 'broker_connection_started' not in existing_columns:
                conn.execute(text(f"ALTER TABLE {user_table} ADD COLUMN broker_connection_started BOOLEAN NOT NULL DEFAULT {bool_default(False)}"))

            
            if 'created_at' not in existing_columns:
                conn.execute(text(f"ALTER TABLE {user_table} ADD COLUMN created_at {datetime_type()}"))
                conn.execute(text(f"UPDATE {user_table} SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL"))
            if 'updated_at' not in existing_columns:
                conn.execute(text(f"ALTER TABLE {user_table} ADD COLUMN updated_at {datetime_type()}"))
                conn.execute(text(f"UPDATE {user_table} SET updated_at = CURRENT_TIMESTAMP WHERE updated_at IS NULL"))

            if 'allow_penny_stocks' not in existing_columns:
                conn.execute(text(f"ALTER TABLE {user_table} ADD COLUMN allow_penny_stocks BOOLEAN NOT NULL DEFAULT {bool_default(False)}"))
            if 'allow_biotech' not in existing_columns:
                conn.execute(text(f"ALTER TABLE {user_table} ADD COLUMN allow_biotech BOOLEAN NOT NULL DEFAULT {bool_default(True)}"))
            if 'allow_etf_trading' not in existing_columns:
                conn.execute(text(f"ALTER TABLE {user_table} ADD COLUMN allow_etf_trading BOOLEAN NOT NULL DEFAULT {bool_default(True)}"))
            if 'allow_leveraged_etfs' not in existing_columns:
                conn.execute(text(f"ALTER TABLE {user_table} ADD COLUMN allow_leveraged_etfs BOOLEAN NOT NULL DEFAULT {bool_default(False)}"))
            if 'allow_inverse_etfs' not in existing_columns:
                conn.execute(text(f"ALTER TABLE {user_table} ADD COLUMN allow_inverse_etfs BOOLEAN NOT NULL DEFAULT {bool_default(False)}"))
            if 'allow_crypto_etfs' not in existing_columns:
                conn.execute(text(f"ALTER TABLE {user_table} ADD COLUMN allow_crypto_etfs BOOLEAN NOT NULL DEFAULT {bool_default(True)}"))
            if 'allow_options_trading' not in existing_columns:
                conn.execute(text(f"ALTER TABLE {user_table} ADD COLUMN allow_options_trading BOOLEAN NOT NULL DEFAULT {bool_default(False)}"))
            if 'stripe_customer_id' not in existing_columns:
                conn.execute(text("ALTER TABLE \"user\" ADD COLUMN stripe_customer_id VARCHAR(255)"))

            if 'stripe_subscription_id' not in existing_columns:
                conn.execute(text("ALTER TABLE \"user\" ADD COLUMN stripe_subscription_id VARCHAR(255)"))

            if 'stripe_price_id' not in existing_columns:
                conn.execute(text("ALTER TABLE \"user\" ADD COLUMN stripe_price_id VARCHAR(255)"))

            if 'subscription_plan' not in existing_columns:
                conn.execute(text("ALTER TABLE \"user\" ADD COLUMN subscription_plan VARCHAR(50)"))

            if 'subscription_current_period_end' not in existing_columns:
                conn.execute(text(f"ALTER TABLE {user_table} ADD COLUMN subscription_current_period_end {datetime_type()}"))

            if 'subscription_cancel_at_period_end' not in existing_columns:
                conn.execute(text(f"ALTER TABLE {user_table} ADD COLUMN subscription_cancel_at_period_end BOOLEAN NOT NULL DEFAULT {bool_default(False)}"))

        if 'trades' in table_names:
            trade_columns = {col['name'] for col in inspector.get_columns('trades')}

            if 'exit_price' not in trade_columns:
                conn.execute(text("ALTER TABLE trades ADD COLUMN exit_price FLOAT"))

            if 'pnl' not in trade_columns:
                conn.execute(text("ALTER TABLE trades ADD COLUMN pnl FLOAT"))

            if 'pnl_source' not in trade_columns:
                conn.execute(text("ALTER TABLE trades ADD COLUMN pnl_source VARCHAR(64)"))

            if 'closed_at' not in trade_columns:
                conn.execute(text(f"ALTER TABLE trades ADD COLUMN closed_at {datetime_type()}"))


        if 'user_events' in table_names:
            user_event_columns = {col['name'] for col in inspector.get_columns('user_events')}
            if 'event_context' not in user_event_columns:
                conn.execute(text("ALTER TABLE user_events ADD COLUMN event_context TEXT"))

        if 'scans' in table_names:
            scan_columns = {col['name'] for col in inspector.get_columns('scans')}
            if 'market_day' not in scan_columns:
                conn.execute(text("ALTER TABLE scans ADD COLUMN market_day VARCHAR(20)"))
            if 'best_symbol' not in scan_columns:
                conn.execute(text("ALTER TABLE scans ADD COLUMN best_symbol VARCHAR(10)"))
            if 'best_decision' not in scan_columns:
                conn.execute(text("ALTER TABLE scans ADD COLUMN best_decision VARCHAR(20)"))
            if 'best_score' not in scan_columns:
                conn.execute(text("ALTER TABLE scans ADD COLUMN best_score INTEGER"))

        if 'market_regimes' in table_names:
            regime_columns = {col['name'] for col in inspector.get_columns('market_regimes')}
            if 'updated_at' not in regime_columns:
                conn.execute(text(f"ALTER TABLE market_regimes ADD COLUMN updated_at {datetime_type()}"))

        if 'blog_posts' in table_names:
            blog_columns = {col['name'] for col in inspector.get_columns('blog_posts')}
            blog_alters = {
                'title': "ALTER TABLE blog_posts ADD COLUMN title VARCHAR(180) NOT NULL DEFAULT ''",
                'slug': "ALTER TABLE blog_posts ADD COLUMN slug VARCHAR(220)",
                'meta_title': "ALTER TABLE blog_posts ADD COLUMN meta_title VARCHAR(220)",
                'meta_description': "ALTER TABLE blog_posts ADD COLUMN meta_description VARCHAR(320)",
                'excerpt': "ALTER TABLE blog_posts ADD COLUMN excerpt TEXT",
                'body_html': "ALTER TABLE blog_posts ADD COLUMN body_html TEXT NOT NULL DEFAULT ''",
                'target_keyword': "ALTER TABLE blog_posts ADD COLUMN target_keyword VARCHAR(180)",
                'status': "ALTER TABLE blog_posts ADD COLUMN status VARCHAR(30) NOT NULL DEFAULT 'draft'",
                'author_name': "ALTER TABLE blog_posts ADD COLUMN author_name VARCHAR(120) NOT NULL DEFAULT 'XeanVI'",
                'canonical_url': "ALTER TABLE blog_posts ADD COLUMN canonical_url VARCHAR(320)",
                'og_image': "ALTER TABLE blog_posts ADD COLUMN og_image VARCHAR(320)",
                'featured_image_alt': "ALTER TABLE blog_posts ADD COLUMN featured_image_alt VARCHAR(240)",
                'featured_image_caption': "ALTER TABLE blog_posts ADD COLUMN featured_image_caption TEXT",
                'created_at': f"ALTER TABLE blog_posts ADD COLUMN created_at {datetime_type()}",
                'updated_at': f"ALTER TABLE blog_posts ADD COLUMN updated_at {datetime_type()}",
                'published_at': f"ALTER TABLE blog_posts ADD COLUMN published_at {datetime_type()}",
            }
            for col_name, stmt in blog_alters.items():
                if col_name not in blog_columns:
                    conn.execute(text(stmt))

        if 'blog_keyword_plans' in table_names:
            keyword_plan_columns = {col['name'] for col in inspector.get_columns('blog_keyword_plans')}
            keyword_plan_alters = {
                'cluster': "ALTER TABLE blog_keyword_plans ADD COLUMN cluster VARCHAR(120)",
                'target_keyword': "ALTER TABLE blog_keyword_plans ADD COLUMN target_keyword VARCHAR(180) NOT NULL DEFAULT ''",
                'search_intent': "ALTER TABLE blog_keyword_plans ADD COLUMN search_intent VARCHAR(80) NOT NULL DEFAULT 'educational'",
                'suggested_title': "ALTER TABLE blog_keyword_plans ADD COLUMN suggested_title VARCHAR(220) NOT NULL DEFAULT ''",
                'priority': "ALTER TABLE blog_keyword_plans ADD COLUMN priority INTEGER NOT NULL DEFAULT 3",
                'linked_page': "ALTER TABLE blog_keyword_plans ADD COLUMN linked_page VARCHAR(220)",
                'status': "ALTER TABLE blog_keyword_plans ADD COLUMN status VARCHAR(40) NOT NULL DEFAULT 'planned'",
                'planned_publish_date': "ALTER TABLE blog_keyword_plans ADD COLUMN planned_publish_date DATE",
                'blog_post_id': "ALTER TABLE blog_keyword_plans ADD COLUMN blog_post_id INTEGER",
                'notes': "ALTER TABLE blog_keyword_plans ADD COLUMN notes TEXT",
                'created_at': f"ALTER TABLE blog_keyword_plans ADD COLUMN created_at {datetime_type()}",
                'updated_at': f"ALTER TABLE blog_keyword_plans ADD COLUMN updated_at {datetime_type()}",
            }
            for col_name, stmt in keyword_plan_alters.items():
                if col_name not in keyword_plan_columns:
                    conn.execute(text(stmt))
        if 'blog_publishing_plans' in table_names:
            plan_columns = {col['name'] for col in inspector.get_columns('blog_publishing_plans')}
            plan_alters = {
                'title': "ALTER TABLE blog_publishing_plans ADD COLUMN title VARCHAR(220) NOT NULL DEFAULT ''",
                'target_keyword': "ALTER TABLE blog_publishing_plans ADD COLUMN target_keyword VARCHAR(180)",
                'search_intent': "ALTER TABLE blog_publishing_plans ADD COLUMN search_intent VARCHAR(80)",
                'funnel_stage': "ALTER TABLE blog_publishing_plans ADD COLUMN funnel_stage VARCHAR(80)",
                'content_type': "ALTER TABLE blog_publishing_plans ADD COLUMN content_type VARCHAR(80)",
                'priority': "ALTER TABLE blog_publishing_plans ADD COLUMN priority INTEGER NOT NULL DEFAULT 3",
                'status': "ALTER TABLE blog_publishing_plans ADD COLUMN status VARCHAR(40) NOT NULL DEFAULT 'idea'",
                'planned_publish_date': "ALTER TABLE blog_publishing_plans ADD COLUMN planned_publish_date DATE",
                'assigned_author': "ALTER TABLE blog_publishing_plans ADD COLUMN assigned_author VARCHAR(120)",
                'notes': "ALTER TABLE blog_publishing_plans ADD COLUMN notes TEXT",
                'related_blog_post_id': "ALTER TABLE blog_publishing_plans ADD COLUMN related_blog_post_id INTEGER",
                'created_at': f"ALTER TABLE blog_publishing_plans ADD COLUMN created_at {datetime_type()}",
                'updated_at': f"ALTER TABLE blog_publishing_plans ADD COLUMN updated_at {datetime_type()}",
            }
            for col_name, stmt in plan_alters.items():
                if col_name not in plan_columns:
                    conn.execute(text(stmt))

        conn.commit()


ensure_db_initialized()

def track_user_event(event_name: str, user: User = None, context: dict = None) -> None:
    try:
        event = UserEvent(
            user_id=getattr(user, 'id', None),
            event_name=event_name,
            event_context=json.dumps(context or {}),
        )
        db.session.add(event)
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        logger.warning('Failed to track user event %s: %s', event_name, exc)


def order_outcome_from_payload(order: dict) -> str:
    status = (order.get('status') or '').lower()
    if order.get('strategy') == 'target1_then_trailing_runner':
        t1 = order.get('target_1_order') or {}
        runner = order.get('runner_order') or {}
        runner_trailing = order.get('runner_trailing_order') or {}
        if (runner_trailing.get('status') or '').lower() == 'filled':
            return 'win'
        if (runner.get('status') or '').lower() == 'filled':
            return 'breakeven_or_small_win'
        if (t1.get('status') or '').lower() == 'filled':
            return 'partial_win'
        if status in {'rejected'}:
            return 'rejected'
        if status in {'canceled', 'expired'}:
            return 'failed'
        return 'open'
    legs = order.get('legs') or []
    for leg in legs:
        leg_type = (leg.get('order_type') or '').lower()
        leg_status = (leg.get('status') or '').lower()
        if leg_type == 'limit' and leg_status == 'filled':
            return 'win'
        if leg_type == 'stop' and leg_status == 'filled':
            return 'loss'
    if status in {'rejected'}:
        return 'rejected'
    if status in {'canceled', 'expired'}:
        return 'failed'
    if status == 'filled':
        return 'working_or_filled'
    return 'open'


@app.route('/')
def index():
    if current_user.is_authenticated:
        setup_checklist = get_user_setup_checklist(current_user)
        if setup_checklist['core_complete']:
            return redirect(url_for('dashboard'))
        return redirect(url_for('setup_checklist'))

    return render_template('landing.html')


@app.route('/dev-unlock/<token>')
def dev_unlock(token):
    if config.IS_PRODUCTION:
        return "Not Found", 404
    # Check if the token matches your .env setting
    dev_bypass_token = os.getenv('DEV_BYPASS_TOKEN', '').strip()
    if dev_bypass_token and token == dev_bypass_token:
        session['dev_access'] = True
        flash("Developer access granted.", "success")
        return redirect(url_for('index'))
    return "Unauthorized", 403

@app.route('/waitlist')
@app.route('/waitlist/')
@app.route('/waitlist/thank-you')
@app.route('/waitlist/thank-you/')
@app.route('/join-waitlist', methods=['GET', 'POST'])
@app.route('/join-waitlist/', methods=['GET', 'POST'])
def legacy_waitlist_redirect():
    return redirect(url_for('signup', plan='monthly'), code=301)

@app.route('/pricing')
def pricing():
    return render_template('upgrade.html', current_user=current_user)


@app.route('/signup', methods=['GET', 'POST'])
@limiter.limit("3 per hour")  # 🛑 Blocks botnet mass-account creation
def signup():
    # Capture the plan from the URL parameter (?plan=monthly)
    intended_plan = request.args.get('plan')

    if request.method == 'POST':
        tos_accepted = request.form.get('tos_agreement')
        if not tos_accepted:
            flash('You must agree to the technical execution terms to continue.', 'error')
            return redirect(url_for('signup', plan=intended_plan))

        email = (request.form.get('email') or '').strip().lower()
        password = request.form.get('password')

        if not is_valid_email(email):
            flash('Please enter a valid email address.', 'error')
            return redirect(url_for('signup', plan=intended_plan))

        # Check if user exists
        if User.query.filter_by(email=email).first():
            flash('An account with that email already exists.', 'error')
            return redirect(url_for('signup', plan=intended_plan))

        # Create the user
        new_user = User(
            email=email,
            password_hash=generate_password_hash(password, method='pbkdf2:sha256'),
            full_name=request.form.get('full_name'),
            subscription_status='free',  # Starts free until payment clears
        )
        db.session.add(new_user)
        db.session.commit()
        track_user_event('signup.completed', user=new_user, context={'plan': intended_plan or ''})
        add_signup_user_to_brevo(new_user)
        update_brevo_contact_attributes(new_user, get_user_brevo_funnel_attributes(new_user))
        login_user(new_user)

        # REDIRECT LOGIC: If they chose a plan, send them to upgrade first
        if intended_plan in ['monthly', 'annual']:
            return redirect(url_for('upgrade', plan=intended_plan, signup_success='1'))

        # Otherwise, send them to standard onboarding
        return redirect(url_for('onboarding'))

    return render_template('signup.html')

@app.route('/login', methods=['GET', 'POST'])
@limiter.limit("5 per minute")
def login():
    if request.method == 'POST':
        try:
            email = request.form.get('email')
            password = request.form.get('password')

            user = User.query.filter_by(email=email).first()

            if not user or not check_password_hash(user.password_hash, password):
                flash('Invalid email or password', 'error')
                return redirect(url_for('login'))

            login_user(user)
            return redirect(url_for('dashboard'))

        except Exception as e:
            logger.error(f"Login failure for {email}: {str(e)}")
            flash("An internal authentication error occurred. Please try again.", 'error')
            return redirect(url_for('login'))

    return render_template('login.html')






@app.route('/forgot-password', methods=['GET', 'POST'])
@app.route('/reset_password', methods=['GET', 'POST'])
@limiter.limit("5 per hour")
def forgot_password():
    """
    Step 1:
    User enters email.
    If account exists, send password reset email.
    Always show the same response to avoid exposing whether an email exists.
    """
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        email = (request.form.get('email') or '').strip().lower()

        if email:
            user = User.query.filter_by(email=email).first()

            if user:
                reset_url = build_password_reset_url(user)
                send_password_reset_email(user, reset_url)

        flash(
            'If that email exists in our system, a password reset link has been sent.',
            'success',
        )
        return redirect(url_for('forgot_password'))

    return render_template('forgot_password.html')


@app.route('/reset-password/<token>', methods=['GET', 'POST'])
@limiter.limit("10 per hour")
def reset_password_with_token(token):
    """
    Step 2:
    User clicks secure email link and sets a new password.
    """
    if current_user.is_authenticated:
        logout_user()

    user = verify_password_reset_token(token)

    if not user:
        flash('That password reset link is invalid or expired. Please request a new one.', 'error')
        return redirect(url_for('forgot_password'))

    if request.method == 'POST':
        password = request.form.get('password') or ''
        confirm_password = request.form.get('confirm_password') or ''

        if len(password) < 8:
            flash('Your new password must be at least 8 characters long.', 'error')
            return redirect(url_for('reset_password_with_token', token=token))

        if password != confirm_password:
            flash('Passwords do not match. Please try again.', 'error')
            return redirect(url_for('reset_password_with_token', token=token))

        user.password_hash = generate_password_hash(password, method='pbkdf2:sha256')
        db.session.commit()

        flash('Your password has been updated. You can now log in.', 'success')
        return redirect(url_for('login'))

    return render_template('reset_password.html', token=token)



@app.route('/lp/rule-based-trading-automation')
def paid_ads_landing():
    return render_template('paid_ads_landing.html')

@app.route('/features')
def features():
    return render_template('features.html')

@app.route('/playbook')
def playbook():
    """Public strategy page explaining the 'Screen, Validate, Execute' workflow."""
    if current_user.is_authenticated and not current_user.playbook_reviewed:
        current_user.playbook_reviewed = True
        db.session.commit()
    return render_template('playbook.html')


@app.route('/broker-integration')
def broker_integration():
    return render_template('broker_integration.html')


@app.route('/trading-automation')
def trading_automation():
    return render_template('trading_automation.html')


@app.route('/terms')
def terms():
    return render_template('terms.html')


@app.route('/privacy')
def privacy():
    return render_template('privacy.html')


@app.route('/about')
def about():
    return render_template('about.html')

@app.route('/contact')
def contact():
    return render_template('contact.html')



@app.route('/blog')
def blog_index():
    posts = BlogPost.query.filter_by(status='published').order_by(BlogPost.published_at.desc(), BlogPost.created_at.desc()).all()
    return render_template('blog_index.html', posts=posts)


@app.route('/blog/<slug>')
def blog_post(slug):
    post = BlogPost.query.filter_by(slug=slug, status='published').first()
    if post is None:
        # Backward-compatible fallback for legacy links that may still match a stored canonical_url.
        fallback_path = f"/blog/{slug}"
        post = BlogPost.query.filter(
            BlogPost.status == 'published',
            BlogPost.canonical_url.isnot(None),
            BlogPost.canonical_url.endswith(fallback_path)
        ).first_or_404()

    if slug != post.slug:
        return redirect(url_for('blog_post', slug=post.slug), code=301)

    canonical_url = build_blog_canonical_url(post.slug)
    base_url = get_public_base_url()
    og_image_absolute = ""
    if post.og_image:
        og_image_absolute = post.og_image if post.og_image.startswith(('http://', 'https://')) else f"{base_url}{post.og_image}"
    return render_template('blog_post.html', post=post, canonical_url=canonical_url, base_url=base_url, og_image_absolute=og_image_absolute)


@app.route('/sitemap.xml')
def sitemap_xml():
    """Generates the XML sitemap for search engines using an explicit public allowlist."""
    links = []
    public_paths = [
        '/',
        '/features',
        '/pricing',
        '/playbook',
        '/broker-integration',
        '/trading-automation',
        '/transparency',
        '/terms',
        '/privacy',
        '/contact',
        '/blog',
    ]

    base_url = get_public_base_url()

    existing_rules = {rule.rule for rule in app.url_map.iter_rules() if "GET" in rule.methods}
    for path in public_paths:
        if path in existing_rules:
            links.append((f"{base_url}{path}", datetime.now().strftime('%Y-%m-%d')))
    blog_posts = BlogPost.query.filter_by(status='published').order_by(BlogPost.updated_at.desc()).all()
    for post in blog_posts:
        lastmod_dt = post.updated_at or post.published_at or post.created_at or datetime.now(timezone.utc)
        links.append((f"{base_url}/blog/{post.slug}", lastmod_dt.strftime('%Y-%m-%d')))

    # Build the XML structure
    sitemap_xml_content = render_template('sitemap_xml.xml', links=links)
    response = make_response(sitemap_xml_content)
    response.headers["Content-Type"] = "application/xml"
    return response


@app.route('/robots.txt')
def robots_txt():
    """Updated to point to the new XML sitemap."""
    base_url = get_public_base_url()
    lines = [
        "User-agent: *",
        "Disallow: /dashboard",
        "Disallow: /admin/",
        "Disallow: /api/",
        "Disallow: /settings",
        "Disallow: /onboarding",
        "Disallow: /setup-checklist",
        "Disallow: /logout",
        "Disallow: /alpaca/",
        f"Sitemap: {base_url}/sitemap.xml",
    ]
    return "\n".join(lines), 200, {'Content-Type': 'text/plain'}


@app.route('/healthz')
def healthz():
    return jsonify({'ok': True}), 200


@app.route('/readyz')
def readyz():
    try:
        db.session.execute(text('SELECT 1'))
        redis_client.ping()
    except Exception:
        logger.exception('Readiness check failed')
        return jsonify({'ok': False}), 503
    return jsonify({'ok': True}), 200



@app.route('/learn')
def learn_gone():
    return ("", 410)


@app.route('/learn/<path:slug>')
def learn_topic_gone(slug):
    return ("", 410)


@app.route('/articles')
def articles_gone():
    return ("", 410)


@app.route('/articles/<path:slug>')
def article_topic_gone(slug):
    return ("", 410)

@app.route('/transparency')
def transparency():
    # In a fully fleshed-out app, you might pass dynamic backtest stats here
    # from your analyze_performance.py script. For now, we render the hub.
    if current_user.is_authenticated and not current_user.transparency_reviewed:
        current_user.transparency_reviewed = True
        db.session.commit()
    return render_template('transparency.html', current_user=current_user)


@app.route('/transparency/reviewed', methods=['POST'])
@login_required
def transparency_reviewed():
    current_user.transparency_reviewed = True
    db.session.commit()
    return ok({'tracked': True})


@app.route('/api/transparency/stats')
def api_transparency_stats():
    """Serves the pre-calculated backtest performance metrics."""
    report_path = os.path.join(app.root_path, 'static', 'performance_report.json')
    try:
        with open(report_path, 'r') as f:
            data = json.load(f)
        return ok(data)
    except FileNotFoundError:
        return fail("Performance report is currently generating. Please check back shortly.", 404)


@app.route('/logout', methods=['POST'])
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))



def user_has_alpaca_paper_connection(user: User) -> bool:
    # Keep this strict to avoid false positives from active-mode legacy accessors.
    # Legacy single-token users should reconnect through onboarding so paper/live
    # tokens are separated explicitly.
    return bool(
        getattr(user, 'alpaca_paper_account_id', None)
        or getattr(user, 'alpaca_paper_access_token', None)
    )


def user_has_alpaca_live_connection(user: User) -> bool:
    return bool(
        getattr(user, 'alpaca_live_account_id', None)
        or getattr(user, 'alpaca_live_access_token', None)
    )


def live_mode_unlocked(user: User) -> bool:
    return bool(onboarding_requirements_met(user) and user_has_alpaca_live_connection(user))


def onboarding_requirements_met(user: User) -> bool:
    return bool(
        user_has_alpaca_paper_connection(user)
        and user_has_alpaca_live_connection(user)
        and getattr(user, 'paper_bankroll_set', False)
        and (getattr(user, 'paper_bankroll', 0) or 0) > 0
    )



def user_is_pro(user):
    return (getattr(user, 'subscription_status', '') or '').lower() == 'pro'


def get_plan_access(user):
    is_pro = user_is_pro(user)

    return {
        'is_pro': is_pro,
        'plan_label': 'PRO ACTIVE' if is_pro else 'FREE PREVIEW',
        'run_mode': 'full_automation' if is_pro else 'scan_preview',
        'can_run_scan_preview': True,
        'can_activate_auto_workflow': is_pro,
        'can_route_paper_orders': is_pro,
        'can_monitor_orders': is_pro,
        'can_save_trade_logs': is_pro,
        'upgrade_url': url_for('upgrade', **{'from': 'scan_preview'}) if not is_pro else None,
    }


def format_subscription_status(user: User) -> dict:
    raw_status = (getattr(user, 'subscription_status', '') or '').lower()
    subscription_plan = (getattr(user, 'subscription_plan', '') or '').lower()
    period_end = getattr(user, 'subscription_current_period_end', None)
    has_stripe_customer = bool(getattr(user, 'stripe_customer_id', None))
    has_active_subscription = raw_status in {'pro', 'past_due'}
    cancel_at_period_end = bool(getattr(user, 'subscription_cancel_at_period_end', False))

    if raw_status == 'pro' and subscription_plan == 'annual':
        plan_label = 'Annual PRO'
    elif raw_status == 'pro' and subscription_plan == 'monthly':
        plan_label = 'Monthly PRO'
    elif raw_status == 'past_due':
        plan_label = 'PRO Payment Past Due'
    else:
        plan_label = 'Free Preview'

    if raw_status == 'pro':
        status_label = 'Active'
        status_class = 'success'
    elif raw_status == 'past_due':
        status_label = 'Payment Past Due'
        status_class = 'warning'
    else:
        status_label = 'Free'
        status_class = 'muted'

    current_period_end_label = period_end.strftime('%b %d, %Y') if period_end else None

    return {
        'plan_label': plan_label,
        'status_label': status_label,
        'status_class': status_class,
        'current_period_end_label': current_period_end_label,
        'cancel_at_period_end': cancel_at_period_end,
        'has_stripe_customer': has_stripe_customer,
        'has_active_subscription': has_active_subscription,
    }

def get_user_setup_checklist(user: User) -> dict:
    paper_items = [
        {
            'field': 'alpaca_paper_connected',
            'label': 'Connect Alpaca Paper Account',
            'short_label': 'Alpaca Paper',
            'description': 'Connect your Alpaca paper account so paper-mode routing, monitoring, and workflow checks can run with your own broker credentials.',
            'completed': bool(getattr(user, 'alpaca_paper_access_token', None) or getattr(user, 'alpaca_paper_account_id', None)),
            'required': True,
            'status': 'Complete' if bool(getattr(user, 'alpaca_paper_access_token', None) or getattr(user, 'alpaca_paper_account_id', None)) else 'Action Needed',
            'url': url_for('onboarding'),
            'action_label': 'Connect Paper Account',
            'completed_action_label': 'Review Paper Connection',
            'completed_note': 'Paper account connected.',
            'icon': 'fa-plug',
        },
        {
            'field': 'paper_bankroll_set',
            'label': 'Configure Paper Bankroll',
            'short_label': 'Paper Money',
            'description': 'Set your starting paper bankroll so XeanVI can calculate simulated position sizing and paper-mode risk controls.',
            'completed': bool(user.paper_bankroll_set and (user.paper_bankroll or 0) > 0),
            'required': True,
            'status': 'Complete' if bool(user.paper_bankroll_set and (user.paper_bankroll or 0) > 0) else 'Required',
            'url': url_for('onboarding'),
            'action_label': 'Configure Paper Bankroll',
            'completed_action_label': 'Update Paper Bankroll',
            'completed_note': 'Paper bankroll saved.',
            'icon': 'fa-wallet',
        },
        {
            'field': 'first_scan_completed',
            'label': 'Run First Paper Scan',
            'short_label': 'First Scan',
            'description': 'Run a paper scan to validate your dashboard workflow before using real capital. This is recommended for onboarding confidence.',
            'completed': bool(getattr(user, 'first_scan_completed', False) or getattr(user, 'scan_preview_completed', False)),
            'required': False,
            'status': 'Complete' if bool(getattr(user, 'first_scan_completed', False) or getattr(user, 'scan_preview_completed', False)) else 'Recommended',
            'url': url_for('dashboard'),
            'action_label': 'Run First Paper Scan',
            'completed_action_label': 'Review First Paper Scan',
            'completed_note': 'First paper scan complete.',
            'icon': 'fa-radar',
        },
    ]

    raw_subscription_status = (getattr(user, 'subscription_status', '') or '').lower()
    live_ready = bool(raw_subscription_status in {'pro', 'past_due'})
    live_access_label = 'active' if raw_subscription_status == 'pro' else 'active (grace period)'
    live_risk_configured = bool((getattr(user, 'live_bankroll', 0) or 0) > 0)
    live_items = [
        {
        'field': 'live_plan_access',
        'label': 'Live Plan Access',
        'short_label': 'Plan Access',
        'description': 'Live broker connection requires active PRO access (including payment grace-period access when applicable).',
        'completed': live_ready,
        'required': True,
        'status': 'Complete' if live_ready else 'Required',
        'url': url_for('pricing') if not live_ready else url_for('billing'),
        'action_label': 'Upgrade for Live Access',
        'completed_action_label': 'Manage Billing',
        'completed_note': f'Live plan access is {live_access_label}.',
        'icon': 'fa-shield-halved',
        },
        {
        'field': 'alpaca_live_connected',
        'label': 'Connect Alpaca Live Account',
        'short_label': 'Alpaca Live',
        'description': 'Connect your live Alpaca account when you are ready for broker-linked live setup. Live execution stays protected by backend safety gates.',
        'completed': bool(getattr(user, 'alpaca_live_account_id', None) or getattr(user, 'alpaca_live_access_token', None)),
        'required': True,
        'status': 'Complete' if bool(getattr(user, 'alpaca_live_account_id', None) or getattr(user, 'alpaca_live_access_token', None)) else 'Action Needed',
        'url': url_for('onboarding'),
        'action_label': 'Connect Live Account',
        'completed_action_label': 'Review Live Connection',
        'completed_note': 'Live account connected.',
        'icon': 'fa-plug',
        },
        {
        'field': 'live_risk_controls',
        'label': 'Configure Live Risk Controls',
        'short_label': 'Risk Controls',
        'description': 'Set a positive live bankroll baseline so live-mode position sizing and risk checks use live account equity.',
        'completed': live_risk_configured,
        'required': True,
        'status': 'Complete' if live_risk_configured else 'Required',
        'url': url_for('onboarding'),
        'action_label': 'Configure Live Risk Controls',
        'completed_action_label': 'Review Risk Controls',
        'completed_note': 'Live bankroll baseline is configured.',
        'icon': 'fa-triangle-exclamation',
        },
    ]

    recommended_items = [
        {
            'field': 'playbook_reviewed',
            'label': 'Review Trading Playbook',
            'description': 'Recommended: review playbook and setup criteria documentation.',
            'completed': bool(user.playbook_reviewed),
            'status': 'Complete' if bool(user.playbook_reviewed) else 'Recommended',
            'url': url_for('playbook'),
            'action_label': 'Review Trading Playbook',
            'icon': 'fa-book-open',
        },
        {
            'field': 'transparency_reviewed',
            'label': 'Review AI Logic',
            'description': 'Recommended: review model logic and transparency notes.',
            'completed': bool(user.transparency_reviewed),
            'status': 'Complete' if bool(user.transparency_reviewed) else 'Recommended',
            'url': url_for('transparency'),
            'action_label': 'Review Recommended Safety Notes',
            'icon': 'fa-circle-info',
        },
    ]

    items = list(paper_items) + list(live_items)
    total_required = sum(1 for item in items if item['required'])
    completed_required = sum(1 for item in items if item['required'] and item['completed'])
    percent_complete = int(round((completed_required / total_required) * 100)) if total_required else 0
    core_complete = completed_required == total_required
    return {
        'paper_items': paper_items,
        'live_items': live_items,
        'recommended_items': recommended_items,
        'items': items,
        'completed_required': completed_required,
        'total_required': total_required,
        'percent_complete': percent_complete,
        'core_complete': core_complete,
    }


@app.route('/setup-checklist')
@login_required
def setup_checklist():
    track_user_event('setup_checklist_viewed', user=current_user)
    checklist = get_user_setup_checklist(current_user)
    return render_template('setup_checklist.html', current_user=current_user, setup_checklist=checklist)



@app.route('/dashboard')
@login_required
def dashboard():
    track_user_event('dashboard_viewed', user=current_user)
    # Clean, quiet entry into the command center
    latest_regime = trade_db.get_current_market_regime() or {}
    market_regime_status = (latest_regime.get('regime_status') or 'normal').lower()
    checklist = get_user_setup_checklist(current_user)
    return render_template(
        'dashboard.html',
        current_user=current_user,
        market_regime_status=market_regime_status,
        setup_checklist=checklist,
        plan_access=get_plan_access(current_user),
    )


@app.route('/upgrade')
@login_required
def upgrade():
    return redirect(url_for('pricing'), code=302)


@app.route('/billing')
@login_required
def billing():
    billing_status = format_subscription_status(current_user)
    track_user_event('billing_page_viewed', user=current_user)
    return render_template('billing.html', current_user=current_user, billing_status=billing_status)


def get_stripe_price_for_plan(plan: str) -> str | None:
    if plan == 'monthly':
        return config.STRIPE_PRICE_ID_MONTHLY
    if plan == 'annual':
        return config.STRIPE_PRICE_ID_ANNUAL
    return None


def get_plan_from_price_id(price_id: str | None) -> str:
    if not price_id:
        return 'unknown'
    if price_id == config.STRIPE_PRICE_ID_MONTHLY:
        return 'monthly'
    if price_id == config.STRIPE_PRICE_ID_ANNUAL:
        return 'annual'
    return 'unknown'


def get_or_create_stripe_customer(user: User) -> str:
    if user.stripe_customer_id:
        return user.stripe_customer_id

    if not is_valid_email(user.email):
        logger.error('Stripe customer creation blocked: invalid email for user_id=%s email=%s', user.id, user.email)
        raise ValueError('Invalid account email. Please update your email before checkout.')

    customer = stripe.Customer.create(
        email=user.email,
        name=user.full_name or user.email,
        metadata={'xeanvi_user_id': str(user.id)},
    )
    user.stripe_customer_id = customer.id
    db.session.commit()
    return customer.id


def apply_subscription_to_user(user: User, subscription: dict, price_id: str | None = None) -> None:
    status = (subscription.get('status') or '').lower()
    status_map = {
        'active': 'pro',
        'trialing': 'pro',
        'past_due': 'past_due',
        'canceled': 'free',
        'unpaid': 'free',
        'incomplete_expired': 'free',
    }
    mapped_status = status_map.get(status)
    if mapped_status:
        user.subscription_status = mapped_status

    user.stripe_subscription_id = subscription.get('id') or user.stripe_subscription_id
    resolved_price_id = price_id
    if not resolved_price_id:
        items = (subscription.get('items') or {}).get('data') or []
        if items and items[0].get('price'):
            resolved_price_id = items[0]['price'].get('id')
    user.stripe_price_id = resolved_price_id
    user.subscription_plan = get_plan_from_price_id(resolved_price_id)

    period_end = subscription.get('current_period_end')
    user.subscription_current_period_end = datetime.utcfromtimestamp(period_end) if period_end else None
    user.subscription_cancel_at_period_end = bool(subscription.get('cancel_at_period_end', False))

    if (user.subscription_status or '').lower() == 'pro':
        update_brevo_contact_attributes(
            user,
            {
                'IS_PRO': True,
                'SUBSCRIPTION_STATUS': 'pro',
                'SETUP_CHECKLIST_COMPLETED': get_user_setup_checklist(user)['core_complete'],
            },
        )




def get_launch_promo_discounts(plan: str) -> list[dict[str, str]]:
    if plan != 'monthly':
        return []
    if not config.LAUNCH_PROMO_ENABLED:
        return []
    if not config.LAUNCH_PROMO_STRIPE_COUPON_ID:
        return []
    return [{'coupon': config.LAUNCH_PROMO_STRIPE_COUPON_ID}]

@app.route('/api/create-checkout-session', methods=['POST'])
@login_required
def create_checkout_session():
    stripe_missing = config.validate_stripe_config()
    if stripe_missing:
        logger.error('Stripe checkout blocked due to missing configuration keys: %s', ",".join(stripe_missing))
        flash('Billing is temporarily unavailable. Please contact support.', 'error')
        return redirect(url_for('upgrade'))
    body = request.get_json(silent=True) or {}
    plan = (request.form.get('plan') or body.get('plan') or '').strip().lower()

    if plan not in {'monthly', 'annual'}:
        flash('Invalid subscription plan selected.', 'error')
        return redirect(url_for('upgrade'))

    price_id = get_stripe_price_for_plan(plan)
    if not price_id:
        flash('Billing is temporarily unavailable. Please contact support.', 'error')
        return redirect(url_for('upgrade'))

    try:
        stripe_customer_id = get_or_create_stripe_customer(current_user)
        promo_discounts = get_launch_promo_discounts(plan)
        launch_promo_value = 'two_months_for_one' if promo_discounts else 'none'
        checkout_kwargs = {
            'mode': 'subscription',
            'customer': stripe_customer_id,
            'line_items': [{'price': price_id, 'quantity': 1}],
            'success_url': f"{config.APP_BASE_URL}/checkout-redirect?session_id={{CHECKOUT_SESSION_ID}}",
            'cancel_url': f"{config.APP_BASE_URL}/upgrade?checkout=cancelled",
            'client_reference_id': str(current_user.id),
            'metadata': {'user_id': str(current_user.id), 'plan': plan, 'launch_promo': launch_promo_value},
            'subscription_data': {'metadata': {'user_id': str(current_user.id), 'plan': plan, 'launch_promo': launch_promo_value}},
        }
        if promo_discounts:
            checkout_kwargs['discounts'] = promo_discounts
        else:
            checkout_kwargs['allow_promotion_codes'] = True
        checkout_session = stripe.checkout.Session.create(**checkout_kwargs)
        track_user_event('checkout.started', user=current_user, context={
            'stripe_session_id': checkout_session.get('id'),
            'price_id': price_id,
            'plan': plan,
            'source_route': '/api/create-checkout-session',
            'created_at': datetime.utcnow().isoformat(),
        })
        return redirect(checkout_session.url, code=303)
    except ValueError as exc:
        flash(str(exc), 'error')
        return redirect(url_for('settings'))
    except Exception:
        logger.exception('Stripe checkout session creation failed for user_id=%s', current_user.id)
        flash('Unable to start checkout right now. Please try again shortly.', 'error')
        return redirect(url_for('upgrade'))



@app.route('/api/create-billing-portal-session', methods=['POST'])
@login_required
def create_billing_portal_session():
    if not config.STRIPE_SECRET_KEY:
        flash('Billing is temporarily unavailable. Please contact support.', 'error')
        return redirect(url_for('billing'))

    if not current_user.stripe_customer_id:
        flash('No Stripe billing profile exists yet. Upgrade to PRO to create one.', 'error')
        return redirect(url_for('upgrade'))

    return_path = getattr(config, 'STRIPE_CUSTOMER_PORTAL_RETURN_PATH', '/billing') or '/billing'
    return_path = return_path if return_path.startswith('/') else '/billing'
    return_url = f"{config.APP_BASE_URL}{return_path}"

    try:
        portal_session = stripe.billing_portal.Session.create(
            customer=current_user.stripe_customer_id,
            return_url=return_url,
        )
        track_user_event('billing_portal_started', user=current_user)
        return redirect(portal_session.url, code=303)
    except Exception:
        logger.exception('Stripe billing portal session failed for user_id=%s', current_user.id)
        flash('Unable to open billing portal right now. Please try again shortly.', 'error')
        return redirect(url_for('billing'))


@app.route('/checkout-redirect')
@login_required
def checkout_redirect():
    session_id = (request.args.get('session_id') or '').strip()
    if not session_id:
        flash('Missing checkout session reference.', 'error')
        return redirect(url_for('upgrade'))

    try:
        checkout_session = stripe.checkout.Session.retrieve(session_id)
    except Exception:
        logger.exception('Failed retrieving checkout session %s', session_id)
        flash('Unable to verify checkout status. Please contact support if you were charged.', 'error')
        return redirect(url_for('upgrade'))

    session_user_id = (checkout_session.get('metadata') or {}).get('user_id') or checkout_session.get('client_reference_id')
    if str(session_user_id) != str(current_user.id):
        flash('Checkout verification failed for this account.', 'error')
        return redirect(url_for('upgrade'))

    subscription_id = checkout_session.get('subscription')
    paid = checkout_session.get('payment_status') == 'paid'
    if not (subscription_id and paid):
        flash('Payment is not completed yet. Please try again shortly.', 'error')
        return redirect(url_for('upgrade'))

    try:
        subscription = stripe.Subscription.retrieve(subscription_id)
        price_id = None
        line_items = checkout_session.get('line_items', {}).get('data', []) if checkout_session.get('line_items') else []
        if line_items and line_items[0].get('price'):
            price_id = line_items[0]['price'].get('id')
        apply_subscription_to_user(current_user, subscription, price_id=price_id)
        if checkout_session.get('customer'):
            current_user.stripe_customer_id = checkout_session.get('customer')
        db.session.commit()
        update_brevo_contact_attributes(current_user, {'IS_PRO': True, 'SUBSCRIPTION_STATUS': 'pro', 'SETUP_CHECKLIST_COMPLETED': get_user_setup_checklist(current_user)['core_complete']})
        flash('Your PRO subscription is active. Welcome to XeanVI PRO.', 'success')
        return redirect(url_for('setup_checklist'))
    except Exception:
        db.session.rollback()
        logger.exception('Checkout finalization failed for user_id=%s session_id=%s', current_user.id, session_id)
        flash('We could not finalize your subscription yet. Support has been notified.', 'error')
        return redirect(url_for('upgrade'))


@app.route('/api/stripe-webhook', methods=['POST'])
@csrf.exempt
def stripe_webhook():
    payload = request.get_data(cache=False, as_text=False)
    sig_header = request.headers.get('Stripe-Signature')
    endpoint_secret = config.STRIPE_WEBHOOK_SECRET

    if not endpoint_secret or not sig_header:
        return jsonify({'error': 'invalid webhook configuration'}), 400

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, endpoint_secret)
    except (ValueError, stripe.error.SignatureVerificationError):
        return jsonify({'error': 'invalid signature'}), 400

    event_type = event.get('type')
    event_id = event.get('id')
    obj = (event.get('data') or {}).get('object') or {}

    def find_user(subscription_id=None, customer_id=None, reference_user_id=None, customer_email=None):
        user = None
        if subscription_id:
            user = User.query.filter_by(stripe_subscription_id=subscription_id).first()
        if not user and reference_user_id:
            user = db.session.get(User, int(reference_user_id)) if str(reference_user_id).isdigit() else None
        if not user and customer_id:
            user = User.query.filter_by(stripe_customer_id=customer_id).first()
        if not user and customer_email:
            user = User.query.filter_by(email=(customer_email or '').strip().lower()).first()
        return user

    if event_id and StripeEvent.query.filter_by(event_id=event_id).first():
        return jsonify({'status': 'duplicate'}), 200

    try:
        if event_type == 'checkout.session.completed':
            reference_user_id = obj.get('client_reference_id') or (obj.get('metadata') or {}).get('user_id')
            user = find_user(customer_id=obj.get('customer'), reference_user_id=reference_user_id, customer_email=obj.get('customer_email'))
            subscription_id = obj.get('subscription')
            if user and subscription_id:
                subscription = stripe.Subscription.retrieve(subscription_id)
                prior_state = (user.subscription_status, user.stripe_subscription_id, user.stripe_price_id)
                apply_subscription_to_user(user, subscription)
                user.stripe_customer_id = obj.get('customer') or user.stripe_customer_id
                _ = prior_state
                status = (user.subscription_status or '').lower()
                if status in {'pro'}:
                    update_brevo_contact_attributes(user, {'IS_PRO': True, 'SUBSCRIPTION_STATUS': 'pro'})
                    track_user_event('invoice.paid', user=user, context={'subscription_id_masked': mask_identifier(sub_id or '')})
                elif status == 'past_due':
                    update_brevo_contact_attributes(user, {'IS_PRO': False, 'SUBSCRIPTION_STATUS': 'past_due'})
                track_user_event('checkout.completed', user=user, context={'subscription_id_masked': mask_identifier(subscription_id), 'customer_id_masked': mask_identifier(obj.get('customer')), 'session_id_masked': mask_identifier(obj.get('id'))})


        elif event_type == 'checkout.session.expired':
            reference_user_id = obj.get('client_reference_id') or (obj.get('metadata') or {}).get('user_id')
            user = find_user(customer_id=obj.get('customer'), reference_user_id=reference_user_id, customer_email=obj.get('customer_email'))
            if user:
                track_user_event('checkout.expired', user=user, context={'session_id_masked': mask_identifier(obj.get('id')), 'customer_id_masked': mask_identifier(obj.get('customer'))})
            else:
                logger.warning('checkout.session.expired received without matching user reference_user_id=%s', reference_user_id)

        elif event_type in {'customer.subscription.created', 'customer.subscription.updated'}:
            reference_user_id = (obj.get('metadata') or {}).get('user_id')
            user = find_user(subscription_id=obj.get('id'), customer_id=obj.get('customer'), reference_user_id=reference_user_id, customer_email=obj.get('customer_email'))
            if user:
                prior_state = (user.subscription_status, user.stripe_subscription_id, user.stripe_price_id)
                apply_subscription_to_user(user, obj)
                _ = prior_state
                status = (user.subscription_status or '').lower()
                if status in {'pro', 'trialing'}:
                    update_brevo_contact_attributes(user, {'IS_PRO': True, 'SUBSCRIPTION_STATUS': 'pro'})
                    track_user_event('invoice.paid', user=user, context={'subscription_id_masked': mask_identifier(sub_id or '')})
                elif status == 'past_due':
                    update_brevo_contact_attributes(user, {'IS_PRO': False, 'SUBSCRIPTION_STATUS': 'past_due'})

        elif event_type == 'customer.subscription.deleted':
            reference_user_id = (obj.get('metadata') or {}).get('user_id')
            user = find_user(subscription_id=obj.get('id'), customer_id=obj.get('customer'), reference_user_id=reference_user_id, customer_email=obj.get('customer_email'))
            if user:
                user.subscription_status = 'free'
                user.subscription_cancel_at_period_end = False
                update_brevo_contact_attributes(user, {'IS_PRO': False, 'SUBSCRIPTION_STATUS': 'free'})
                track_user_event('subscription.deleted', user=user, context={'subscription_id_masked': mask_identifier(obj.get('id') or '')})

        elif event_type == 'invoice.payment_failed':
            sub_id = obj.get('subscription')
            customer_id = obj.get('customer')
            reference_user_id = (obj.get('metadata') or {}).get('user_id')
            user = find_user(subscription_id=sub_id, customer_id=customer_id, reference_user_id=reference_user_id, customer_email=obj.get('customer_email'))
            if user:
                user.subscription_status = 'past_due'
                update_brevo_contact_attributes(user, {'IS_PRO': False, 'SUBSCRIPTION_STATUS': 'past_due'})
                track_user_event('invoice.payment_failed', user=user, context={'subscription_id_masked': mask_identifier(sub_id or '')})

        elif event_type == 'invoice.paid':
            sub_id = obj.get('subscription')
            customer_id = obj.get('customer')
            reference_user_id = (obj.get('metadata') or {}).get('user_id')
            user = find_user(subscription_id=sub_id, customer_id=customer_id, reference_user_id=reference_user_id, customer_email=obj.get('customer_email'))
            if user and sub_id:
                subscription = stripe.Subscription.retrieve(sub_id)
                if subscription.get('status') in {'active', 'trialing'}:
                    apply_subscription_to_user(user, subscription)
                    user.subscription_status = 'pro'
                    update_brevo_contact_attributes(user, {'IS_PRO': True, 'SUBSCRIPTION_STATUS': 'pro'})
                    track_user_event('invoice.paid', user=user, context={'subscription_id_masked': mask_identifier(sub_id or '')})
        if event_id:
            db.session.add(StripeEvent(event_id=event_id, event_type=event_type or 'unknown'))
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({'status': 'duplicate'}), 200
    except Exception:
        db.session.rollback()
        logger.exception('Error handling Stripe webhook event type=%s', event_type)
        return jsonify({'error': 'processing_failed'}), 500

    return jsonify({'status': 'success'}), 200




@app.route('/onboarding', methods=['GET', 'POST'])
@login_required
def onboarding():
    setup_checklist = get_user_setup_checklist(current_user)

    if request.method == 'POST':
        if not request.form.get('risk_ack'):
            flash('You must acknowledge the trading risk before continuing.', 'error')
            return redirect(url_for('onboarding'))

        try:
            starting_bankroll = float(request.form.get('bankroll', 5000.0))
        except (TypeError, ValueError):
            flash('Enter a valid starting paper bankroll amount.', 'error')
            return redirect(url_for('onboarding'))

        current_user.trading_mode = 'paper'
        current_user.paper_bankroll = starting_bankroll
        current_user.bankroll = starting_bankroll
        current_user.paper_bankroll_set = starting_bankroll > 0
        current_user.onboarding_completed = onboarding_requirements_met(current_user)
        db.session.commit()
        track_user_event('onboarding_completed', user=current_user, context={'starting_bankroll': starting_bankroll})

        if current_user.onboarding_completed:
            flash('Onboarding complete. Paper and Live accounts are connected.', 'success')
        else:
            flash('Paper-mode risk setup saved. Connect Alpaca Live to finish onboarding and unlock LIVE mode.', 'success')
        return redirect(url_for('setup_checklist'))

    return render_template(
        'onboarding.html',
        current_user=current_user,
        setup_checklist=setup_checklist,
    )

@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    scanner_health = build_scanner_effectiveness_report(user=current_user, limit=1)
    if request.method == 'POST':
        # 1. Update Core Settings
        new_bankroll = float(request.form.get('bankroll', 0.0))
        if current_user.trading_mode == 'live':
            current_user.live_bankroll = new_bankroll
        else:
            current_user.paper_bankroll = new_bankroll
            current_user.paper_bankroll_set = new_bankroll > 0
            current_user.onboarding_completed = onboarding_requirements_met(current_user)
        current_user.sync_legacy_bankroll_from_active_mode()
        refresh_interval = int(request.form.get('refresh_interval', 30000))
        current_user.refresh_interval = (
            refresh_interval if refresh_interval in VALID_REFRESH_INTERVALS else 30000
        )

        # 2. Update ESG & Personalization Filters
        if hasattr(current_user, 'esg_fossil_fuels'):
            current_user.esg_fossil_fuels = 'esg_fossil_fuels' in request.form
            current_user.esg_weapons = 'esg_weapons' in request.form
            current_user.esg_tobacco = 'esg_tobacco' in request.form
            current_user.exclude_penny_stocks = 'exclude_penny_stocks' in request.form
            current_user.exclude_biotech = 'exclude_biotech' in request.form

        db.session.commit()
        flash('Settings and Risk Parameters saved successfully.', 'success')
        return redirect(url_for('dashboard'))

    return render_template(
        'settings.html',
        current_user=current_user,
        paper_connected=user_has_alpaca_paper_connection(current_user),
        live_connected=user_has_alpaca_live_connection(current_user),
        live_unlocked=live_mode_unlocked(current_user),
        scanner_health=scanner_health,
    )

@app.route('/alpaca/login')
@login_required
def alpaca_login():
    oauth_env = (request.args.get('env') or 'paper').strip().lower()
    if oauth_env not in {'paper', 'live'}:
        oauth_env = 'paper'

    client_id = app.config.get('ALPACA_CLIENT_ID')
    redirect_uri = app.config.get('ALPACA_REDIRECT_URI')
    if not client_id or not redirect_uri:
        logger.error('Alpaca OAuth start failed: missing required OAuth configuration.')
        flash('Alpaca OAuth is not configured yet. Please contact support.', 'error')
        return redirect(url_for('onboarding'))

    current_user.broker_connection_started = True
    db.session.commit()
    track_user_event('broker_connection_started', user=current_user)

    oauth_state = secrets.token_urlsafe(32)
    session['oauth_state'] = oauth_state
    session['alpaca_oauth_user_id'] = current_user.id
    session['alpaca_oauth_env'] = oauth_env

    params = {
        'response_type': 'code',
        'client_id': client_id,
        'redirect_uri': redirect_uri,
        'scope': 'trading',
        'state': oauth_state,
        'env': oauth_env,
    }

    alpaca_auth_url = f"https://app.alpaca.markets/oauth/authorize?{urlencode(params)}"
    return redirect(alpaca_auth_url)



@app.route('/api/setup-checklist/mark', methods=['POST'])
@login_required
def api_setup_checklist_mark():
    data = request.get_json(silent=True) or {}
    step = data.get('step')
    allowed_steps = {
        'playbook_reviewed',
        'first_scan_completed',
        'transparency_reviewed',
        'broker_connection_started',
    }
    if step not in allowed_steps:
        return fail('Invalid setup checklist step.', 400)

    setattr(current_user, step, True)
    db.session.commit()
    if step == 'first_scan_completed':
        track_user_event('first_scan_completed', user=current_user)
    update_brevo_contact_attributes(current_user, get_user_brevo_funnel_attributes(current_user))
    return ok({'step': step, 'setup_checklist': get_user_setup_checklist(current_user)})




@app.route('/api/admin/sync-brevo-contact', methods=['POST'])
@login_required
def admin_sync_brevo_contact():
    if not ADMIN_EMAIL or (current_user.email or '').lower() != ADMIN_EMAIL:
        return fail('Forbidden', 403)

    update_brevo_contact_attributes(current_user, get_user_brevo_funnel_attributes(current_user))
    return ok({'synced': True})


@app.route('/api/admin/conversion-summary')
@login_required
def api_admin_conversion_summary():
    if not ADMIN_EMAIL or (current_user.email or '').strip().lower() != ADMIN_EMAIL:
        return fail('Forbidden', 403)

    rows = db.session.query(UserEvent.event_name, db.func.count(UserEvent.id)).group_by(UserEvent.event_name).all()
    counts = {event_name: count for event_name, count in rows}
    return ok({'counts': counts})






@app.route('/admin/user-recovery')
@login_required
def admin_user_recovery():
    if not is_admin_user():
        return ('Forbidden', 403)
    q = (request.args.get('q') or '').strip()
    users = []
    if q:
        query = User.query
        like = f"%{q}%"
        filters = [
            User.email.ilike(like),
            User.stripe_customer_id.ilike(like),
            User.alpaca_paper_account_id.ilike(like),
            User.alpaca_live_account_id.ilike(like),
        ]
        if q.isdigit():
            filters.append(User.id == int(q))
        users = query.filter(or_(*filters)).order_by(User.id.desc()).limit(25).all()
    return render_template('admin_user_recovery.html', users=[safe_user_summary(u) for u in users], q=q)


@app.route('/admin/user-recovery/<int:user_id>')
@login_required
def admin_user_recovery_detail(user_id):
    if not is_admin_user():
        return ('Forbidden', 403)
    user = User.query.get_or_404(user_id)
    recent_events = UserEvent.query.filter_by(user_id=user.id).order_by(UserEvent.created_at.desc()).limit(10).all()
    recent_trades = Trade.query.filter_by(user_id=user.id).order_by(Trade.created_at.desc()).limit(10).all()
    return render_template('admin_user_detail.html', user=safe_user_summary(user),
                           broker_status={
                               'paper_connected': bool(user.alpaca_paper_account_id or user._alpaca_paper_access_token),
                               'live_connected': bool(user.alpaca_live_account_id or user._alpaca_live_access_token),
                           }, recent_events=recent_events, recent_trades=recent_trades,
                           recent_trade_count=Trade.query.filter_by(user_id=user.id).count())


@app.route('/admin/user-recovery/<int:user_id>/send-reset', methods=['POST'])
@login_required
def admin_user_recovery_send_reset(user_id):
    if not is_admin_user():
        return ('Forbidden', 403)
    user = User.query.get_or_404(user_id)
    reset_url = build_password_reset_url(user)
    sent = send_password_reset_email(user, reset_url)
    flash('Password reset email sent.' if sent else 'Failed to send password reset email.', 'success' if sent else 'error')
    log_admin_recovery_action('send_reset', user.id, {'target_email': user.email, 'result': 'sent' if sent else 'failed'})
    return redirect(url_for('admin_user_recovery_detail', user_id=user.id))


@app.route('/admin/user-recovery/<int:user_id>/clear-onboarding', methods=['POST'])
@login_required
def admin_user_recovery_clear_onboarding(user_id):
    if not is_admin_user():
        return ('Forbidden', 403)
    user = User.query.get_or_404(user_id)
    for field in ['onboarding_completed','paper_bankroll_set','first_scan_completed','scan_preview_completed','playbook_reviewed','transparency_reviewed','broker_connection_started']:
        setattr(user, field, False)
    db.session.commit()
    log_admin_recovery_action('clear_onboarding', user.id, {'target_email': user.email, 'changed_fields': 'onboarding_flags'})
    flash('Onboarding flags reset.', 'success')
    return redirect(url_for('admin_user_recovery_detail', user_id=user.id))


@app.route('/admin/user-recovery/<int:user_id>/mark-onboarding-complete', methods=['POST'])
@login_required
def admin_user_recovery_mark_onboarding_complete(user_id):
    if not is_admin_user():
        return ('Forbidden', 403)
    user = User.query.get_or_404(user_id)
    for field in ['onboarding_completed','paper_bankroll_set','first_scan_completed','scan_preview_completed','playbook_reviewed','transparency_reviewed']:
        setattr(user, field, True)
    user.broker_connection_started = bool(user.alpaca_paper_account_id or user.alpaca_live_account_id or user._alpaca_paper_access_token or user._alpaca_live_access_token)
    db.session.commit()
    log_admin_recovery_action('mark_onboarding_complete', user.id, {'target_email': user.email, 'changed_fields': 'onboarding_flags'})
    flash('Onboarding flags marked complete.', 'success')
    return redirect(url_for('admin_user_recovery_detail', user_id=user.id))


@app.route('/admin/user-recovery/<int:user_id>/set-subscription-status', methods=['POST'])
@login_required
def admin_user_recovery_set_subscription_status(user_id):
    if not is_admin_user():
        return ('Forbidden', 403)
    allowed = {'free', 'pro', 'canceled', 'past_due'}
    status = (request.form.get('subscription_status') or '').strip().lower()
    if status not in allowed:
        flash('Invalid subscription status.', 'error')
        return redirect(url_for('admin_user_recovery_detail', user_id=user_id))
    user = User.query.get_or_404(user_id)
    user.subscription_status = status
    db.session.commit()
    log_admin_recovery_action('set_subscription_status', user.id, {'target_email': user.email, 'status': status})
    flash('Subscription status updated locally only; Stripe billing is unchanged.', 'success')
    return redirect(url_for('admin_user_recovery_detail', user_id=user.id))


@app.route('/admin/blog')
@login_required
def admin_blog_list():
    if not is_admin_user():
        return ("Forbidden", 403)
    posts = BlogPost.query.order_by(BlogPost.updated_at.desc()).all()
    return render_template('admin_blog_list.html', posts=posts)

@app.route('/admin/blog-rhythm')
@login_required
def admin_blog_rhythm():
    if not is_admin_user():
        abort(403)
    plans = BlogPublishingPlan.query.order_by(BlogPublishingPlan.planned_publish_date.asc().nullslast(), BlogPublishingPlan.created_at.desc()).all()
    return render_template('admin_blog_rhythm.html', plans=plans, allowed_statuses=sorted(BLOG_PLAN_ALLOWED_STATUSES))

@app.route('/admin/blog-rhythm/add', methods=['POST'])
@login_required
def admin_blog_rhythm_add():
    if not is_admin_user():
        abort(403)
    title = (request.form.get('title') or '').strip()
    if not title:
        flash('Title is required.', 'error')
        return redirect(url_for('admin_blog_rhythm'))
    planned_publish_date = parse_date(request.form.get('planned_publish_date'))
    if request.form.get('planned_publish_date') and planned_publish_date is None:
        flash('Invalid planned publish date. Please use YYYY-MM-DD.', 'error')
        return redirect(url_for('admin_blog_rhythm'))
    plan = BlogPublishingPlan(
        title=title,
        target_keyword=(request.form.get('target_keyword') or '').strip() or None,
        search_intent=(request.form.get('search_intent') or '').strip() or None,
        funnel_stage=(request.form.get('funnel_stage') or '').strip() or None,
        content_type=(request.form.get('content_type') or '').strip() or None,
        priority=parse_int(request.form.get('priority'), default=3, min_value=1, max_value=5),
        status='idea',
        notes=(request.form.get('notes') or '').strip() or None,
        planned_publish_date=planned_publish_date,
    )
    db.session.add(plan); db.session.commit()
    flash('Blog rhythm topic added.', 'success')
    return redirect(url_for('admin_blog_rhythm'))

@app.route('/admin/blog-rhythm/<int:plan_id>/update', methods=['POST'])
@login_required
def admin_blog_rhythm_update(plan_id):
    if not is_admin_user():
        abort(403)
    plan = BlogPublishingPlan.query.get_or_404(plan_id)
    status = (request.form.get('status') or '').strip()
    if status and status not in BLOG_PLAN_ALLOWED_STATUSES:
        flash('Invalid status.', 'error')
        return redirect(url_for('admin_blog_rhythm'))
    planned_publish_date = parse_date(request.form.get('planned_publish_date'))
    if request.form.get('planned_publish_date') and planned_publish_date is None:
        flash('Invalid planned publish date. Please use YYYY-MM-DD.', 'error')
        return redirect(url_for('admin_blog_rhythm'))
    if status:
        plan.status = status
    plan.assigned_author = (request.form.get('assigned_author') or '').strip() or None
    plan.notes = (request.form.get('notes') or '').strip() or None
    if request.form.get('priority'):
        plan.priority = parse_int(request.form.get('priority'), default=plan.priority or 3, min_value=1, max_value=5)
    plan.planned_publish_date = planned_publish_date
    db.session.commit()
    flash('Blog rhythm topic updated.', 'success')
    return redirect(url_for('admin_blog_rhythm'))

@app.route('/admin/blog-rhythm/<int:plan_id>/create-draft', methods=['POST'])
@login_required
def admin_blog_rhythm_create_draft(plan_id):
    if not is_admin_user():
        abort(403)
    plan = BlogPublishingPlan.query.get_or_404(plan_id)
    if plan.related_blog_post_id:
        return redirect(url_for('admin_blog_edit', post_id=plan.related_blog_post_id))
    title = (plan.title or 'Blog Draft').strip()
    slug = unique_blog_slug(title)
    body_html = "<p>Draft placeholder. Admin review required before publishing.</p>"
    ai_failed = False
    if os.getenv('GEMINI_API_KEY'):
        try:
            notes_parts = [
                plan.notes or '',
                f"Search intent: {plan.search_intent}" if plan.search_intent else '',
                f"Funnel stage: {plan.funnel_stage}" if plan.funnel_stage else '',
                f"Content type: {plan.content_type}" if plan.content_type else '',
            ]
            notes = "\n".join(part for part in notes_parts if part)
            draft = generate_blog_draft(
                title=title,
                target_keyword=plan.target_keyword or '',
                notes=notes,
            )
            if draft and draft.get('body_html'):
                body_html = sanitize_blog_html(draft.get('body_html') or body_html)
        except Exception as exc:
            ai_failed = True
            logger.warning("Blog rhythm AI draft generation failed for plan_id=%s: %s", plan.id, _brief_error_text(str(exc)))
    post = BlogPost(title=title, slug=slug, body_html=body_html, target_keyword=plan.target_keyword, status='draft', excerpt=plan.notes)
    db.session.add(post)
    db.session.flush()
    plan.related_blog_post_id = post.id
    if plan.status in {'idea', 'queued'}:
        plan.status = 'drafted'
    db.session.commit()
    if ai_failed:
        flash('AI draft generation failed; a manual placeholder draft was created. Admin review is required before publishing.', 'warning')
    else:
        flash('Draft created. Admin review is required before publishing.', 'success')
    return redirect(url_for('admin_blog_edit', post_id=post.id))



@app.route('/admin/blog/new', methods=['GET', 'POST'])
@login_required
def admin_blog_new():
    if not is_admin_user():
        return ("Forbidden", 403)
    if request.method == 'POST':
        action = (request.form.get('action') or '').strip().lower()
        requested_status = 'published' if action == 'publish' else 'draft'
        title = (request.form.get('title') or '').strip()
        body_html = request.form.get('body_html') or ''
        slug_input = (request.form.get('slug') or '').strip()
        draft_slug = slug_input or slugify_blog_title(title)
        form_data = {
            'title': title,
            'slug': slug_input,
            'status': requested_status,
            'author_name': (request.form.get('author_name') or '').strip(),
            'meta_title': (request.form.get('meta_title') or '').strip(),
            'meta_description': (request.form.get('meta_description') or '').strip(),
            'excerpt': (request.form.get('excerpt') or '').strip(),
            'body_html': body_html,
            'target_keyword': (request.form.get('target_keyword') or '').strip(),
            'canonical_url': (request.form.get('canonical_url') or '').strip(),
            'og_image': (request.form.get('og_image') or '').strip(),
            'featured_image_alt': (request.form.get('featured_image_alt') or '').strip(),
            'featured_image_caption': (request.form.get('featured_image_caption') or '').strip(),
        }
        seo_report = _safe_seo_report({**form_data, 'title': title, 'slug': draft_slug, 'body_html': body_html}, requested_status, context_label='new blog post')
        human_quality_report = _safe_human_quality({**form_data, 'title': title, 'body_html': body_html}, context_label='new blog post')
        if action == 'apply_safe_fixes':
            fixed = apply_safe_seo_fixes(
                title=form_data['title'],
                slug=form_data['slug'],
                meta_title=form_data['meta_title'],
                meta_description=form_data['meta_description'],
                excerpt=form_data['excerpt'],
                body_html=form_data['body_html'],
                target_keyword=form_data['target_keyword'],
                canonical_url=form_data['canonical_url'],
                og_image=form_data['og_image'],
                seo_report=seo_report,
                site_base_url=getattr(config, 'APP_BASE_URL', 'https://xeanvi.com'),
            )
            fixed_fields = fixed.get('fields', {})
            fixed_changes = fixed.get('changes', [])
            draft_slug = fixed_fields.get('slug') or slugify_blog_title(fixed_fields.get('title') or '')
            seo_report = _safe_seo_report({**fixed_fields, 'slug': draft_slug}, requested_status, context_label='new blog post')
            needs_ai_cleanup = any(any(token in (item or '').lower() for token in [
                'risky claim', 'target keyword is missing', 'does not reference xeanvi', 'add 1–3 relevant internal links', 'add 1-3 relevant internal links', 'no internal links', 'repeated phrase'
            ]) for item in (seo_report.get('warnings', []) + seo_report.get('suggestions', [])))
            ai_changes = []
            if needs_ai_cleanup:
                ai_cleanup = apply_ai_seo_cleanup(
                    title=fixed_fields.get('title') or '',
                    slug=draft_slug,
                    meta_title=fixed_fields.get('meta_title') or '',
                    meta_description=fixed_fields.get('meta_description') or '',
                    excerpt=fixed_fields.get('excerpt') or '',
                    body_html=fixed_fields.get('body_html') or '',
                    target_keyword=fixed_fields.get('target_keyword') or '',
                    seo_report=seo_report,
                    internal_link_suggestions=_safe_internal_links(fixed_fields, context_label='new blog post'),
                )
                if ai_cleanup.get('ok'):
                    fixed_fields.update(ai_cleanup.get('fields', {}))
                    fixed_fields['body_html'] = sanitize_blog_html(fixed_fields.get('body_html') or '')
                    ai_changes = ai_cleanup.get('changes') or []
                elif ai_cleanup.get('error'):
                    fixed_changes.append(f"AI cleanup skipped: {ai_cleanup.get('error')}")
            draft_slug = fixed_fields.get('slug') or slugify_blog_title(fixed_fields.get('title') or '')
            seo_report = _safe_seo_report({**fixed_fields, 'slug': draft_slug}, requested_status, context_label='new blog post')
            flash('Safe SEO fixes applied. Review changes before saving or publishing.', 'success')
            internal_link_suggestions = _safe_internal_links(fixed_fields, context_label='new blog post')
            return render_template(
                'admin_blog_form.html',
                post=None,
                form_data=fixed_fields,
                seo_report=seo_report,
                seo_fix_changes=(fixed_changes + ai_changes),
                unapplied_suggestions=fixed.get('unapplied_suggestions', []),
                draft_generated=False,
                internal_link_suggestions=internal_link_suggestions,
                human_quality_report=_safe_human_quality(fixed_fields, context_label='new blog post'),
            )
        if action == 'check_seo':
            flash('SEO check complete. Review issues below.', 'success')
            internal_link_suggestions = _safe_internal_links(form_data, context_label='new blog post')
            return render_template('admin_blog_form.html', post=None, form_data=form_data, seo_report=seo_report, internal_link_suggestions=internal_link_suggestions, human_quality_report=human_quality_report)
        if action == 'publish' and seo_report['status'] == 'blocked':
            flash('Publishing blocked. Fix the SEO/compliance issues below first.', 'error')
            internal_link_suggestions = _safe_internal_links(form_data, context_label='new blog post')
            return render_template('admin_blog_form.html', post=None, form_data=form_data, seo_report=seo_report, internal_link_suggestions=internal_link_suggestions, human_quality_report=human_quality_report)

        slug = unique_blog_slug(slug_input or title)
        upload_file = request.files.get('featured_image_file')
        og_image_value = form_data['og_image']
        if upload_file and (upload_file.filename or '').strip():
            upload_result = save_blog_featured_image(upload_file, slug)
            if not upload_result.get('ok'):
                flash(upload_result.get('error') or 'Featured image upload failed.', 'error')
                internal_link_suggestions = _safe_internal_links(form_data, context_label='new blog post')
                return render_template('admin_blog_form.html', post=None, form_data=form_data, seo_report=seo_report, internal_link_suggestions=internal_link_suggestions, human_quality_report=human_quality_report)
            og_image_value = upload_result.get('url') or og_image_value
            flash('Featured image uploaded.', 'success')
        if not form_data['featured_image_alt'] or not form_data['featured_image_caption']:
            try:
                generated_image_meta = generate_image_alt_caption(
                    title=title,
                    target_keyword=form_data['target_keyword'],
                    excerpt=form_data['excerpt'],
                    body_html=body_html,
                ) or {}
            except Exception:
                app.logger.exception('Image metadata helper failed for %s', 'new blog post')
                flash('Image alt/caption generation is temporarily unavailable.', 'error')
                generated_image_meta = {}
            if not form_data['featured_image_alt']:
                form_data['featured_image_alt'] = generated_image_meta.get('alt_text') or ''
            if not form_data['featured_image_caption']:
                form_data['featured_image_caption'] = generated_image_meta.get('caption') or ''
        canonical_url = (form_data['canonical_url'] or '').strip() or build_blog_canonical_url(slug)
        post = BlogPost(
            title=title,
            slug=slug,
            meta_title=form_data['meta_title'] or None,
            meta_description=form_data['meta_description'] or None,
            excerpt=form_data['excerpt'] or None,
            body_html=sanitize_blog_html(body_html),
            target_keyword=form_data['target_keyword'] or None,
            status=requested_status,
            canonical_url=canonical_url or None,
            og_image=og_image_value or None,
            featured_image_alt=form_data['featured_image_alt'] or None,
            featured_image_caption=form_data['featured_image_caption'] or None,
        )
        if requested_status == 'published' and not post.published_at:
            post.published_at = datetime.now(timezone.utc)
        db.session.add(post)
        db.session.commit()
        warned = False
        if requested_status == 'published' and seo_report.get('status') == 'needs_work':
            flash('Published with SEO warnings. Review suggestions when possible.', 'error')
            warned = True
        if requested_status == 'published' and int(human_quality_report.get('score') or 0) < 60:
            flash('Published with human-quality warnings. Consider improving the post for specificity and usefulness.', 'error')
            warned = True
        if not warned:
            flash('Blog post saved.', 'success')
        return redirect(url_for('admin_blog_edit', post_id=post.id))
    return render_template('admin_blog_form.html', post=None, internal_link_suggestions=[])



@app.route('/admin/blog/generate-draft', methods=['POST'])
@login_required
def admin_blog_generate_draft():
    if not is_admin_user():
        return ("Forbidden", 403)
    post = None
    post_id_raw = (request.form.get('post_id') or '').strip()
    if post_id_raw:
        try:
            post_id = int(post_id_raw)
        except (TypeError, ValueError):
            flash('Invalid blog draft reference.', 'error')
            return redirect(url_for('admin_blog_list'))
        post = db.session.get(BlogPost, post_id)
        if not post:
            flash('Blog draft not found.', 'error')
            return redirect(url_for('admin_blog_list'))

    title = (request.form.get('title') or '').strip()
    target_keyword = (request.form.get('target_keyword') or '').strip()
    notes = (request.form.get('notes') or '').strip()
    internal_links_raw = (request.form.get('internal_links') or '').strip()
    internal_links = [line.strip() for line in re.split(r'[\n,]+', internal_links_raw) if line.strip()]

    form_data = {
        'title': title,
        'slug': (request.form.get('slug') or '').strip(),
        'status': (request.form.get('status') or 'draft').strip().lower() or 'draft',
        'author_name': (request.form.get('author_name') or '').strip(),
        'meta_title': (request.form.get('meta_title') or '').strip(),
        'meta_description': (request.form.get('meta_description') or '').strip(),
        'excerpt': (request.form.get('excerpt') or '').strip(),
        'body_html': request.form.get('body_html') or '',
        'target_keyword': target_keyword,
        'canonical_url': (request.form.get('canonical_url') or '').strip(),
        'og_image': (request.form.get('og_image') or '').strip(),
        'featured_image_alt': (request.form.get('featured_image_alt') or '').strip(),
        'featured_image_caption': (request.form.get('featured_image_caption') or '').strip(),
        'internal_links': internal_links_raw,
        'notes': notes,
    }

    if not title:
        flash("Enter a blog title before generating an AI draft.", "error")
        suggestions = suggest_internal_links(title=form_data['title'], target_keyword=form_data['target_keyword'], excerpt=form_data['excerpt'], body_html=form_data['body_html'])
        return render_template('admin_blog_form.html', post=post, form_data=form_data, internal_link_suggestions=suggestions)

    if not (os.getenv('GEMINI_API_KEY') or '').strip():
        flash("AI draft generation is not configured. Missing GEMINI_API_KEY.", "error")
        suggestions = suggest_internal_links(title=form_data['title'], target_keyword=form_data['target_keyword'], excerpt=form_data['excerpt'], body_html=form_data['body_html'])
        return render_template('admin_blog_form.html', post=post, form_data=form_data, internal_link_suggestions=suggestions)

    draft = generate_blog_draft(
        title=title,
        target_keyword=target_keyword,
        internal_links=internal_links,
        notes=notes,
    )

    if not draft.get('ok'):
        flash(draft.get('error') or 'AI draft generation failed.', 'error')
        suggestions = suggest_internal_links(title=form_data['title'], target_keyword=form_data['target_keyword'], excerpt=form_data['excerpt'], body_html=form_data['body_html'])
        return render_template('admin_blog_form.html', post=post, form_data=form_data, internal_link_suggestions=suggestions)

    form_data.update({
        'title': draft.get('title') or title,
        'meta_title': draft.get('meta_title') or '',
        'meta_description': draft.get('meta_description') or '',
        'excerpt': draft.get('excerpt') or '',
        'body_html': sanitize_blog_html(draft.get('body_html') or ''),
        'target_keyword': draft.get('target_keyword') or target_keyword,
        'status': 'draft',
    })
    if not (form_data.get('canonical_url') or '').strip():
        form_data['canonical_url'] = build_blog_canonical_url(form_data.get("slug") or slugify_blog_title(form_data.get("title") or ""))
    slug_value = form_data.get("slug") or slugify_blog_title(form_data.get("title") or "")
    seo_report = analyze_blog_post_seo(
        title=form_data.get('title') or '', slug=slug_value,
        meta_title=form_data.get('meta_title') or '', meta_description=form_data.get('meta_description') or '',
        excerpt=form_data.get('excerpt') or '', body_html=form_data.get('body_html') or '',
        target_keyword=form_data.get('target_keyword') or '', canonical_url=form_data.get('canonical_url') or '', status='draft'
    )
    human_quality_report = analyze_human_quality(title=form_data.get('title') or '', excerpt=form_data.get('excerpt') or '', body_html=form_data.get('body_html') or '', target_keyword=form_data.get('target_keyword') or '')
    flash("AI draft generated. Review and edit it before publishing.", "success")
    internal_link_suggestions = suggest_internal_links(
        title=form_data.get('title') or '',
        target_keyword=form_data.get('target_keyword') or '',
        excerpt=form_data.get('excerpt') or '',
        body_html=form_data.get('body_html') or '',
    )
    return render_template('admin_blog_form.html', post=post, form_data=form_data, seo_report=seo_report, internal_link_suggestions=internal_link_suggestions, human_quality_report=human_quality_report)

@app.route('/admin/blog/<int:post_id>/delete', methods=['POST'])
@login_required
def admin_blog_delete(post_id):
    if not is_admin_user():
        return ("Forbidden", 403)
    post = db.session.get(BlogPost, post_id)
    if not post:
        flash('Blog post not found.', 'error')
        return redirect(url_for('admin_blog_list'))

    db.session.delete(post)
    db.session.commit()
    flash('Blog post deleted.', 'success')
    return redirect(url_for('admin_blog_list'))


def _blog_context_label(post_id=None, context_label=None):
    if context_label:
        return context_label
    if post_id is not None:
        return f'blog post {post_id}'
    return 'blog post'


def _safe_internal_links(data, post_id=None, context_label=None):
    try:
        return suggest_internal_links(
            title=(data.get('title') or ''),
            target_keyword=(data.get('target_keyword') or ''),
            excerpt=(data.get('excerpt') or ''),
            body_html=(data.get('body_html') or ''),
        ) or []
    except Exception:
        app.logger.exception('Internal link suggestion helper failed for %s', _blog_context_label(post_id=post_id, context_label=context_label))
        flash('Internal link suggestions are temporarily unavailable.', 'error')
        return []


def _safe_human_quality(data, post_id=None, context_label=None):
    try:
        report = analyze_human_quality(
            title=(data.get('title') or ''),
            excerpt=(data.get('excerpt') or ''),
            body_html=(data.get('body_html') or ''),
            target_keyword=(data.get('target_keyword') or ''),
        ) or {}
    except Exception:
        app.logger.exception('Human quality helper failed for %s', _blog_context_label(post_id=post_id, context_label=context_label))
        flash('Human-quality analysis is temporarily unavailable.', 'error')
        report = {}
    return {
        'score': int(report.get('score') or 0),
        'status': report.get('status') or 'unknown',
        'warnings': report.get('warnings') or [],
        'suggestions': report.get('suggestions') or [],
        'strengths': report.get('strengths') or [],
        'metrics': report.get('metrics') or {},
    }


def _safe_seo_report(data, status, post_id=None, context_label=None):
    try:
        report = analyze_blog_post_seo(
            title=(data.get('title') or ''), slug=(data.get('slug') or ''), meta_title=(data.get('meta_title') or ''),
            meta_description=(data.get('meta_description') or ''), excerpt=(data.get('excerpt') or ''),
            body_html=(data.get('body_html') or ''), target_keyword=(data.get('target_keyword') or ''),
            canonical_url=(data.get('canonical_url') or ''), status=status, og_image=(data.get('og_image') or ''),
            featured_image_alt=(data.get('featured_image_alt') or ''),
        ) or {}
    except Exception:
        app.logger.exception('SEO helper failed for %s', _blog_context_label(post_id=post_id, context_label=context_label))
        flash('SEO analysis is temporarily unavailable.', 'error')
        report = {}
    return {
        'status': report.get('status') or 'ok',
        'score': report.get('score') if report.get('score') is not None else 0,
        'warnings': report.get('warnings') or [],
        'suggestions': report.get('suggestions') or [],
        'blocking_issues': report.get('blocking_issues') or [],
        'metrics': report.get('metrics') or {},
    }

@app.route('/admin/blog/<int:post_id>/edit', methods=['GET', 'POST'])
@login_required
def admin_blog_edit(post_id):
    if not is_admin_user():
        return ("Forbidden", 403)
    post = BlogPost.query.get_or_404(post_id)

    if request.method == 'POST':
        action = (request.form.get('action') or '').strip().lower()
        requested_status = 'published' if action == 'publish' else 'draft'
        title = (request.form.get('title') or '').strip()
        body_html = request.form.get('body_html') or ''
        slug_input = (request.form.get('slug') or '').strip()
        draft_slug = slug_input or slugify_blog_title(title)
        form_data = {
            'title': title,
            'slug': slug_input,
            'status': requested_status,
            'author_name': (request.form.get('author_name') or '').strip(),
            'meta_title': (request.form.get('meta_title') or '').strip(),
            'meta_description': (request.form.get('meta_description') or '').strip(),
            'excerpt': (request.form.get('excerpt') or '').strip(),
            'body_html': body_html,
            'target_keyword': (request.form.get('target_keyword') or '').strip(),
            'canonical_url': (request.form.get('canonical_url') or '').strip(),
            'og_image': (request.form.get('og_image') or '').strip(),
            'featured_image_alt': (request.form.get('featured_image_alt') or '').strip(),
            'featured_image_caption': (request.form.get('featured_image_caption') or '').strip(),
        }
        seo_report = _safe_seo_report({**form_data, 'title': title, 'slug': draft_slug, 'body_html': body_html}, requested_status, post_id=post.id)
        human_quality_report = _safe_human_quality({**form_data, 'title': title, 'body_html': body_html}, post_id=post.id)
        if action == 'apply_safe_fixes':
            fixed = apply_safe_seo_fixes(
                title=form_data['title'],
                slug=form_data['slug'],
                meta_title=form_data['meta_title'],
                meta_description=form_data['meta_description'],
                excerpt=form_data['excerpt'],
                body_html=form_data['body_html'],
                target_keyword=form_data['target_keyword'],
                canonical_url=form_data['canonical_url'],
                og_image=form_data['og_image'],
                seo_report=seo_report,
                site_base_url=getattr(config, 'APP_BASE_URL', 'https://xeanvi.com'),
            )
            fixed_fields = fixed.get('fields', {})
            fixed_changes = fixed.get('changes', [])
            draft_slug = fixed_fields.get('slug') or slugify_blog_title(fixed_fields.get('title') or '')
            seo_report = _safe_seo_report({**fixed_fields, 'slug': draft_slug}, requested_status, post_id=post.id)
            needs_ai_cleanup = any(any(token in (item or '').lower() for token in [
                'risky claim', 'target keyword is missing', 'does not reference xeanvi', 'add 1–3 relevant internal links', 'add 1-3 relevant internal links', 'no internal links', 'repeated phrase'
            ]) for item in (seo_report.get('warnings', []) + seo_report.get('suggestions', [])))
            ai_changes = []
            if needs_ai_cleanup:
                ai_cleanup = apply_ai_seo_cleanup(
                    title=fixed_fields.get('title') or '',
                    slug=draft_slug,
                    meta_title=fixed_fields.get('meta_title') or '',
                    meta_description=fixed_fields.get('meta_description') or '',
                    excerpt=fixed_fields.get('excerpt') or '',
                    body_html=fixed_fields.get('body_html') or '',
                    target_keyword=fixed_fields.get('target_keyword') or '',
                    seo_report=seo_report,
                    internal_link_suggestions=_safe_internal_links(fixed_fields, post_id=post.id),
                )
                if ai_cleanup.get('ok'):
                    fixed_fields.update(ai_cleanup.get('fields', {}))
                    fixed_fields['body_html'] = sanitize_blog_html(fixed_fields.get('body_html') or '')
                    ai_changes = ai_cleanup.get('changes') or []
                elif ai_cleanup.get('error'):
                    fixed_changes.append(f"AI cleanup skipped: {ai_cleanup.get('error')}")
            draft_slug = fixed_fields.get('slug') or slugify_blog_title(fixed_fields.get('title') or '')
            seo_report = _safe_seo_report({**fixed_fields, 'slug': draft_slug}, requested_status, post_id=post.id)
            flash('Safe SEO fixes applied. Review changes before saving or publishing.', 'success')
            internal_link_suggestions = _safe_internal_links(fixed_fields, post_id=post.id)
            return render_template(
                'admin_blog_form.html',
                post=post,
                form_data=fixed_fields,
                seo_report=seo_report,
                seo_fix_changes=(fixed_changes + ai_changes),
                unapplied_suggestions=fixed.get('unapplied_suggestions', []),
                draft_generated=False,
                internal_link_suggestions=internal_link_suggestions,
                human_quality_report=_safe_human_quality(fixed_fields, post_id=post.id),
            )
        if action == 'check_seo':
            flash('SEO check complete. Review issues below.', 'success')
            internal_link_suggestions = _safe_internal_links(form_data, post_id=post.id)
            return render_template('admin_blog_form.html', post=post, form_data=form_data, seo_report=seo_report, internal_link_suggestions=internal_link_suggestions, human_quality_report=human_quality_report)
        if action == 'publish' and seo_report['status'] == 'blocked':
            flash('Publishing blocked. Fix the SEO/compliance issues below first.', 'error')
            internal_link_suggestions = _safe_internal_links(form_data, post_id=post.id)
            return render_template('admin_blog_form.html', post=post, form_data=form_data, seo_report=seo_report, internal_link_suggestions=internal_link_suggestions, human_quality_report=human_quality_report)

        prev_status = post.status
        upload_file = request.files.get('featured_image_file')
        og_image_value = (form_data['og_image'] or '').strip() or (post.og_image or '')
        if upload_file and (upload_file.filename or '').strip():
            try:
                upload_result = save_blog_featured_image(upload_file, form_data['slug'] or title) or {}
            except Exception:
                app.logger.exception('Featured image upload helper failed for blog post %s', post.id)
                upload_result = {'ok': False, 'error': 'Featured image upload failed unexpectedly.'}
            if not upload_result.get('ok'):
                flash(upload_result.get('error') or 'Featured image upload failed.', 'error')
                internal_link_suggestions = _safe_internal_links(form_data, post_id=post.id)
                return render_template('admin_blog_form.html', post=post, form_data=form_data, seo_report=seo_report, internal_link_suggestions=internal_link_suggestions, human_quality_report=human_quality_report)
            og_image_value = upload_result.get('url') or og_image_value
            flash('Featured image uploaded.', 'success')
        if not form_data['featured_image_alt'] or not form_data['featured_image_caption']:
            try:
                generated_image_meta = generate_image_alt_caption(
                    title=title,
                    target_keyword=form_data['target_keyword'],
                    excerpt=form_data['excerpt'],
                    body_html=body_html,
                ) or {}
            except Exception:
                app.logger.exception('Image metadata helper failed for blog post %s', post.id)
                flash('Image alt/caption generation is temporarily unavailable.', 'error')
                generated_image_meta = {}
            if not form_data['featured_image_alt']:
                form_data['featured_image_alt'] = generated_image_meta.get('alt_text') or ''
            if not form_data['featured_image_caption']:
                form_data['featured_image_caption'] = generated_image_meta.get('caption') or ''
        post.title = title
        post.slug = unique_blog_slug(slug_input or title, existing_post_id=post.id)
        post.meta_title = form_data['meta_title'] or None
        post.meta_description = form_data['meta_description'] or None
        post.excerpt = form_data['excerpt'] or None
        post.target_keyword = form_data['target_keyword'] or None
        post.body_html = sanitize_blog_html(body_html)
        post.status = requested_status
        post.canonical_url = (form_data.get('canonical_url') or '').strip()
        if not post.canonical_url:
            try:
                post.canonical_url = build_blog_canonical_url(post.slug) or None
            except Exception:
                app.logger.exception('Failed to build canonical URL for blog post %s', post.id)
                post.canonical_url = None
        post.og_image = og_image_value or None
        post.featured_image_alt = form_data['featured_image_alt'] or None
        post.featured_image_caption = form_data['featured_image_caption'] or None
        if prev_status != 'published' and post.status == 'published' and not post.published_at:
            post.published_at = datetime.now(timezone.utc)
        post.updated_at = datetime.now(timezone.utc)
        try:
            db.session.commit()
        except IntegrityError as exc:
            db.session.rollback()
            app.logger.exception('Failed to update blog post %s due to integrity error', post.id)
            flash('Unable to update post. Another post may already be using this slug.', 'error')
            internal_link_suggestions = _safe_internal_links(form_data, post_id=post.id)
            return render_template('admin_blog_form.html', post=post, form_data=form_data, seo_report=seo_report, internal_link_suggestions=internal_link_suggestions, human_quality_report=human_quality_report), 400
        except Exception:
            db.session.rollback()
            app.logger.exception('Unexpected error while updating blog post %s', post.id)
            flash('Unexpected error while updating blog post. Please try again.', 'error')
            internal_link_suggestions = _safe_internal_links(form_data, post_id=post.id)
            return render_template('admin_blog_form.html', post=post, form_data=form_data, seo_report=seo_report, internal_link_suggestions=internal_link_suggestions, human_quality_report=human_quality_report), 500
        warned = False
        if requested_status == 'published' and seo_report.get('status') == 'needs_work':
            flash('Published with SEO warnings. Review suggestions when possible.', 'error')
            warned = True
        if requested_status == 'published' and int(human_quality_report.get('score') or 0) < 60:
            flash('Published with human-quality warnings. Consider improving the post for specificity and usefulness.', 'error')
            warned = True
        if not warned:
            flash('Blog post updated.', 'success')
        return redirect(url_for('admin_blog_edit', post_id=post.id))
    internal_link_suggestions = _safe_internal_links({'title': post.title, 'target_keyword': post.target_keyword, 'excerpt': post.excerpt, 'body_html': post.body_html}, post_id=post.id)
    human_quality_report = _safe_human_quality({'title': post.title, 'excerpt': post.excerpt, 'body_html': post.body_html, 'target_keyword': post.target_keyword}, post_id=post.id)
    return render_template('admin_blog_form.html', post=post, internal_link_suggestions=internal_link_suggestions, human_quality_report=human_quality_report)




@app.route('/admin/blog/internal-link-suggestions', methods=['POST'])
@login_required
def admin_blog_internal_link_suggestions():
    if not is_admin_user():
        return jsonify({'ok': False, 'error': 'Forbidden'}), 403

    try:
        title = (request.form.get('title') or '').strip()
        target_keyword = (request.form.get('target_keyword') or '').strip()
        excerpt = (request.form.get('excerpt') or '').strip()
        body_html = request.form.get('body_html') or ''

        suggestions = suggest_internal_links(
            title=title,
            target_keyword=target_keyword,
            excerpt=excerpt,
            body_html=body_html,
        )
        return jsonify({'ok': True, 'suggestions': suggestions})
    except Exception:
        return jsonify({'ok': False, 'error': 'Could not generate internal link suggestions.'}), 500

@app.route('/admin/blog/<int:post_id>/unpublish', methods=['POST'])
@login_required
def admin_blog_unpublish(post_id):
    if not is_admin_user():
        return ("Forbidden", 403)
    post = BlogPost.query.get_or_404(post_id)
    post.status = 'draft'
    post.updated_at = datetime.now(timezone.utc)
    db.session.commit()
    flash('Post moved to draft.', 'success')
    return redirect(url_for('admin_blog_edit', post_id=post.id))


@app.route('/api/update_mode', methods=['POST'])
@login_required
def update_mode():
    data = request.get_json(silent=True) or {}
    new_mode = data.get('trading_mode')

    if new_mode not in {'paper', 'live'}:
        return jsonify({
            'ok': False,
            'status': 'error',
            'message': 'Invalid trading mode.'
        }), 400

    # Block non-PRO users from going Live
    if new_mode == 'live' and not user_is_pro(current_user):
        return jsonify({
            'ok': False,
            'status': 'error',
            'message': 'PRO_UPGRADE_REQUIRED: Live execution is a premium feature.'
        }), 403

    # Ensure broker is connected for Paper mode
    if current_user.trading_mode == 'live' and not user_has_alpaca_live_connection(current_user):
        current_user.trading_mode = 'paper'
        current_user.sync_legacy_bankroll_from_active_mode()
        db.session.commit()

    if new_mode == 'paper' and not user_has_alpaca_paper_connection(current_user):
        return jsonify({
            'ok': False,
            'status': 'error',
            'message': 'Connect Alpaca Paper in onboarding before enabling PAPER mode.'
        }), 400

    # Ensure broker is connected for Live mode
    if new_mode == 'live' and not live_mode_unlocked(current_user):
        return jsonify({
            'ok': False,
            'status': 'error',
            'message': 'Connect Alpaca Live in onboarding before enabling LIVE mode.'
        }), 400

    # Save the mode FIRST. This is the most important part.
    current_user.trading_mode = new_mode
    current_user.sync_legacy_bankroll_from_active_mode()
    db.session.commit()

    try:
        fetch_and_sync_bankroll(current_user)
        db.session.refresh(current_user)
    except Exception as exc:
        logger.error("Mode switched but bankroll sync failed: %s", exc)

    return jsonify({
        'ok': True,
        'status': 'success',
        'mode': current_user.trading_mode,
        'bankroll': current_user.bankroll,
        'paper_bankroll': current_user.paper_bankroll,
        'live_bankroll': current_user.live_bankroll,
    })


@app.route('/alpaca/callback')
@login_required
def alpaca_callback():
    returned_state = request.args.get('state')
    saved_state = session.pop('oauth_state', None)

    if not returned_state or returned_state != saved_state:
        flash("Security Error: Invalid OAuth state token. Request aborted.", "error")
        return redirect(url_for('settings'))

    code = request.args.get('code')
    if not code:
        flash("Authorization failed.", "error")
        return redirect(url_for('settings'))

    if session.get('alpaca_oauth_user_id') != current_user.id:
        flash('Your Alpaca connection session expired or changed. Please try again.', 'error')
        session.pop('oauth_state', None)
        session.pop('alpaca_oauth_user_id', None)
        session.pop('alpaca_oauth_env', None)
        return redirect(url_for('onboarding'))

    # Use the centralized OAuth token endpoint
    token_url = "https://api.alpaca.markets/oauth/token"
    payload = {
        'grant_type': 'authorization_code',
        'code': code,
        'client_id': app.config['ALPACA_CLIENT_ID'],
        'client_secret': app.config['ALPACA_CLIENT_SECRET'],
        'redirect_uri': app.config['ALPACA_REDIRECT_URI'],
    }

    try:
        response = requests.post(token_url, data=payload, timeout=15)

        if response.status_code != 200:
            logger.error('Alpaca token exchange rejected with status=%s for user_id=%s', response.status_code, current_user.id)
            flash('Alpaca rejected the token exchange. Please retry and confirm your broker OAuth app settings.', 'error')
            return redirect(url_for('settings'))

        data = response.json()
        if 'access_token' in data:
            token = data['access_token']
            oauth_env = (session.get('alpaca_oauth_env') or 'paper').strip().lower()
            session.pop('oauth_state', None)
            session.pop('alpaca_oauth_user_id', None)
            session.pop('alpaca_oauth_env', None)

            connection_result = detect_and_store_alpaca_connection(current_user, token, env=oauth_env)
            current_user.onboarding_completed = onboarding_requirements_met(current_user)
            db.session.commit()

            connected_parts = []
            if connection_result.get("paper_connected"):
                connected_parts.append("Paper")
            if connection_result.get("live_connected"):
                connected_parts.append("Live")

            if connection_result.get('paper_connected'):
                update_brevo_contact_attributes(
                    current_user,
                    {
                        'ALPACA_PAPER_CONNECTED': True,
                        'SUBSCRIPTION_STATUS': current_user.subscription_status or 'free',
                        'IS_PRO': user_is_pro(current_user) if 'user_is_pro' in globals() else ((current_user.subscription_status or '').lower() == 'pro'),
                    }
                )

            if connected_parts:
                flash(
                    f"Broker connected: {', '.join(connected_parts)} account(s) authorized.",
                    "success"
                )
            else:
                flash(
                    "Alpaca authorized, but no Paper or Live trading account could be verified. Check Alpaca permissions.",
                    "error"
                )
        else:
            logger.warning('Alpaca OAuth callback missing access token for user_id=%s', current_user.id)
            flash('Alpaca authorization did not return an access token. Please try again.', 'error')
    except Exception:
        logger.exception('Alpaca token exchange system error for user_id=%s', current_user.id)
        flash('System error while finalizing Alpaca authorization. Please try again shortly.', 'error')

    return redirect(url_for('settings'))


@app.route('/v1/oauth/callback')
def sandbox_callback():
    return alpaca_callback()  # This acts as an alias


@app.route('/alpaca/logout')
@login_required
def alpaca_logout():
    env = (request.args.get('env') or 'paper').strip().lower()
    if env not in {'paper', 'live'}:
        env = 'paper'
    if env == 'live':
        current_user.alpaca_live_access_token = None
        current_user.alpaca_live_account_id = None
        current_user.live_bankroll = 0.0
        if current_user.trading_mode == 'live':
            current_user.trading_mode = 'paper'
    else:
        current_user.alpaca_paper_access_token = None
        current_user.alpaca_paper_account_id = None
        current_user.paper_bankroll = 0.0
        current_user.paper_bankroll_set = False
    current_user.onboarding_completed = onboarding_requirements_met(current_user)
    current_user.sync_legacy_bankroll_from_active_mode()
    db.session.commit()
    flash(f"Alpaca {env} account disconnected.", 'success')
    return redirect(url_for('settings'))





@app.route('/api/execution-readiness')
@login_required
def api_execution_readiness():
    from execution_diagnostics import evaluate_execution_readiness, decision_allowlist, env_bool, onboarding_complete

    latest_payload = None
    no_recent_scan = False
    try:
        raw = redis_client.get(f'latest_scan:{current_user.id}')
        if raw:
            latest_payload = json.loads(raw)
    except Exception:
        latest_payload = None

    if latest_payload is None:
        scans = get_recent_scans() or []
        for scan in scans:
            if int(scan.get('user_id') or 0) == int(current_user.id):
                latest_payload = scan
                break

    if latest_payload is None:
        no_recent_scan = True
        latest_payload = {}

    diag = evaluate_execution_readiness(current_user, latest_payload)
    contract_diag = diag.get('scan_contract')
    if not isinstance(contract_diag, dict):
        contract_diag = validate_scan_payload_contract(latest_payload if isinstance(latest_payload, dict) else {})
    if no_recent_scan:
        no_scan_reason = {'code': 'NO_RECENT_SCAN', 'message': 'No recent scan found for current user.'}

        def _append_reason_once(reason_list):
            if not any((r or {}).get('code') == no_scan_reason['code'] for r in reason_list):
                reason_list.append(no_scan_reason)

        _append_reason_once(diag['blocked_reasons'])
        _append_reason_once(diag.setdefault('active_mode_blocked_reasons', []))
        _append_reason_once(diag.setdefault('paper_blocked_reasons', []))
        _append_reason_once(diag.setdefault('live_blocked_reasons', []))
        diag['execution_ready'] = False
        if diag.get('active_mode') == 'paper':
            diag['paper_execution_ready'] = False
        elif diag.get('active_mode') == 'live':
            diag['live_execution_ready'] = False

    payload = {
        'active_mode': diag.get('active_mode', getattr(current_user, 'trading_mode', 'paper')),
        'execution_ready': diag.get('execution_ready'),
        'paper_execution_ready': diag.get('paper_execution_ready'),
        'live_execution_ready': diag.get('live_execution_ready'),
        'paper_blocked_reasons': diag.get('paper_blocked_reasons', []),
        'live_blocked_reasons': diag.get('live_blocked_reasons', []),
        'active_mode_blocked_reasons': diag.get('active_mode_blocked_reasons', []),
        'execution_enabled': env_bool('CENTRAL_SCANNER_EXECUTION_ENABLED', False),
        'live_execution_enabled': env_bool('CENTRAL_SCANNER_LIVE_EXECUTION_ENABLED', False),
        'user_is_pro': str(getattr(current_user, 'subscription_status', 'free')).lower() == 'pro',
        'trading_mode': getattr(current_user, 'trading_mode', 'paper'),
        'has_paper_token': bool(getattr(current_user, 'alpaca_paper_access_token', None)),
        'has_live_token': bool(getattr(current_user, 'alpaca_live_access_token', None)),
        'has_active_alpaca_token': bool(getattr(current_user, 'alpaca_access_token', None)),
        'onboarding_complete': onboarding_complete(current_user),
        'paper_setup_ready': bool(diag.get('paper_setup_ready')),
        'paper_setup_blocked_reasons': diag.get('paper_setup_blocked_reasons', []),
        'live_onboarding_ready': bool(diag.get('live_onboarding_ready')),
        'live_onboarding_blocked_reasons': diag.get('live_onboarding_blocked_reasons', []),
        'buy_window_open': diag.get('buy_window_open'),
        'decision_allowlist': sorted(decision_allowlist()),
        'latest_scan_evaluation': diag,
        'scan_contract': contract_diag,
    }
    return ok(payload)



@app.route('/api/scanner-effectiveness')
@login_required
def api_scanner_effectiveness():
    limit = parse_int(request.args.get('limit'), default=50, min_value=1, max_value=200)
    target_user = current_user
    if is_admin_user() and str(request.args.get('scope', 'user')).strip().lower() == 'all':
        target_user = None
    report = build_scanner_effectiveness_report(user=target_user, limit=limit)
    return ok(report)


@app.route('/api/scanner/effectiveness')
@login_required
def api_scanner_effectiveness_v2():
    return api_scanner_effectiveness()


@app.route('/api/runtime-health')
def api_runtime_health():
    websocket_upgrade_header = (request.headers.get('Upgrade') or '').lower()
    return ok(
        {
            'db_path': config.DB_PATH,
            'ws_proxy_hint': 'Ensure proxy forwards Upgrade/Connection headers for /ws/watchlist when using Nginx/Gunicorn.',
            'ws_upgrade_header_seen': websocket_upgrade_header,
        }
    )


@app.route('/api/scan', methods=['POST', 'GET'])
@login_required
def api_scan():
    try:
        # Sync bankroll before scanning so risk sizing uses current account equity.
        fetch_and_sync_bankroll(current_user)
        result = run_scan(current_user)
        contract_diag = validate_scan_payload_contract(result if isinstance(result, dict) else {})
        logger.info('Scan contract user_id=%s has_best_pick=%s key=%s executable_ready=%s missing=%s decision=%s qty_valid=%s notes=%s', current_user.id, contract_diag.get('has_best_pick'), contract_diag.get('best_pick_key_used'), contract_diag.get('executable_payload_ready'), contract_diag.get('missing_order_fields'), contract_diag.get('decision'), contract_diag.get('qty_valid'), contract_diag.get('payload_shape_notes'))
        try:
            result["dynamic_orb_state"] = get_latest_dynamic_orb_state()
        except Exception as exc:
            logger.warning("Dynamic ORB state unavailable for scan response: %s", exc)
            result["dynamic_orb_state"] = get_dynamic_orb_metadata_fallback(
                reason="Dynamic ORB state unavailable; existing static rules remain active.",
            )
        risk_controls = {
            'failed_trades_today': get_failed_trades_today(),
            'max_failed_trades_per_day': config.MAX_FAILED_TRADES_PER_DAY,
            'buy_window_open': buy_window_open(),
            'no_buy_before_et': config.NO_BUY_BEFORE_ET,
        }
        result['risk_controls'] = risk_controls
        result["user_id"] = current_user.id
        result["report_user_id"] = current_user.id
        result["trading_mode"] = getattr(current_user, "trading_mode", "paper")
        result["subscription_status"] = getattr(current_user, "subscription_status", "free")
        result["scan_source"] = "dashboard_manual"
        result["scan_attribution_version"] = 1
        scan_id = insert_scan(result)
        result['scan_id'] = scan_id
        if not current_user.first_scan_completed:
            current_user.first_scan_completed = True
        db.session.commit()

        approved_plan = approve_scan_for_user(redis_client, current_user, result)
        result["approved_execution_plan"] = approved_plan
        track_user_event('scan_generated', user=current_user, context={'scan_id': scan_id})
        try:
            redis_client.setex(f'latest_scan:{current_user.id}', 60 * 60 * 8, json.dumps(result))
        except Exception as redis_exc:
            logger.warning(f"Redis cache write failed (bypassing): {redis_exc}")
        watchlist_manager.set_items(result.get('watchlist', []))
        return ok(
            result,
            history={'scans': get_recent_scans(), 'trades': get_recent_trades()},
        )
    except ScanError as exc:
        return fail(str(exc))
    except Exception as exc:
        return fail(f'Scan failed: {exc}', 500)





def build_debug_rejection_reasons(payload: dict) -> list[str]:
    reasons = list(payload.get('rejection_reasons') or [])

    def add(reason: str):
        if reason not in reasons:
            reasons.append(reason)

    decision = str(payload.get('decision') or '').upper()
    setup_grade = str(payload.get('setup_grade') or '').upper()
    if decision == 'SKIP':
        add('decision_skip')
    if setup_grade == 'NO TRADE':
        add('setup_grade_no_trade')
    if payload.get('tradable_by_xeanvi') is False:
        add('not_tradeable_by_xeanvi')

    current = float(payload.get('current_price') or 0)
    stop = payload.get('stop_price')
    if stop is not None and current < float(stop):
        add('below_stop')

    vwap = payload.get('vwap')
    if vwap is not None and current < float(vwap):
        add('below_vwap')

    spread = payload.get('spread_pct')
    if spread is not None and float(spread) > float(config.MOMENTUM_MAX_SPREAD_PCT):
        add('spread_too_wide')

    entry = payload.get('entry_price')
    buy_lower = payload.get('buy_lower')
    buy_upper = payload.get('buy_upper')
    near_entry = False
    if entry is not None:
        e = float(entry)
        near_entry = abs(current - e) / e <= 0.003 if e > 0 else False
        if current < e and not near_entry:
            add('price_below_entry')
    in_controlled_range = False
    if buy_lower is not None and buy_upper is not None:
        lo, hi = float(buy_lower), float(buy_upper)
        in_controlled_range = lo <= current <= hi
    if entry is not None and not (in_controlled_range or near_entry):
        add('no_controlled_entry')

    qty = payload.get('qty')
    if qty is not None and float(qty) <= 0:
        add('qty_zero')

    return reasons


def build_debug_final_explanation(payload: dict) -> str:
    symbol = str(payload.get('symbol') or '').upper()
    reasons = set(payload.get('rejection_reasons') or [])
    asset_type = str(payload.get('asset_type') or '').upper()

    if 'not_tradeable_by_xeanvi' in reasons:
        if asset_type == 'LEVERAGED_ETF':
            return (
                f"{symbol} is a leveraged ETF. Leveraged ETF trading is disabled by "
                "platform/user settings, so XeanVI will not trade it."
            )
        if asset_type == 'INVERSE_ETF':
            return (
                f"{symbol} is an inverse ETF. Inverse ETF trading is disabled by "
                "platform/user settings, so XeanVI will not trade it."
            )
        if asset_type == 'OPTION':
            return f"{symbol} looks like an options contract. Options trading is not supported yet."
        return f"{symbol} is blocked by asset-type/user/platform trading settings."

    setup_phrases = [
        ('below_stop', 'below the calculated stop'),
        ('below_vwap', 'below VWAP'),
        ('spread_too_wide', 'spread is too wide'),
        ('price_below_entry', 'below the planned entry'),
        ('no_controlled_entry', 'not inside a controlled entry zone'),
        ('qty_zero', 'risk sizing produced zero shares'),
        ('setup_grade_no_trade', 'setup grade is NO TRADE'),
        ('decision_skip', 'decision is SKIP'),
    ]
    setup_reasons = [phrase for reason, phrase in setup_phrases if reason in reasons]
    if {'diagnostic_data_unavailable', 'deep_analysis_failed'} & reasons:
        return 'Symbol could not be fully analyzed because market/quote/bar data was incomplete.'
    if setup_reasons:
        if len(setup_reasons) == 1:
            setup_text = setup_reasons[0]
        elif len(setup_reasons) == 2:
            setup_text = f"{setup_reasons[0]} and {setup_reasons[1]}"
        else:
            setup_text = f"{', '.join(setup_reasons[:-1])}, and {setup_reasons[-1]}"
        return f"{symbol} was found, but XeanVI should not chase it because it is {setup_text}."

    return str(payload.get('final_explanation') or 'Symbol analyzed.')

@app.route('/api/debug-symbol/<symbol>')
@login_required
def api_debug_symbol(symbol: str):
    symbol = (symbol or '').upper().strip()
    feed = request.args.get('feed', '').strip().lower() or resolve_data_feed(current_user)
    snapshots = get_snapshots([symbol], feed=feed)
    quotes = get_latest_quotes([symbol], feed=feed)
    snap = snapshots.get(symbol, {})
    q = quotes.get(symbol, {})
    price = float((q.get('ap') or snap.get('minuteBar', {}).get('c') or 0) or 0)
    prev = float((snap.get('prevDailyBar', {}).get('c') or 0) or 0)
    day_change = ((price - prev) / prev * 100.0) if prev > 0 else 0.0
    profile = get_company_profile(symbol)
    asset = get_alpaca_asset(symbol)
    classification = classify_asset(
        symbol, asset, profile,
        platform_flags={'biotech': config.BIOTECH_TRADING_ENABLED, 'etf': config.ETF_TRADING_ENABLED, 'leveraged_etf': config.LEVERAGED_ETF_TRADING_ENABLED, 'inverse_etf': config.INVERSE_ETF_TRADING_ENABLED, 'crypto_etf': config.CRYPTO_ETF_TRADING_ENABLED, 'options': config.OPTIONS_TRADING_ENABLED},
        user_flags={'biotech': bool(getattr(current_user, 'allow_biotech', True)), 'etf': bool(getattr(current_user, 'allow_etf_trading', True)), 'leveraged_etf': bool(getattr(current_user, 'allow_leveraged_etfs', False)), 'inverse_etf': bool(getattr(current_user, 'allow_inverse_etfs', False)), 'crypto_etf': bool(getattr(current_user, 'allow_crypto_etfs', True)), 'options': bool(getattr(current_user, 'allow_options_trading', False))}
    )
    rejected = prev <= 0 or not classification.get('tradable_by_xeanvi', False)
    reason = 'missing_prev_close' if prev <= 0 else classification.get('rejection_reason')
    rejection_reasons = [reason] if reason else list(classification.get('rejection_reasons') or [])
    payload = {
        'symbol': symbol, 'asset_type': classification.get('asset_type'), 'asset_type_reason': classification.get('asset_type_reason'),
        'platform_allowed': classification.get('platform_allowed'), 'user_allowed': classification.get('user_allowed'),
        'tradable_by_xeanvi': classification.get('tradable_by_xeanvi'),
        'current_price': price, 'prev_close': prev, 'day_change_pct': day_change,
        'rvol': None, 'intraday_dollar_volume': None, 'spread_pct': None, 'vwap': None, 'above_vwap': None,
        'high_of_day': None, 'distance_from_hod_pct': None, 'extended_from_vwap_pct': None,
        'catalyst_score': None, 'catalyst_source': None, 'liquidity_score': None, 'momentum_score': None,
        'setup_grade': 'NO TRADE', 'decision': 'NO TRADE' if rejected else 'WATCH',
        'buy_lower': None, 'buy_upper': None, 'entry_price': None, 'stop_price': None, 'target_1': None, 'target_2': None, 'qty': 0,
        'rejected': rejected, 'rejection_reasons': rejection_reasons, 'final_explanation': reason or 'Symbol analyzed.',
        'data_feed_used': feed, 'scan_time_et': now_et().isoformat(),
    }
    try:
        end = scanner_module.now_utc()
        bars_day = get_bars([symbol], '1Day', end - scanner_module.timedelta(days=400), end, 400, feed=feed).get(symbol, [])
        bars_min = get_bars([symbol], '1Min', end - scanner_module.timedelta(days=3), end, 1000, feed=feed).get(symbol, [])
        spy_min = get_bars(['SPY'], '1Min', end - scanner_module.timedelta(days=3), end, 1000, feed=feed).get('SPY', [])
        analyzed = analyze_symbol(symbol, snap, q, bars_day, bars_min, 0.0, profile or {}, asset or {}, spy_min, {}, {'longs_blocked': False})
        payload.update({
            'rvol': analyzed.get('details', {}).get('rvol'),
            'intraday_dollar_volume': analyzed.get('details', {}).get('liquidity', {}).get('dollar_volume'),
            'spread_pct': analyzed.get('details', {}).get('spread_pct'),
            'vwap': analyzed.get('details', {}).get('vwap_hold_reclaim', {}).get('vwap'),
            'above_vwap': analyzed.get('details', {}).get('vwap_hold_reclaim', {}).get('reclaimed_vwap'),
            'high_of_day': analyzed.get('details', {}).get('opening_range', {}).get('high_of_day'),
            'distance_from_hod_pct': analyzed.get('details', {}).get('opening_range_confirmation', {}).get('distance_from_hod_pct'),
            'extended_from_vwap_pct': analyzed.get('details', {}).get('entry_quality', {}).get('entry_extension_pct'),
            'catalyst_score': analyzed.get('scores', {}).get('catalyst'),
            'catalyst_source': analyzed.get('details', {}).get('catalyst', {}).get('model'),
            'liquidity_score': analyzed.get('scores', {}).get('liquidity'),
            'momentum_score': analyzed.get('score_total'),
            'setup_grade': analyzed.get('setup_grade'),
            'decision': analyzed.get('decision'),
            'buy_lower': analyzed.get('buy_lower'),
            'buy_upper': analyzed.get('buy_upper'),
            'entry_price': analyzed.get('entry_price'),
            'stop_price': analyzed.get('stop_price'),
            'target_1': analyzed.get('target_1'),
            'target_2': analyzed.get('target_2'),
            'qty': analyzed.get('qty'),
            'final_explanation': '; '.join(analyzed.get('details', {}).get('quick_notes', [])[:2]) or payload['final_explanation'],
        })
    except Exception:
        if 'diagnostic_data_unavailable' not in payload['rejection_reasons']:
            payload['rejection_reasons'].append('diagnostic_data_unavailable')
        if 'deep_analysis_failed' not in payload['rejection_reasons']:
            payload['rejection_reasons'].append('deep_analysis_failed')
        payload['rejected'] = True
        payload['final_explanation'] = (
            'Symbol could not be fully analyzed because market/quote/bar data was incomplete.'
        )

    payload['rejection_reasons'] = build_debug_rejection_reasons(payload)
    payload['rejected'] = bool(payload['rejection_reasons'])
    if payload.get('rejected') and not payload.get('final_explanation'):
        payload['final_explanation'] = 'Symbol is not actionable under current setup constraints.'
    payload['final_explanation'] = build_debug_final_explanation(payload)
    return jsonify(payload)

@app.route('/api/metrics')
@login_required
def api_metrics():
    """Returns the latest scan data and risk stats for the dashboard refresh."""
    latest_scan_data = None
    try:
        raw_scan = redis_client.get(f'latest_scan:{current_user.id}')
        if raw_scan:
            latest_scan_data = json.loads(raw_scan)
    except Exception as redis_exc:
        logger.warning(f"Redis cache read failed (bypassing): {redis_exc}")
    failed_trades_today = get_failed_trades_today()
    return ok({
        'latest_scan': latest_scan_data,
        'risk_controls': {
            'failed_trades_today': failed_trades_today,
            'max_failed_trades_per_day': config.MAX_FAILED_TRADES_PER_DAY,
            'can_trade_today': failed_trades_today < config.MAX_FAILED_TRADES_PER_DAY,
        }
    })


@app.route('/api/history')
def api_history():
    return ok({'scans': get_recent_scans(), 'trades': get_recent_trades(), 'failed_trades_today': get_failed_trades_today()})


@app.route('/api/chart/<symbol>')
def api_chart(symbol: str):
    try:
        user = current_user if getattr(current_user, 'is_authenticated', False) else None
        return ok(get_stock_chart_pack(symbol.upper(), user=user))
    except Exception as exc:
        return fail(str(exc), 500)


@app.route('/api/execute', methods=['POST'])
@login_required
def api_execute():
    data = request.get_json(silent=True) or {}
    symbol = str(data.get("symbol") or "").upper().strip()
    scan_id = data.get("scan_id") or data.get("scanId")

    required = ['symbol', 'entry_price', 'stop_price', 'target_1', 'target_2', 'qty', 'current_price', 'buy_upper', 'score_total', 'decision']
    missing = [k for k in required if k not in data]
    if missing:
        return fail(f'Missing fields: {", ".join(missing)}')
    if not config.MOMENTUM_AUTO_EXECUTE_ENABLED:
        return fail('Execution blocked: MOMENTUM_AUTO_EXECUTE_ENABLED is false.', 403)

    failed_today = get_failed_trades_today()
    if failed_today >= config.MAX_FAILED_TRADES_PER_DAY:
        return fail(
            f'Daily loss lock is active. You already have {failed_today} failed trades today.',
            403,
            failed_trades_today=failed_today,
            max_failed_trades_per_day=config.MAX_FAILED_TRADES_PER_DAY,
        )

    if not buy_window_open():
        return fail(f'Execution blocked until after {config.NO_BUY_BEFORE_ET} ET.', 403)

    try:
        score_total = int(data['score_total'])
        catalyst_score = int((data.get('scores') or {}).get('catalyst', 0))
        current_price = float(data['current_price'])
        entry_price = float(data['entry_price'])
        buy_upper = float(data['buy_upper'])
        stop_price = float(data['stop_price'])
        target_1 = float(data['target_1'])
        target_2 = float(data['target_2'])
        qty = int(data['qty'])
        spread_pct = float((data.get('details') or {}).get('spread_pct', 0))
        opening_confirmed = bool((data.get('details') or {}).get('opening_range_confirmation', {}).get('breakout_confirmed', False))
        vwap_reclaimed = bool((data.get('details') or {}).get('vwap_hold_reclaim', {}).get('reclaimed_vwap', False))

        if data.get('decision') == 'WAIT':
            return fail(f'Execution blocked until after {config.NO_BUY_BEFORE_ET} ET.', 403)
        setup_grade = data.get('setup_grade', 'NO TRADE')
        if setup_grade not in {'A+', 'A'}:
            return fail('Execution blocked because only A or A+ setups are allowed.', 403)
        if score_total < config.MIN_SCORE_TO_EXECUTE:
            return fail('Execution blocked because the score is too low.', 403)
        if catalyst_score < config.MIN_CATALYST_SCORE:
            return fail('Execution blocked because the catalyst score is too low.', 403)
        if spread_pct > config.MAX_SPREAD_PCT:
            return fail('Execution blocked because the spread is too wide.', 403)
        if current_price > buy_upper:
            return fail('Execution blocked because price is extended above the buy zone.', 403)
        if qty < 1:
            return fail('Execution blocked because position size is zero after risk sizing.', 403)
        if not opening_confirmed:
            return fail('Execution blocked because the opening-range breakout is not confirmed.', 403)
        if not vwap_reclaimed:
            return fail('Execution blocked because VWAP hold/reclaim is not confirmed.', 403)
        if (entry_price - stop_price) * qty > config.MAX_DOLLAR_LOSS_PER_TRADE + 0.01:
            return fail('Execution blocked because the trade risks more than the max dollar loss.', 403)

        track_user_event('automation_started', user=current_user, context={'symbol': symbol, 'scan_id': scan_id})
        guard = validate_execution_against_approved_scan(
            redis_client=redis_client,
            user=current_user,
            symbol=symbol,
            scan_id=scan_id,
        )

        dynamic_orb_state = get_dynamic_orb_metadata()

        if not guard.get("ok"):
            logger.warning(
                "LIVE_TRADE_BLOCKED user_id=%s email=%s symbol=%s scan_id=%s reason=%s",
                current_user.id,
                current_user.email,
                symbol,
                scan_id,
                guard.get("error"),
            )
            return fail(guard.get("error", "Trade blocked."), guard.get("status", 400))

        order = place_managed_entry_order(
            symbol=data['symbol'],
            qty=qty,
            entry_price=entry_price,
            stop_price=stop_price,
            target_1_price=target_1,
            target_2_price=target_2,
            user=current_user,
        )
        audit_order_result = dict(order) if isinstance(order, dict) else {"order_result": order}
        audit_order_result["dynamic_orb_state"] = dynamic_orb_state

        audit_trade_log(
            logger=logger,
            user=current_user,
            symbol=symbol,
            scan_id=scan_id,
            qty=data.get("qty"),
            entry_price=data.get("entry_price"),
            stop_price=data.get("stop_price"),
            target_1=data.get("target_1"),
            target_2=data.get("target_2"),
            order_result=audit_order_result,
            raw_json_metadata={"dynamic_orb_state": dynamic_orb_state},
        )

        risk_per_share = round(entry_price - stop_price, 2)

        setup_context = {
            'symbol': data['symbol'],
            'decision': data.get('decision', 'N/A'),
            'score_total': score_total,
            'setup_grade': setup_grade,
            'catalyst_score': catalyst_score,
            'rvol': data.get('rvol', (data.get('details') or {}).get('rvol', 'N/A')),
            'entry_price': entry_price,
            'stop_price': stop_price,
            'target_1': target_1,
            'target_2': target_2,
            'qty': qty,
            'risk_per_share': risk_per_share,
        }

        # --- NEW: Generate AI Explainability Thesis ---
        try:
            thesis_result = generate_trade_thesis(setup_context)
        except Exception as exc:
            logger.error(f"Thesis generation exception: {exc}")
            # Ultimate failsafe so /api/execute never breaks
            thesis_result = {
                "thesis": f"XeanVI is flagging this setup because {data['symbol']} has a score_total of {score_total} with setup grade {setup_grade}. This is a probability-based setup.",
                "key_reasons": ["Systematic criteria met", "Risk parameters defined"],
                "risk_note": "This is a probability-based setup, not a guaranteed outcome."
            }
        # ----------------------------------------------

        trade_payload = {
            'user_id': current_user.id,
            'scan_id': data.get('scan_id'),
            'symbol': data['symbol'],
            'side': 'buy',
            'decision': data.get('decision', 'BUY NOW'),
            'score_total': score_total,
            'current_price': current_price,
            'entry_price': entry_price,
            'buy_lower': float(data.get('buy_lower', entry_price)),
            'buy_upper': buy_upper,
            'stop_price': stop_price,
            'target_1': target_1,
            'target_2': target_2,
            'qty': qty,
            'risk_per_share': risk_per_share,
            'reward_to_target_1': round(target_1 - entry_price, 2),
            'reward_to_target_2': round(target_2 - entry_price, 2),
            'rr_ratio_1': data.get('rr_ratio_1'),
            'rr_ratio_2': data.get('rr_ratio_2'),
            'order_id': order.get('id'),
            'order_status': order.get('status'),
            'status': order.get('status') or 'pending',
            'filled_avg_price': order.get('filled_avg_price'),
            'filled_qty': order.get('filled_qty'),
            'outcome': order_outcome_from_payload(order),
            'notes': 'Pegged entry + 15s timeout + target-1 scale-out with trailing runner automation.',
            'raw_json': {
                'order_bundle': order,
                'execution_request': data,
                'ai_explainability': thesis_result,
                'dynamic_orb_state': dynamic_orb_state,
            },
        }
        raw_json = trade_payload.get('raw_json') or {}
        if not isinstance(raw_json, dict):
            raw_json = {'original_raw_json': raw_json}
        raw_json['dynamic_orb_state'] = dynamic_orb_state
        trade_payload['raw_json'] = raw_json

        trade_id = insert_trade(trade_payload)
        # --- NEW: Trigger Real-Time Notification ---
        alert_payload = {
            'action': 'trade_alert',
            'title': 'Execution Confirmed',
            'message': f'Bought {qty} shares of {data["symbol"]} at ${entry_price}. AI Stop-loss active.',
            'level': 'success',
        }
        try:
            watchlist_manager.broadcast_all(json.dumps(alert_payload))
        except Exception as e:
            logger.error(f"Failed to push ws notification: {e}")
        # -------------------------------------------
        return ok(
            {
                'trade_id': trade_id,
                'order_id': order.get('id'),
                'status': order.get('status'),
                'symbol': data['symbol'],
                'qty': qty,
                'entry_price': entry_price,
                'stop_price': stop_price,
                'target_1': target_1,
                'target_2': target_2,
                'max_dollar_loss': round((entry_price - stop_price) * qty, 2),
                'thesis': thesis_result.get('thesis', ''),
                'key_reasons': thesis_result.get('key_reasons', []),
                'risk_note': thesis_result.get('risk_note', ''),
                'risk_controls': {
                    'failed_trades_today': get_failed_trades_today(),
                    'max_failed_trades_per_day': config.MAX_FAILED_TRADES_PER_DAY,
                    'can_trade_today': get_failed_trades_today() < config.MAX_FAILED_TRADES_PER_DAY,
                    'buy_window_open': buy_window_open(),
                    'no_buy_before_et': config.NO_BUY_BEFORE_ET,
                },
            },
            history={'trades': get_recent_trades()},
        )
    except BrokerError as exc:
        return fail(str(exc))
    except Exception as exc:
        return fail(f'Execution failed: {exc}', 500)


@app.route('/api/order-status/<order_id>')
@login_required
def api_order_status(order_id: str):
    try:
        user_token = getattr(current_user, 'alpaca_access_token', None)
        trade = get_trade_by_order_id(order_id)
        if not trade:
            return fail('Trade not found for order id.', 404)
        raw = trade.get('raw_json') or '{}'
        if isinstance(raw, str):
            raw = json.loads(raw or '{}')
        bundle = raw.get('order_bundle') if isinstance(raw, dict) else None
        if not isinstance(bundle, dict):
            order = get_order(order_id, token=user_token, user=current_user)
        else:
            order = dict(bundle)
            if bundle.get('strategy') == 'target1_then_trailing_runner':
                bundle = maybe_activate_runner_trailing(
                    bundle,
                    breakeven_price=float(trade.get('entry_price') or 0),
                    token=user_token,
                    user=current_user,
                )
                order['target_1_order'] = (
                    get_order(bundle.get('target_1_order_id'), token=user_token, user=current_user)
                    if bundle.get('target_1_order_id')
                    else {}
                )
                if bundle.get('runner_trailing_order_id'):
                    order['runner_trailing_order'] = get_order(
                        bundle.get('runner_trailing_order_id'),
                        token=user_token,
                        user=current_user,
                    )
                elif bundle.get('runner_stop_order_id'):
                    order['runner_order'] = get_order(
                        bundle.get('runner_stop_order_id'),
                        token=user_token,
                        user=current_user,
                    )
                raw['order_bundle'] = bundle
        updates = {
            'order_status': order.get('status'),
            'status': order.get('status') or trade.get('status') or 'pending',
            'filled_avg_price': order.get('filled_avg_price'),
            'filled_qty': order.get('filled_qty'),
            'outcome': order_outcome_from_payload(order),
            'raw_json': raw if isinstance(raw, dict) else order,
        }
        update_trade_status(order_id, updates)

        order['risk_controls'] = {
            'failed_trades_today': get_failed_trades_today(),
            'max_failed_trades_per_day': config.MAX_FAILED_TRADES_PER_DAY,
            'can_trade_today': get_failed_trades_today() < config.MAX_FAILED_TRADES_PER_DAY,
            'buy_window_open': buy_window_open(),
            'no_buy_before_et': config.NO_BUY_BEFORE_ET,
        }
        return ok(order, history={'trades': get_recent_trades(), 'failed_trades_today': get_failed_trades_today()})
    except BrokerError as exc:
        return fail(str(exc))
    except Exception as exc:
        return fail(f'Order lookup failed: {exc}', 500)


@sock.route('/ws/watchlist')
def ws_watchlist(ws):
    try:
        watchlist_manager.stream(ws)
    except Exception:
        return


with app.app_context():
    validate_runtime_database_safety(app)
    assert_existing_production_database_has_users(db)
    db.create_all()
    ensure_schema_migrations()
    assert_not_empty_production_database(db)

@app.errorhandler(CSRFError)
def handle_csrf_error(e):
    reason = e.description or 'CSRF validation failed.'
    if request.path.startswith('/api/'):
        return jsonify({'ok': False, 'error': reason}), 400
    return "Request validation failed. Please refresh and try again.", 400


if __name__ == '__main__':
    start_engine()
    app.run(host=config.HOST, port=config.PORT, debug=config.DEBUG, use_reloader=False)
