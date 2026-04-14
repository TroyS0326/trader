import json
import logging
import os
import requests
import secrets
import sqlite3

from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, render_template, request, redirect, session, url_for, flash
from flask_login import login_user, logout_user, current_user, login_required
from flask_login import LoginManager
from flask_sock import Sock
from flask_talisman import Talisman
from flask_wtf.csrf import CSRFProtect
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
from models import Post, User
from onboarding import verify_alpaca_data_feed
from scanner import ScanError, buy_window_open, get_stock_chart_pack, now_et, run_scan
from watchlist import watchlist_manager

app = Flask(__name__)

# 1. Enable Global CSRF Protection
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
    # Start with CSP disabled until configured for external scripts (TradingView, etc.)
    Talisman(app, content_security_policy=None)

# Force cookies to only travel over HTTPS
app.config['SESSION_COOKIE_SECURE'] = True
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

app.config['SECRET_KEY'] = config.SECRET_KEY
# Force SQLAlchemy to use the exact same database file as your raw SQLite connections
app.config['SQLALCHEMY_DATABASE_URI'] = f"sqlite:///{os.path.abspath(config.DB_PATH)}"
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['ALPACA_CLIENT_ID'] = config.ALPACA_CLIENT_ID
app.config['ALPACA_CLIENT_SECRET'] = config.ALPACA_CLIENT_SECRET
app.config['ALPACA_REDIRECT_URI'] = os.getenv('ALPACA_REDIRECT_URI', 'https://broker-api.sandbox.alpaca.markets/v1/oauth/callback')
db.init_app(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
sock = Sock(app)
logger = logging.getLogger(__name__)

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


ensure_db_initialized()

LATEST_SCAN = None
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


def ensure_user_gamification_columns() -> None:
    """Backfill schema for discipline XP and streak metrics."""
    conn = sqlite3.connect(config.DB_PATH)
    try:
        cursor = conn.execute("PRAGMA table_info(user)")
        existing_columns = {row[1] for row in cursor.fetchall()}
        additions = {
            'discipline_xp': "ALTER TABLE user ADD COLUMN discipline_xp INTEGER NOT NULL DEFAULT 0",
            'current_streak': "ALTER TABLE user ADD COLUMN current_streak INTEGER NOT NULL DEFAULT 0",
            'highest_streak': "ALTER TABLE user ADD COLUMN highest_streak INTEGER NOT NULL DEFAULT 0",
        }
        for column_name, ddl in additions.items():
            if column_name not in existing_columns:
                conn.execute(ddl)
        conn.commit()
    finally:
        conn.close()


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
    # 1. If they aren't logged in, show the new SEO marketing page
    if not current_user.is_authenticated:
        return render_template('landing.html')

    # 2. If they ARE logged in, the front door is ALWAYS the Dashboard
    return redirect(url_for('dashboard'))


@app.route('/signup', methods=['GET', 'POST'])
@limiter.limit("3 per hour")  # 🛑 Blocks botnet mass-account creation
def signup():
    if request.method == 'POST':
        # (Keep your existing code here that grabs the email/password and saves to DB)
        tos_accepted = request.form.get('tos_agreement')

        if not tos_accepted:
            flash('You must agree to the technical execution terms to continue.', 'error')
            return redirect(url_for('signup'))

        email = request.form.get('email')
        password = request.form.get('password')
        full_name = request.form.get('full_name')
        phone = request.form.get('phone')

        existing_user = User.query.filter_by(email=email).first()
        if existing_user:
            flash('An account with that email already exists.', 'error')
            return redirect(url_for('signup'))

        # Save user in our DB immediately
        new_user = User(
            email=email,
            password_hash=generate_password_hash(password, method='pbkdf2:sha256'),
            full_name=full_name,
            address=request.form.get('address'),
            city=request.form.get('city'),
            state=request.form.get('state'),
            zip_code=request.form.get('zip_code'),
            phone=phone,
            subscription_status='free',
        )

        db.session.add(new_user)
        db.session.commit()

        # Log the user in immediately after creating the account
        login_user(new_user)
        # THE FIX: Send them straight to the broker uplink page
        return redirect(url_for('onboarding'))

    return render_template('signup.html')


@app.route('/login', methods=['GET', 'POST'])
@limiter.limit("5 per minute")  # 🛑 Blocks brute-force password guessing
def login():
    if request.method == 'POST':
        try:
            email = request.form.get('email')
            password = request.form.get('password')

            print(f"--- ATTEMPTING LOGIN FOR: {email} ---")

            user = User.query.filter_by(email=email).first()

            if not user or not check_password_hash(user.password_hash, password):
                print("FAILED: Wrong password or user doesn't exist.")
                flash('Invalid email or password', 'error')
                return redirect(url_for('login'))

            print("SUCCESS: Logging user in...")
            login_user(user)
            return redirect(url_for('dashboard'))

        except Exception as e:
            print(f"CRITICAL BACKEND ERROR: {str(e)}")
            flash(f"System Error: {str(e)}", 'error')
            return redirect(url_for('login'))

    return render_template('login.html')






@app.route('/features')
def features():
    return render_template('features.html')

@app.route('/playbook')
def playbook():
    # Make it accessible to both logged-in users and public marketing traffic
    return render_template('playbook.html')

@app.route('/terms')
def terms():
    return render_template('terms.html')


@app.route('/privacy')
def privacy():
    return render_template('privacy.html')



@app.route('/learn')
def learn_hub():
    # This acts as the main index for your articles
    return render_template('learn.html')


@app.route('/learn/<article_slug>')
def article(article_slug):
    # Dynamically serve evergreen SEO content based on the URL
    try:
        # e.g., renders templates/articles/risk-management.html
        return render_template(f'articles/{article_slug}.html')
    except Exception:
        # Fallback if the article doesn't exist
        return redirect(url_for('learn_hub'))

@app.route('/transparency')
def transparency():
    # In a fully fleshed-out app, you might pass dynamic backtest stats here
    # from your analyze_performance.py script. For now, we render the hub.
    return render_template('transparency.html', current_user=current_user)

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))


@app.route('/dashboard')
@login_required
def dashboard():
    return render_template('dashboard.html', current_user=current_user)


@app.route('/upgrade')
@login_required
def upgrade():
    # If they are already PRO, don't let them buy it again!
    if current_user.subscription_status == 'pro':
        flash("You are already a PRO member. Your AI execution is unlocked.", "success")
        return redirect(url_for('dashboard'))

    return render_template('upgrade.html', current_user=current_user)


@app.route('/api/process_checkout', methods=['POST'])
@login_required
def process_checkout():
    # In a real app, this is where you would integrate Stripe Checkout.
    # For now, we simulate a successful payment and instantly upgrade the user.
    plan = request.form.get('plan', 'monthly')

    try:
        current_user.subscription_status = 'pro'
        db.session.commit()
        flash("Payment Successful! Welcome to XeanVI PRO.", "success")
    except Exception:
        db.session.rollback()
        flash("Payment failed. Please try again.", "error")

    return redirect(url_for('dashboard'))




@app.route('/onboarding', methods=['GET', 'POST'])
@login_required
def onboarding():
    if request.method == 'POST':
        current_user.bankroll = float(request.form.get('bankroll', 5000.0))
        current_user.trading_mode = 'paper'
        db.session.commit()
        flash('Setup complete! Welcome to Command Center.', 'success')
        return redirect(url_for('dashboard'))

    return render_template('onboarding.html', current_user=current_user)

@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    if request.method == 'POST':
        # Update their bankroll and risk settings
        current_user.bankroll = float(request.form.get('bankroll', 0.0))
        refresh_interval = int(request.form.get('refresh_interval', 30000))
        current_user.refresh_interval = (
            refresh_interval if refresh_interval in VALID_REFRESH_INTERVALS else 30000
        )
        current_user.show_news = 'show_news' in request.form
        current_user.show_watchlist = 'show_watchlist' in request.form
        current_user.show_terminal = 'show_terminal' in request.form
        # Note: If you want to save the Paper/Live toggle, you will need to add a
        # 'trading_mode' column to your User model in models.py later!

        db.session.commit()
        flash('Settings saved successfully.', 'success')
        return redirect(url_for('dashboard'))

    return render_template('settings.html', current_user=current_user)


@app.route('/syndicate')
@login_required
def syndicate():
    return render_template('leaderboard.html')

@app.route('/community')
@login_required
def community():
    posts = Post.query.order_by(Post.created_at.desc()).limit(50).all()
    return render_template('community.html', posts=posts, current_user=current_user)


@app.route('/api/post_idea', methods=['POST'])
@login_required
@limiter.limit("10 per hour")
def post_idea():
    ticker = request.form.get('ticker', '').strip().upper()
    setup_grade = request.form.get('setup_grade', 'WATCH')
    content = request.form.get('content', '').strip()

    if not ticker or not content:
        flash('Ticker and trade notes are required.', 'error')
        return redirect(url_for('community'))

    new_post = Post(
        user_id=current_user.id,
        ticker=ticker,
        setup_grade=setup_grade,
        content=content,
    )

    try:
        db.session.add(new_post)
        db.session.commit()
        flash('Trade idea shared with the syndicate!', 'success')
    except Exception:
        db.session.rollback()
        flash('Failed to post idea. Please try again.', 'error')

    return redirect(url_for('community'))


@app.route('/scanner')
@login_required
def scanner():
    # This is where the morning scan actually lives now
    return render_template('index.html', app_title="XeanVI")



@app.route('/update_settings', methods=['POST'])
@login_required
def update_settings():
    current_user.bankroll = float(request.form.get('bankroll'))
    current_user.risk_pct = float(request.form.get('risk_pct'))
    refresh_interval = int(request.form.get('refresh_interval', 30000))
    current_user.refresh_interval = (
        refresh_interval if refresh_interval in VALID_REFRESH_INTERVALS else 30000
    )
    db.session.commit()
    flash('Risk parameters updated successfully.', 'success')
    return redirect(url_for('dashboard'))


@app.route('/connect_broker', methods=['POST'])
@login_required
def connect_broker():
    # Grab the keys AND the radio button selection from the form
    api_key = request.form.get('api_key')
    api_secret = request.form.get('api_secret')
    claimed_feed = request.form.get('claimed_feed')  # Will be 'iex' or 'sip'

    # Run the "Trust, But Verify" script
    result = verify_alpaca_data_feed(current_user.id, api_key, api_secret, claimed_feed)

    if result['success']:
        flash(result['message'], 'success')
    else:
        flash(result['message'], 'error')

    return redirect(url_for('dashboard'))


@app.route('/alpaca/login')
@login_required
def alpaca_login():
    oauth_state = secrets.token_urlsafe(32)
    session['oauth_state'] = oauth_state

    alpaca_auth_url = (
        f"https://app.alpaca.markets/oauth/authorize"
        f"?response_type=code"
        f"&client_id={app.config['ALPACA_CLIENT_ID']}"
        f"&redirect_uri={app.config['ALPACA_REDIRECT_URI']}"
        f"&scope=trading"
        f"&state={oauth_state}"
        f"&env=paper"
    )
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
        return redirect(url_for('dashboard'))

    token_url = "https://broker-api.sandbox.alpaca.markets/v1/oauth/token"
    payload = {
        'grant_type': 'authorization_code',
        'code': code,
        'client_id': app.config['ALPACA_CLIENT_ID'],
        'client_secret': app.config['ALPACA_CLIENT_SECRET'],
        'redirect_uri': app.config['ALPACA_REDIRECT_URI'],
    }

    try:
        response = requests.post(token_url, data=payload, timeout=15)
        response.raise_for_status()
        data = response.json()

        if 'access_token' in data:
            current_user.alpaca_access_token = data['access_token']
            current_user.alpaca_account_id = data.get('account_id')
            db.session.commit()
            flash("Broker connected successfully via secure OAuth!", "success")
        else:
            flash(f"OAuth Error: {data.get('error_description', 'Unknown error')}", "error")
    except Exception as e:
        flash(f"Connection error: {str(e)}", "error")

    if current_user.bankroll == 0.0:
        return redirect(url_for('onboarding'))
    return redirect(url_for('settings'))


@app.route('/v1/oauth/callback')
def sandbox_callback():
    return alpaca_callback()


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


@app.route('/api/run-scan', methods=['POST'])
@login_required
def run_scan_api():
    try:
        # 1. Run your ACTUAL Python scanner from scanner.py
        result = run_scan()

        # 2. Extract the winning stock from the scanner's results
        best_pick = result.get('best_pick', {})
        real_target_ticker = best_pick.get('symbol', 'SPY')
        score = best_pick.get('score_total', 'N/A')

        # 3. Build live logs based on real data to send back to the dashboard
        real_logs = [
            {'msg': 'Running full market analysis via scanner.py...', 'color': 'var(--text-muted)'},
            {'msg': f'AI Engine found top setup. Score: {score}/100', 'color': 'var(--success)'},
            {'msg': f'Real Target Acquired: {real_target_ticker}', 'color': 'var(--accent-blue)'}
        ]

        # Save the scan to your database history (optional but recommended)
        from db import insert_scan
        insert_scan(result)

        return jsonify({
            'status': 'success',
            'target_ticker': real_target_ticker,
            'logs': real_logs
        })

    except Exception as e:
        # If your bot crashes (e.g. no stocks found), show it on the dashboard
        return jsonify({
            'status': 'error',
            'target_ticker': 'SPY',  # Default to SPY if it crashes so the chart doesn't break
            'logs': [{'msg': f'BOT ERROR: {str(e)}', 'color': 'var(--danger)'}]
        }), 500


@app.route('/api/scan', methods=['POST', 'GET'])
def api_scan():
    global LATEST_SCAN
    try:
        result = run_scan()
        risk_controls = {
            'failed_trades_today': get_failed_trades_today(),
            'max_failed_trades_per_day': config.MAX_FAILED_TRADES_PER_DAY,
            'can_trade_today': get_failed_trades_today() < config.MAX_FAILED_TRADES_PER_DAY,
            'buy_window_open': buy_window_open(),
            'no_buy_before_et': config.NO_BUY_BEFORE_ET,
        }
        result['risk_controls'] = risk_controls
        scan_id = insert_scan(result)
        result['scan_id'] = scan_id
        LATEST_SCAN = result
        watchlist_manager.set_items(result.get('watchlist', []))
        return ok(
            result,
            history={'scans': get_recent_scans(), 'trades': get_recent_trades()},
        )
    except ScanError as exc:
        return fail(str(exc))
    except Exception as exc:
        return fail(f'Scan failed: {exc}', 500)


@app.route('/api/history')
def api_history():
    return ok({'scans': get_recent_scans(), 'trades': get_recent_trades(), 'failed_trades_today': get_failed_trades_today()})


@app.route('/api/chart/<symbol>')
def api_chart(symbol: str):
    try:
        return ok(get_stock_chart_pack(symbol.upper()))
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
        trade = get_trade_by_order_id(order_id)
        if not trade:
            return fail('Trade not found for order id.', 404)
        raw = trade.get('raw_json') or '{}'
        if isinstance(raw, str):
            raw = json.loads(raw or '{}')
        bundle = raw.get('order_bundle') if isinstance(raw, dict) else None
        if not isinstance(bundle, dict):
            order = get_order(order_id)
        else:
            order = dict(bundle)
            if bundle.get('strategy') == 'target1_then_trailing_runner':
                bundle = maybe_activate_runner_trailing(bundle, breakeven_price=float(trade.get('entry_price') or 0))
                order['target_1_order'] = get_order(bundle.get('target_1_order_id')) if bundle.get('target_1_order_id') else {}
                if bundle.get('runner_trailing_order_id'):
                    order['runner_trailing_order'] = get_order(bundle.get('runner_trailing_order_id'))
                elif bundle.get('runner_stop_order_id'):
                    order['runner_order'] = get_order(bundle.get('runner_stop_order_id'))
                raw['order_bundle'] = bundle
        updates = {
            'order_status': order.get('status'),
            'filled_avg_price': order.get('filled_avg_price'),
            'filled_qty': order.get('filled_qty'),
            'outcome': order_outcome_from_payload(order),
            'raw_json': raw if isinstance(raw, dict) else order,
        }
        update_trade_status(order_id, updates)

        # --- GAMIFICATION LOGIC ---
        if updates['outcome'] in ['win', 'partial_win']:
            # Reward points for a successful AI execution
            current_user.discipline_xp += 50
            db.session.commit()
        elif updates['outcome'] == 'loss':
            # Reward points just for letting the system hit the stop-loss instead of holding bags!
            current_user.discipline_xp += 25
            db.session.commit()
        # --------------------------

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
    db.create_all()
    ensure_user_refresh_interval_column()
    ensure_user_layout_columns()
    ensure_user_gamification_columns()


if __name__ == '__main__':
    start_engine()
    app.run(host=config.HOST, port=config.PORT, debug=config.DEBUG, use_reloader=False)
