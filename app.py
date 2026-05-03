import hashlib
import json
import logging
import os
import redis
import requests
import secrets
import sqlite3
import stripe
from urllib.parse import urlencode
from werkzeug.middleware.proxy_fix import ProxyFix

from datetime import datetime
from sqlalchemy import inspect, text
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, make_response, render_template, request, redirect, session, url_for, flash
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
import scanner as scanner_module
from broker import BrokerError, get_order, maybe_activate_runner_trailing, place_managed_entry_order
import db as trade_db
from db import get_failed_trades_today, get_recent_scans, get_recent_trades, get_trade_by_order_id, init_db, insert_scan, insert_trade, update_trade_status
from execution import start_engine
from models import db
from models import User, UserEvent, Waitlist
from onboarding import fetch_and_sync_bankroll, verify_alpaca_data_feed, detect_and_store_alpaca_connection
from scanner import ScanError, buy_window_open, get_stock_chart_pack, now_et, run_scan
from watchlist import watchlist_manager
from explainability import generate_trade_thesis
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
    storage_uri="memory://",
)

# Enforce HTTPS, HSTS, and strict Content Security Policies
if os.getenv('FLASK_ENV') == 'production':
    csp = {
        'default-src': [
            "'self'",
        ],
        'script-src': [
            "'self'",
            'https://js.stripe.com',  # Required for checkout
            "'unsafe-inline'",  # Often needed for quick inline JS like Bootstrap/Alpine
        ],
        'frame-src': [
            "'self'",
            'https://js.stripe.com',
        ],
    }
    Talisman(app, content_security_policy=csp)

# THE FIX: Allow login even if the host/referrer strings have a proxy-induced mismatch
app.config['WTF_CSRF_SSL_STRICT'] = True

# Ensure these remain bulletproof
app.config['SESSION_COOKIE_DOMAIN'] = '.xeanvi.com'
app.config['SESSION_COOKIE_SECURE'] = True
app.config['WTF_CSRF_TRUSTED_ORIGINS'] = [
    'xeanvi.com',
    'www.xeanvi.com',
    'https://xeanvi.com',
    'https://www.xeanvi.com'
]

app.config['SECRET_KEY'] = config.SECRET_KEY
# Force SQLAlchemy to use the exact same database file as your raw SQLite connections
app.config['SQLALCHEMY_DATABASE_URI'] = f"sqlite:///{os.path.abspath(config.DB_PATH)}"
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
redis_client = redis.Redis.from_url(os.getenv('REDIS_URL', 'redis://localhost:6379/0'), decode_responses=True)
ADMIN_EMAIL = os.getenv('ADMIN_EMAIL', '').strip().lower()

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


def ensure_db_initialized() -> None:
    try:
        init_db()
        return
    except (sqlite3.OperationalError, PermissionError) as exc:
        fallback_dir = os.getenv('DB_FALLBACK_DIR', '/tmp')
        fallback_path = os.path.join(fallback_dir, 'veteran_trades.db')
        logger.warning('Primary DB path failed (%s). Falling back to %s. Error: %s', config.DB_PATH, fallback_path, exc)
        config.DB_PATH = fallback_path
        trade_db.config.DB_PATH = fallback_path
        init_db()


VALID_REFRESH_INTERVALS = {10000, 30000, 60000}


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

    user = User.query.get(int(user_id))

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

        logger.error('Brevo password reset email failed: %s %s', response.status_code, response.text)
        return False

    except Exception as exc:
        logger.error('Brevo password reset email exception: %s', exc)
        return False


def add_signup_user_to_brevo(user):
    api_key = getattr(config, 'BREVO_API_KEY', None) or os.getenv('BREVO_API_KEY')
    list_id = getattr(config, 'BREVO_SIGNUP_LIST_ID', 0)

    if not api_key:
        logger.error('Brevo signup automation skipped: missing BREVO_API_KEY for user_id=%s', user.id)
        return False

    if not list_id:
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

        logger.error('Brevo signup automation failed for user_id=%s status=%s response=%s', user.id, response.status_code, response.text)
        return False
    except Exception as exc:
        logger.error('Brevo signup automation exception for user_id=%s: %s', user.id, exc)
        return False



def ensure_schema_migrations() -> None:
    """Safely backfill schema missing from older SQLite DBs using the existing SQLAlchemy pool."""
    inspector = inspect(db.engine)
    table_names = inspector.get_table_names()

    with db.engine.connect() as conn:
        if 'user' in table_names:
            existing_columns = {col['name'] for col in inspector.get_columns('user')}

            if 'refresh_interval' not in existing_columns:
                conn.execute(text("ALTER TABLE user ADD COLUMN refresh_interval INTEGER NOT NULL DEFAULT 30000"))

            if 'show_news' not in existing_columns:
                conn.execute(text("ALTER TABLE user ADD COLUMN show_news BOOLEAN NOT NULL DEFAULT 1"))
                conn.execute(text("ALTER TABLE user ADD COLUMN show_watchlist BOOLEAN NOT NULL DEFAULT 1"))
                conn.execute(text("ALTER TABLE user ADD COLUMN show_terminal BOOLEAN NOT NULL DEFAULT 1"))

            if 'esg_fossil_fuels' not in existing_columns:
                conn.execute(text("ALTER TABLE user ADD COLUMN esg_fossil_fuels BOOLEAN NOT NULL DEFAULT 0"))
                conn.execute(text("ALTER TABLE user ADD COLUMN esg_weapons BOOLEAN NOT NULL DEFAULT 0"))
                conn.execute(text("ALTER TABLE user ADD COLUMN esg_tobacco BOOLEAN NOT NULL DEFAULT 0"))
                conn.execute(text("ALTER TABLE user ADD COLUMN exclude_penny_stocks BOOLEAN NOT NULL DEFAULT 1"))
                conn.execute(text("ALTER TABLE user ADD COLUMN exclude_biotech BOOLEAN NOT NULL DEFAULT 0"))

            if 'trading_mode' not in existing_columns:
                conn.execute(text("ALTER TABLE user ADD COLUMN trading_mode VARCHAR(20) NOT NULL DEFAULT 'paper'"))

            if 'alpaca_data_feed' not in existing_columns:
                conn.execute(text("ALTER TABLE user ADD COLUMN alpaca_data_feed VARCHAR(10) NOT NULL DEFAULT 'iex'"))

            if 'alpaca_paper_access_token' not in existing_columns:
                conn.execute(text("ALTER TABLE user ADD COLUMN alpaca_paper_access_token TEXT"))

            if 'alpaca_live_access_token' not in existing_columns:
                conn.execute(text("ALTER TABLE user ADD COLUMN alpaca_live_access_token TEXT"))

            if 'alpaca_paper_account_id' not in existing_columns:
                conn.execute(text("ALTER TABLE user ADD COLUMN alpaca_paper_account_id VARCHAR(100)"))

            if 'alpaca_live_account_id' not in existing_columns:
                conn.execute(text("ALTER TABLE user ADD COLUMN alpaca_live_account_id VARCHAR(100)"))

            if 'paper_bankroll' not in existing_columns:
                conn.execute(text("ALTER TABLE user ADD COLUMN paper_bankroll FLOAT NOT NULL DEFAULT 0.0"))

            if 'live_bankroll' not in existing_columns:
                conn.execute(text("ALTER TABLE user ADD COLUMN live_bankroll FLOAT NOT NULL DEFAULT 0.0"))

            if 'onboarding_completed' not in existing_columns:
                conn.execute(text("ALTER TABLE user ADD COLUMN onboarding_completed BOOLEAN NOT NULL DEFAULT 0"))

            if 'paper_bankroll_set' not in existing_columns:
                conn.execute(text("ALTER TABLE user ADD COLUMN paper_bankroll_set BOOLEAN NOT NULL DEFAULT 0"))

            if 'first_scan_completed' not in existing_columns:
                conn.execute(text("ALTER TABLE user ADD COLUMN first_scan_completed BOOLEAN NOT NULL DEFAULT 0"))

            if 'playbook_reviewed' not in existing_columns:
                conn.execute(text("ALTER TABLE user ADD COLUMN playbook_reviewed BOOLEAN NOT NULL DEFAULT 0"))

            if 'transparency_reviewed' not in existing_columns:
                conn.execute(text("ALTER TABLE user ADD COLUMN transparency_reviewed BOOLEAN NOT NULL DEFAULT 0"))

            if 'broker_connection_started' not in existing_columns:
                conn.execute(text("ALTER TABLE user ADD COLUMN broker_connection_started BOOLEAN NOT NULL DEFAULT 0"))

        if 'trades' in table_names:
            trade_columns = {col['name'] for col in inspector.get_columns('trades')}

            if 'exit_price' not in trade_columns:
                conn.execute(text("ALTER TABLE trades ADD COLUMN exit_price FLOAT"))

            if 'pnl' not in trade_columns:
                conn.execute(text("ALTER TABLE trades ADD COLUMN pnl FLOAT"))

            if 'pnl_source' not in trade_columns:
                conn.execute(text("ALTER TABLE trades ADD COLUMN pnl_source VARCHAR(64)"))

            if 'closed_at' not in trade_columns:
                conn.execute(text("ALTER TABLE trades ADD COLUMN closed_at DATETIME"))

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

    # 2. Check for the secret session flag
    if session.get('dev_access'):
        return render_template('landing.html')

    # 3. Default for the public: the Coming Soon/Waitlist page
    return render_template('waitlist.html')


@app.route('/dev-unlock/<token>')
def dev_unlock(token):
    # Check if the token matches your .env setting
    if token == os.getenv('DEV_BYPASS_TOKEN', 'fallback_secret'):
        session['dev_access'] = True
        flash("Developer access granted. Waitlist bypassed.", "success")
        return redirect(url_for('index'))
    return "Unauthorized", 403


@app.route('/join-waitlist', methods=['POST'])
def join_waitlist():
    email = request.form.get('email', '').strip().lower()
    
    if not email:
        flash("A valid email is required.", "error")
        return redirect(url_for('index'))

    # 1. Local Tracking (Wrapped in safety net)
    try:
        existing = Waitlist.query.filter_by(email=email).first()
        if not existing:
            is_early = Waitlist.query.count() < 25
            db.session.add(Waitlist(email=email, is_early_bird=is_early))
            db.session.commit()
    except Exception as e:
        logger.error(f"Database Waitlist Error: {e}")
        db.session.rollback()

    # 2. PULL FROM CONFIG MODULE
    api_key = getattr(config, 'BREVO_API_KEY', None) or os.getenv('BREVO_API_KEY')
    
    if not api_key:
        logger.error("CRITICAL: BREVO_API_KEY is missing from environment variables!")
        flash("System Configuration Error: Missing API Key.", "error")
        return redirect(url_for('index'))

    # 3. Brevo API Execution
    url = "https://api.brevo.com/v3/contacts"
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "api-key": api_key
    }
    
    payload = {
        "email": email,
        "listIds": [5], 
        "updateEnabled": True
    }
    
    try:
        response = requests.post(url, json=payload, headers=headers)
        
        if response.status_code in [200, 201, 204]:
            flash("You've been successfully added to the priority waitlist.", "success")
        else:
            logger.error(f"Brevo API Rejected: {response.text}")
            flash(f"Brevo Error: We could not secure your spot.", "error")
            
    except Exception as e:
        logger.error(f"Brevo Connection Failed: {e}")
        flash("System connection error. Please try again.", "error")
        
    return redirect(url_for('index'))


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

        email = request.form.get('email')
        password = request.form.get('password')

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
        track_user_event('signup_completed', user=new_user, context={'plan': intended_plan or ''})
        add_signup_user_to_brevo(new_user)
        login_user(new_user)

        # REDIRECT LOGIC: If they chose a plan, send them to upgrade first
        if intended_plan in ['monthly', 'annual']:
            return redirect(url_for('upgrade', plan=intended_plan))

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


@app.route('/terms')
def terms():
    return render_template('terms.html')


@app.route('/privacy')
def privacy():
    return render_template('privacy.html')


@app.route('/faq')
def faq():
    return render_template('faq.html')


@app.route('/sitemap.xml')
def sitemap_xml():
    """Generates the XML sitemap for search engines."""
    links = []
    # Using the same strict exclusion list to hide the dashboard and back-end
    excluded_endpoints = [
        'static', 'sitemap_xml', 'robots_txt', 'api_runtime_health', 'dev_unlock',
        'stripe_webhook', 'create_checkout_session', 'checkout_redirect',
        'ws_watchlist', 'api_scan', 'api_metrics', 'api_history',
        'api_chart', 'api_execute', 'api_order_status', 'api_transparency_stats',
        'dashboard', 'onboarding', 'settings', 'logout', 'upgrade',
        'learn', 'learn_topic', 'transparency', 'join_waitlist',
        'alpaca_login', 'alpaca_logout', 'alpaca_callback', 'sandbox_callback',
        'forgot_password', 'reset_password_with_token',
    ]

    # Use 'https' and your actual domain for the sitemap links
    base_url = "https://xeanvi.com"

    for rule in app.url_map.iter_rules():
        if "GET" in rule.methods and rule.endpoint not in excluded_endpoints and not rule.arguments:
            try:
                url = f"{base_url}{url_for(rule.endpoint)}"
                # Defaulting to today's date for indexing freshness
                lastmod = datetime.now().strftime('%Y-%m-%d')
                links.append((url, lastmod))
            except Exception as e:
                logger.error(f"XML Sitemap Error for {rule.endpoint}: {e}")
                continue

    # Build the XML structure
    sitemap_xml_content = render_template('sitemap_xml.xml', links=links)
    response = make_response(sitemap_xml_content)
    response.headers["Content-Type"] = "application/xml"
    return response


@app.route('/robots.txt')
def robots_txt():
    """Updated to point to the new XML sitemap."""
    lines = [
        "User-agent: *",
        "Disallow: /dashboard",
        "Disallow: /api/",
        "Disallow: /settings",
        "Disallow: /logout",
        "Disallow: /alpaca/",
        "Sitemap: https://xeanvi.com/sitemap.xml",  # Pointing to the XML file
    ]
    return "\n".join(lines), 200, {'Content-Type': 'text/plain'}



@app.route('/learn')
@login_required
def learn():
    # In the future, you can track 'completed_lessons' in the DB
    return render_template('learn.html', current_user=current_user)


@app.route('/learn/<topic>')
@login_required
def learn_topic(topic):
    # This dynamic route allows lesson pages like /learn/rvol or /learn/risk-management
    return render_template(f'lessons/{topic}.html', current_user=current_user)

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


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))



def user_has_alpaca_paper_connection(user: User) -> bool:
    return bool(
        getattr(user, 'alpaca_paper_account_id', None)
        or getattr(user, 'alpaca_paper_access_token', None)
        or getattr(user, 'alpaca_access_token', None)
    )


def get_user_setup_checklist(user: User) -> dict:
    items = [
        {
            'field': 'onboarding_completed',
            'label': 'Paper-mode risk setup completed',
            'short_label': 'Paper Setup',
            'description': 'Accept the trading risk acknowledgment, choose paper mode, and set your starting paper bankroll.',
            'completed': bool(user.onboarding_completed),
            'required': True,
            'optional': False,
            'url': url_for('onboarding'),
            'action_label': 'Start Paper Setup',
            'completed_action_label': 'Review Paper Setup',
            'completed_note': 'Paper-mode setup is complete. Your account starts in paper mode so you can test rules before broker-connected workflows.',
            'icon': 'fa-shield-halved',
        },
        {
            'field': 'paper_bankroll_set',
            'label': 'Paper bankroll configured',
            'short_label': 'Paper Bankroll',
            'description': 'Set your paper bankroll and risk sizing baseline in settings.',
            'completed': bool(user.paper_bankroll_set or (user.paper_bankroll or 0) > 0),
            'required': True,
            'optional': False,
            'url': url_for('settings'),
            'action_label': 'Open Settings',
            'completed_action_label': 'Open Again',
            'completed_note': 'Paper bankroll baseline is set for risk controls.',
            'icon': 'fa-wallet',
        },
        {
            'field': 'playbook_reviewed',
            'label': 'Trading playbook reviewed',
            'short_label': 'Playbook',
            'description': 'Review the user-defined rules that govern signal and execution behavior.',
            'completed': bool(user.playbook_reviewed),
            'required': True,
            'optional': False,
            'url': url_for('playbook'),
            'action_label': 'Review Playbook',
            'completed_action_label': 'Open Again',
            'completed_note': 'Playbook has been reviewed and acknowledged.',
            'icon': 'fa-book-open',
        },
        {
            'field': 'first_scan_completed',
            'label': 'First paper scan completed',
            'short_label': 'First Scan',
            'description': 'Run one paper scan to validate scanner output and workflow readiness.',
            'completed': bool(user.first_scan_completed),
            'required': True,
            'optional': False,
            'url': url_for('dashboard'),
            'action_label': 'Open Scanner',
            'completed_action_label': 'Open Again',
            'completed_note': 'Initial paper scan is complete.',
            'icon': 'fa-radar',
        },
        {
            'field': 'transparency_reviewed',
            'label': 'Transparency rules reviewed',
            'short_label': 'Transparency',
            'description': 'Review model logic, safeguards, and reporting transparency rules.',
            'completed': bool(user.transparency_reviewed),
            'required': True,
            'optional': False,
            'url': url_for('transparency'),
            'action_label': 'View Transparency',
            'completed_action_label': 'Open Again',
            'completed_note': 'Transparency rules have been reviewed.',
            'icon': 'fa-circle-info',
        },
        {
            'field': 'alpaca_paper_connected',
            'label': 'Alpaca paper account connected',
            'short_label': 'Alpaca Paper',
            'description': 'Connect your Alpaca paper account so XeanVI can route paper-mode orders through Alpaca’s paper trading environment.',
            'completed': user_has_alpaca_paper_connection(user) or bool(user.broker_connection_started and getattr(user, 'alpaca_paper_account_id', None)),
            'required': True,
            'optional': False,
            'url': url_for('onboarding'),
            'action_label': 'Connect Alpaca Paper',
            'completed_action_label': 'Review Connection',
            'completed_note': 'Your Alpaca paper account is connected. Paper-mode order routing can now use Alpaca’s paper trading environment.',
            'icon': 'fa-plug',
        },
    ]
    total_required = sum(1 for item in items if item['required'])
    completed_required = sum(1 for item in items if item['required'] and item['completed'])
    percent_complete = int(round((completed_required / total_required) * 100)) if total_required else 0
    core_complete = completed_required == total_required
    return {
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
    )


@app.route('/upgrade')
@login_required
def upgrade():
    track_user_event('upgrade_page_viewed', user=current_user)
    # If they are already PRO, don't let them buy it again!
    if current_user.subscription_status == 'pro':
        flash("You are already a PRO member. Your automation tools are unlocked.", "success")
        return redirect(url_for('dashboard'))

    return render_template('upgrade.html', current_user=current_user)


@app.route('/api/create-checkout-session', methods=['GET', 'POST'])
@login_required
def create_checkout_session():
    # Check both form data (POST) and URL parameters (GET) for the plan
    plan = request.form.get('plan') or request.args.get('plan') or 'monthly'

    # Retrieve Price IDs from config.py
    price_id = (
        config.STRIPE_PRICE_ID_ANNUAL if plan == 'annual'
        else config.STRIPE_PRICE_ID_MONTHLY
    )

    # CRITICAL: If Price IDs are missing in .env, redirect back with an error
    if not price_id:
        flash("Billing setup is incomplete (Missing Price IDs). Please check your .env file.", "error")
        return redirect(url_for('upgrade'))

    try:
        track_user_event('checkout_started', user=current_user, context={'plan': plan})
        checkout_session = stripe.checkout.Session.create(
            customer_email=current_user.email,
            client_reference_id=str(current_user.id),
            payment_method_types=['card'],
            line_items=[{'price': price_id, 'quantity': 1}],
            mode='subscription',
            success_url=url_for('onboarding', _external=True) + '?session_id={CHECKOUT_SESSION_ID}',
            cancel_url=url_for('upgrade', _external=True),
        )
        return redirect(checkout_session.url, code=303)
    except Exception as e:
        # Log the actual Stripe error to your console for debugging
        logger.error(f"Stripe Session Error: {str(e)}")
        flash(f"Stripe Error: {str(e)}", "error")
        return redirect(url_for('upgrade'))


@app.route('/checkout-redirect')
@login_required
def checkout_redirect():
    plan = request.args.get('plan', 'monthly')
    price_id = (
        config.STRIPE_PRICE_ID_ANNUAL
        if plan == 'annual'
        else config.STRIPE_PRICE_ID_MONTHLY
    )

    if not price_id:
        flash("Billing is temporarily unavailable. Please contact support.", "error")
        return redirect(url_for('upgrade'))

    try:
        track_user_event('checkout_started', user=current_user, context={'plan': plan, 'source': 'checkout_redirect'})
        checkout_session = stripe.checkout.Session.create(
            customer_email=current_user.email,
            client_reference_id=str(current_user.id),
            payment_method_types=['card'],
            line_items=[{'price': price_id, 'quantity': 1}],
            mode='subscription',
            success_url=url_for('onboarding', _external=True) + '?session_id={CHECKOUT_SESSION_ID}',
            cancel_url=url_for('upgrade', _external=True),
        )
        return redirect(checkout_session.url, code=303)
    except Exception as exc:
        logger.error("Stripe Error: %s", exc)
        return redirect(url_for('dashboard'))


@app.route('/api/stripe-webhook', methods=['POST'])
@csrf.exempt
def stripe_webhook():
    payload = request.get_data()
    sig_header = request.headers.get('Stripe-Signature')
    endpoint_secret = config.STRIPE_WEBHOOK_SECRET

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, endpoint_secret
        )
    except (ValueError, stripe.error.SignatureVerificationError) as exc:
        return jsonify({'error': str(exc)}), 400

    if event['type'] == 'checkout.session.completed':
        checkout_session = event['data']['object']
        client_ref_id = getattr(checkout_session, 'client_reference_id', None)

        if client_ref_id:
            user = User.query.get(int(client_ref_id))
        else:
            customer_email = getattr(checkout_session, 'customer_email', None)
            user = User.query.filter_by(email=customer_email).first()

        if user:
            user.subscription_status = 'pro'
            db.session.commit()
            logger.info("User %s upgraded to PRO via Stripe.", user.email)

    elif event['type'] == 'customer.subscription.deleted':
        subscription = event['data']['object']
        customer = stripe.Customer.retrieve(subscription['customer'])
        user = User.query.filter_by(email=customer.email).first()
        if user:
            user.subscription_status = 'free'
            db.session.commit()
            logger.info("User %s downgraded to FREE (Subscription Ended).", customer.email)

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
        current_user.onboarding_completed = True
        current_user.paper_bankroll_set = starting_bankroll > 0
        db.session.commit()
        track_user_event('onboarding_completed', user=current_user, context={'starting_bankroll': starting_bankroll})

        flash('Paper-mode risk setup saved.', 'success')
        return redirect(url_for('setup_checklist'))

    return render_template(
        'onboarding.html',
        current_user=current_user,
        setup_checklist=setup_checklist,
    )

@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    if request.method == 'POST':
        # 1. Update Core Settings
        new_bankroll = float(request.form.get('bankroll', 0.0))
        if current_user.trading_mode == 'live':
            current_user.live_bankroll = new_bankroll
        else:
            current_user.paper_bankroll = new_bankroll
            current_user.paper_bankroll_set = new_bankroll > 0
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

    return render_template('settings.html', current_user=current_user)

@app.route('/alpaca/login')
@login_required
def alpaca_login():
    setup_checklist = get_user_setup_checklist(current_user)
    if not current_user.onboarding_completed:
        flash('Complete onboarding before connecting your Alpaca paper account.', 'error')
        return redirect(url_for('onboarding'))

    current_user.broker_connection_started = True
    db.session.commit()
    track_user_event('broker_connection_started', user=current_user)
    oauth_state = secrets.token_urlsafe(32)
    session['oauth_state'] = oauth_state

    params = {
        'response_type': 'code',
        'client_id': app.config['ALPACA_CLIENT_ID'],
        'redirect_uri': app.config['ALPACA_REDIRECT_URI'],
        'scope': 'trading',
        'state': oauth_state,
        'env': 'paper',
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
    return ok({'step': step, 'setup_checklist': get_user_setup_checklist(current_user)})




@app.route('/api/admin/conversion-summary')
@login_required
def api_admin_conversion_summary():
    if not ADMIN_EMAIL or (current_user.email or '').strip().lower() != ADMIN_EMAIL:
        return fail('Forbidden', 403)

    rows = db.session.query(UserEvent.event_name, db.func.count(UserEvent.id)).group_by(UserEvent.event_name).all()
    counts = {event_name: count for event_name, count in rows}
    return ok({'counts': counts})


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
    if new_mode == 'live' and current_user.subscription_status != 'pro':
        return jsonify({
            'ok': False,
            'status': 'error',
            'message': 'PRO_UPGRADE_REQUIRED: Live execution is a premium feature.'
        }), 403

    # Ensure broker is connected for Paper mode
    if new_mode == 'paper' and not user_has_alpaca_paper_connection(current_user):
        return jsonify({
            'ok': False,
            'status': 'error',
            'message': 'PAPER_BROKER_LINK_REQUIRED: Connect your Alpaca Paper account first.'
        }), 400

    # Ensure broker is connected for Live mode
    if new_mode == 'live' and not current_user.alpaca_live_access_token:
        return jsonify({
            'ok': False,
            'status': 'error',
            'message': 'LIVE_BROKER_LINK_REQUIRED: Connect your Alpaca Live account first.'
        }), 400

    # Save the mode FIRST. This is the most important part.
    current_user.trading_mode = new_mode
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
        # --- ADD THESE DEBUG PRINTS ---
        print("\n--- INITIATING ALPACA TOKEN EXCHANGE ---")
        print(f"TARGET URL: {token_url}")
        print(f"PAYLOAD SENT: {payload}")

        response = requests.post(token_url, data=payload, timeout=15)

        print(f"RESPONSE STATUS: {response.status_code}")
        print(f"RAW ALPACA RESPONSE: {response.text}")
        print("----------------------------------------\n")
        # ------------------------------

        if response.status_code != 200:
            logger.error(f"Alpaca Rejection: {response.text}") #
            flash(f"Alpaca rejected the exchange. Error: {response.text}", "error")
            return redirect(url_for('settings'))

        data = response.json()
        if 'access_token' in data:
            token = data['access_token']

            connection_result = detect_and_store_alpaca_connection(current_user, token)

            connected_parts = []
            if connection_result.get("paper_connected"):
                connected_parts.append("Paper")
            if connection_result.get("live_connected"):
                connected_parts.append("Live")

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
            flash(f"OAuth Error: {data.get('error_description', 'Unknown error')}", "error")
    except Exception as e:
        logger.error(f"Token Exchange System Error: {str(e)}")
        flash(f"System Error: {str(e)}", "error")

    return redirect(url_for('settings'))


@app.route('/v1/oauth/callback')
def sandbox_callback():
    return alpaca_callback()  # This acts as an alias


@app.route('/alpaca/logout')
@login_required
def alpaca_logout():
    current_user.alpaca_paper_access_token = None
    current_user.alpaca_live_access_token = None
    current_user.alpaca_paper_account_id = None
    current_user.alpaca_live_account_id = None
    current_user._alpaca_access_token = None
    current_user.alpaca_account_id = None
    current_user.paper_bankroll = 0.0
    current_user.live_bankroll = 0.0
    current_user.bankroll = 0.0
    db.session.commit()
    flash('Alpaca accounts disconnected.', 'success')
    return redirect(url_for('dashboard'))




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
        risk_controls = {
            'failed_trades_today': get_failed_trades_today(),
            'max_failed_trades_per_day': config.MAX_FAILED_TRADES_PER_DAY,
            'buy_window_open': buy_window_open(),
            'no_buy_before_et': config.NO_BUY_BEFORE_ET,
        }
        result['risk_controls'] = risk_controls
        scan_id = insert_scan(result)
        result['scan_id'] = scan_id
        if not current_user.first_scan_completed:
            current_user.first_scan_completed = True
            db.session.commit()

        approved_plan = approve_scan_for_user(redis_client, current_user, result)
        result["approved_execution_plan"] = approved_plan
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

    # --- FREEMIUM GATE ---
    # Assuming you have a toggle for 'trading_mode' (paper vs live)
    trading_mode = getattr(current_user, 'trading_mode', 'paper')

    if trading_mode == 'live' and current_user.subscription_status == 'free':
        return fail(
            'Live Execution is a PRO feature. Upgrade to unlock real-money automated trading.',
            403,
            needs_upgrade=True,
        )
    # ---------------------

    required = ['symbol', 'entry_price', 'stop_price', 'target_1', 'target_2', 'qty', 'current_price', 'buy_upper', 'score_total', 'decision']
    missing = [k for k in required if k not in data]
    if missing:
        return fail(f'Missing fields: {", ".join(missing)}')

    if not user_has_alpaca_paper_connection(current_user):
        return fail('Connect your Alpaca paper account before routing paper orders.', 400)

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

        guard = validate_execution_against_approved_scan(
            redis_client=redis_client,
            user=current_user,
            symbol=symbol,
            scan_id=scan_id,
        )
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
            order_result=order,
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
                'ai_explainability': thesis_result
            },
        }
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
    db.create_all() # This creates the 'user' table first
    ensure_schema_migrations()

@app.errorhandler(CSRFError)
def handle_csrf_error(e):
    return f"CRITICAL CSRF FAILURE: {e.description}", 400


if __name__ == '__main__':
    start_engine()
    app.run(host=config.HOST, port=config.PORT, debug=config.DEBUG, use_reloader=False)
