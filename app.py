import json
import logging
import os
import redis
import requests
import secrets
import sqlite3
import stripe
import threading
from urllib.parse import urlencode
from werkzeug.middleware.proxy_fix import ProxyFix

from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, render_template, request, redirect, session, url_for, flash
from flask_login import login_user, logout_user, current_user, login_required
from flask_login import LoginManager
from flask_sock import Sock
from flask_talisman import Talisman
from flask_wtf.csrf import CSRFError, CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.security import generate_password_hash, check_password_hash

import config
import scanner as scanner_module
from broker import BrokerError, get_order, maybe_activate_runner_trailing, place_managed_entry_order
import db as trade_db
from db import get_failed_trades_today, get_recent_scans, get_recent_trades, get_trade_by_order_id, init_db, insert_scan, insert_trade, update_trade_status
from execution import start_engine
from models import db
from models import User, Waitlist
from onboarding import fetch_and_sync_bankroll, verify_alpaca_data_feed
from scanner import buy_window_open, get_stock_chart_pack, now_et
from tasks import async_run_scan_task
from watchlist import watchlist_manager

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


def ensure_user_refresh_interval_column() -> None:
    """Backfill schema for existing SQLite DBs that predate refresh_interval."""
    conn = sqlite3.connect(config.DB_PATH)
    try:
        cursor = conn.execute("PRAGMA table_info(user)")
        existing_columns = {row[1] for row in cursor.fetchall()}
        if 'refresh_interval' not in existing_columns:
            conn.execute(
                "ALTER TABLE user ADD COLUMN refresh_interval INTEGER NOT NULL DEFAULT 30000"
            )
            conn.commit()
    finally:
        conn.close()


def ensure_user_layout_columns() -> None:
    """Backfill schema for dashboard layout toggles on older SQLite DBs."""
    conn = sqlite3.connect(config.DB_PATH)
    try:
        cursor = conn.execute("PRAGMA table_info(user)")
        existing_columns = {row[1] for row in cursor.fetchall()}
        additions = {
            'show_news': "ALTER TABLE user ADD COLUMN show_news BOOLEAN NOT NULL DEFAULT 1",
            'show_watchlist': "ALTER TABLE user ADD COLUMN show_watchlist BOOLEAN NOT NULL DEFAULT 1",
            'show_terminal': "ALTER TABLE user ADD COLUMN show_terminal BOOLEAN NOT NULL DEFAULT 1",
        }
        for column_name, ddl in additions.items():
            if column_name not in existing_columns:
                conn.execute(ddl)
        conn.commit()
    finally:
        conn.close()


def ensure_user_personalization_columns() -> None:
    """Backfill schema for ESG and risk personalization toggles."""
    conn = sqlite3.connect(config.DB_PATH)
    try:
        cursor = conn.execute("PRAGMA table_info(user)")
        existing_columns = {row[1] for row in cursor.fetchall()}
        additions = {
            'esg_fossil_fuels': "ALTER TABLE user ADD COLUMN esg_fossil_fuels BOOLEAN NOT NULL DEFAULT 0",
            'esg_weapons': "ALTER TABLE user ADD COLUMN esg_weapons BOOLEAN NOT NULL DEFAULT 0",
            'esg_tobacco': "ALTER TABLE user ADD COLUMN esg_tobacco BOOLEAN NOT NULL DEFAULT 0",
            'exclude_penny_stocks': "ALTER TABLE user ADD COLUMN exclude_penny_stocks BOOLEAN NOT NULL DEFAULT 1",
            'exclude_biotech': "ALTER TABLE user ADD COLUMN exclude_biotech BOOLEAN NOT NULL DEFAULT 0",
        }
        for column_name, ddl in additions.items():
            if column_name not in existing_columns:
                conn.execute(ddl)
        conn.commit()
    finally:
        conn.close()

def ensure_user_alpaca_data_feed_column() -> None:
    """Backfill schema for per-user Alpaca market-data feed preference."""
    conn = sqlite3.connect(config.DB_PATH)
    try:
        cursor = conn.execute("PRAGMA table_info(user)")
        existing_columns = {row[1] for row in cursor.fetchall()}
        if 'alpaca_data_feed' not in existing_columns:
            conn.execute("ALTER TABLE user ADD COLUMN alpaca_data_feed VARCHAR(10) NOT NULL DEFAULT 'iex'")
            conn.commit()
    finally:
        conn.close()


ensure_db_initialized()

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
    # 1. If you are logged in, you always go to the dashboard
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))

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
    """Allows logged-in users to view the pricing tier list without being bounced."""
    return render_template('landing.html')


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






@app.route('/features')
def features():
    return render_template('features.html')

@app.route('/playbook')
def playbook():
    """Public strategy page explaining the 'Screen, Validate, Execute' workflow."""
    return render_template('playbook.html')


@app.route('/broker-integration')
def broker_integration():
    return render_template('broker_integration.html')


@app.route('/alpaca-integration')
def alpaca_integration_legacy_redirect():
    """Legacy route kept for backward compatibility."""
    return redirect(url_for('broker_integration'), code=301)

@app.route('/terms')
def terms():
    return render_template('terms.html')


@app.route('/privacy')
def privacy():
    return render_template('privacy.html')


@app.route('/faq')
def faq():
    return render_template('faq.html')



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
    return render_template('transparency.html', current_user=current_user)


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


@app.route('/dashboard')
@login_required
def dashboard():
    # Clean, quiet entry into the command center
    return render_template('dashboard.html', current_user=current_user)


@app.route('/upgrade')
@login_required
def upgrade():
    # If they are already PRO, don't let them buy it again!
    if current_user.subscription_status == 'pro':
        flash("You are already a PRO member. Your AI execution is unlocked.", "success")
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
        checkout_session = stripe.checkout.Session.create(
            customer_email=current_user.email,
            payment_method_types=['card'],
            line_items=[{'price': price_id, 'quantity': 1}],
            mode='subscription',
            success_url=url_for('dashboard', _external=True) + '?session_id={CHECKOUT_SESSION_ID}',
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
        checkout_session = stripe.checkout.Session.create(
            customer_email=current_user.email,
            payment_method_types=['card'],
            line_items=[{'price': price_id, 'quantity': 1}],
            mode='subscription',
            success_url=url_for('dashboard', _external=True) + '?session_id={CHECKOUT_SESSION_ID}',
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
        customer_email = checkout_session.get('customer_email')
        user = User.query.filter_by(email=customer_email).first()
        if user:
            user.subscription_status = 'pro'
            db.session.commit()
            logger.info("User %s upgraded to PRO via Stripe.", customer_email)

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
    if request.method == 'POST':
        # Ensure the risk checkbox was checked
        if not request.form.get('risk_ack'):
            flash('You must acknowledge the trading risks to proceed.', 'error')
            return redirect(url_for('onboarding'))

        current_user.bankroll = float(request.form.get('bankroll', 5000.0))
        current_user.trading_mode = 'paper'
        # Optional: Add a 'risk_acknowledged' timestamp to your User model
        db.session.commit()

        flash('Risk protocols accepted. Welcome to the Command Center.', 'success')
        return redirect(url_for('dashboard'))

    return render_template('onboarding.html', current_user=current_user)

@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    if request.method == 'POST':
        # 1. Update Core Settings
        current_user.bankroll = float(request.form.get('bankroll', 0.0))
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
            current_user.alpaca_access_token = data['access_token']
            current_user.alpaca_account_id = data.get('account_id')
            verify_alpaca_data_feed(current_user)
            fetch_and_sync_bankroll(current_user)
            db.session.commit()
            flash("Broker connected and bankroll synced!", "success")
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
    current_user.alpaca_access_token = None
    current_user.alpaca_account_id = None
    db.session.commit()
    flash('Alpaca account disconnected.', 'success')
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
    async_run_scan_task.delay(current_user.id)
    return jsonify({
        'ok': True,
        'message': 'Scan dispatched to cluster. Results will arrive via WebSocket.',
    }), 202


@app.route('/api/metrics')
@login_required
def api_metrics():
    """Returns the latest scan data and risk stats for the dashboard refresh."""
    raw_scan = redis_client.get('latest_scan')
    latest_scan_data = json.loads(raw_scan) if raw_scan else None
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

        order = place_managed_entry_order(
            symbol=data['symbol'],
            qty=qty,
            entry_price=entry_price,
            stop_price=stop_price,
            target_1_price=target_1,
            target_2_price=target_2,
            user=current_user,
        )
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
            'risk_per_share': float(data.get('risk_per_share', 0)),
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


def start_redis_ws_listener():
    def listener():
        pubsub = redis_client.pubsub()
        pubsub.subscribe('ws_broadcast')
        for message in pubsub.listen():
            # When Celery finishes and publishes, this grabs it and pushes to WebSockets
            if message['type'] == 'message':
                watchlist_manager.broadcast_all(message['data'])

    t = threading.Thread(target=listener, daemon=True)
    t.start()


start_redis_ws_listener()


@sock.route('/ws/watchlist')
def ws_watchlist(ws):
    try:
        watchlist_manager.stream(ws)
    except Exception:
        return


with app.app_context():
    db.create_all() # This creates the 'user' table first
    ensure_user_refresh_interval_column()
    ensure_user_layout_columns()
    ensure_user_personalization_columns()
    ensure_user_alpaca_data_feed_column()

@app.errorhandler(CSRFError)
def handle_csrf_error(e):
    return f"CRITICAL CSRF FAILURE: {e.description}", 400


if __name__ == '__main__':
    start_engine()
    app.run(host=config.HOST, port=config.PORT, debug=config.DEBUG, use_reloader=False)
