import os
import json
from pathlib import Path
import sys

for k in ["SECRET_KEY","TOKEN_ENCRYPTION_KEY","ALPACA_CLIENT_ID","ALPACA_CLIENT_SECRET","ALPACA_REDIRECT_URI","FINNHUB_API_KEY","GEMINI_API_KEY"]:
    os.environ.setdefault(k, "test")

sys.path.append(str(Path(__file__).resolve().parents[1]))
import app as app_module
from models import db, User, StripeEvent
from sqlalchemy import inspect
import config_check

def _disable_rate_limits():
    app_module.limiter._check_request_limit = lambda *args, **kwargs: None


def _event(event_id, event_type, obj):
    return {"id": event_id, "type": event_type, "data": {"object": obj}}


def test_pricing_and_upgrade_routes_and_sitemap():
    with app_module.app.test_request_context('/pricing'):
        pricing_html = app_module.pricing()
        assert 'Pricing' in pricing_html or 'PRO' in pricing_html
    with app_module.app.test_request_context('/sitemap.xml'):
        sitemap = app_module.sitemap_xml().get_data(as_text=True)
    assert '/pricing' in sitemap
    assert '/upgrade' not in sitemap
    # route resolves and does not raise BuildError
    with app_module.app.test_request_context('/'):
        assert app_module.url_for('upgrade') == '/upgrade'


def test_ensure_schema_migrations_creates_stripe_events_table(tmp_path):
    db_file = tmp_path / 't.db'
    app_module.app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_file}'
    with app_module.app.app_context():
        db.drop_all()
        db.create_all()
        db.session.execute(db.text('DROP TABLE stripe_events'))
        db.session.commit()
        app_module.ensure_schema_migrations()
        assert 'stripe_events' in inspect(db.engine).get_table_names()


def test_brevo_optional_and_required_list_id(monkeypatch):
    u = type('U', (), {'id': 1, 'email': 'a@b.com', 'full_name': 'A B', 'subscription_status': 'free'})
    monkeypatch.setattr(app_module.config, 'BREVO_API_KEY', 'k')
    monkeypatch.setattr(app_module.config, 'BREVO_SIGNUP_SYNC_OPTIONAL', True)
    monkeypatch.setattr(app_module.config, 'BREVO_SIGNUP_LIST_ID', 0)
    assert app_module.add_signup_user_to_brevo(u) is False
    monkeypatch.setattr(app_module.config, 'BREVO_SIGNUP_SYNC_OPTIONAL', False)
    assert app_module.add_signup_user_to_brevo(u) is False


def test_stripe_webhook_behaviors(monkeypatch):
    app_module.app.config['WTF_CSRF_ENABLED'] = False
    _disable_rate_limits()
    with app_module.app.app_context():
        user = User(email='webhook@test.com', password_hash='x', stripe_customer_id='cus_1', subscription_status='free')
        db.session.add(user)
        db.session.commit()
        uid = user.id

    monkeypatch.setattr(app_module.config, 'STRIPE_WEBHOOK_SECRET', 'whsec_test')
    monkeypatch.setattr(app_module, 'update_brevo_contact_attributes', lambda *a, **k: True)
    monkeypatch.setattr(app_module, 'track_user_event', lambda *a, **k: None)

    def construct(payload, sig, secret):
        return json.loads(payload.decode())

    monkeypatch.setattr(app_module.stripe.Webhook, 'construct_event', construct)

    def sub_retrieve(_):
        return {'id': 'sub_1', 'status': 'active', 'customer': 'cus_1', 'items': {'data': [{'price': {'id': 'price_m'}}]}}

    monkeypatch.setattr(app_module.stripe.Subscription, 'retrieve', sub_retrieve)

    evt = _event('evt_1', 'checkout.session.completed', {'customer': 'cus_1', 'subscription': 'sub_1', 'metadata': {'user_id': str(uid)}})
    with app_module.app.test_request_context('/api/stripe-webhook', method='POST', data=json.dumps(evt), headers={'Stripe-Signature': 'sig'}):
        r, code = app_module.stripe_webhook()
        assert code == 200
    with app_module.app.app_context():
        assert StripeEvent.query.filter_by(event_id='evt_1').count() == 1
        assert User.query.get(uid).subscription_status == 'pro'

    with app_module.app.test_request_context('/api/stripe-webhook', method='POST', data=json.dumps(evt), headers={'Stripe-Signature': 'sig'}):
        _, code = app_module.stripe_webhook()
        assert code == 200

    monkeypatch.setattr(app_module.stripe.Subscription, 'retrieve', lambda _id: (_ for _ in ()).throw(RuntimeError('boom')))
    evt_fail = _event('evt_fail', 'invoice.paid', {'customer': 'cus_1', 'subscription': 'sub_1'})
    with app_module.app.test_request_context('/api/stripe-webhook', method='POST', data=json.dumps(evt_fail), headers={'Stripe-Signature': 'sig'}):
        _, code = app_module.stripe_webhook()
        assert code == 500
    with app_module.app.app_context():
        assert StripeEvent.query.filter_by(event_id='evt_fail').count() == 0


def test_config_check_strict_failure_and_success(monkeypatch):
    monkeypatch.setenv('FLASK_ENV', 'development')
    errs = config_check.validate_required_production_config(strict=True)
    assert errs
    for key, value in {
        'SECRET_KEY': 'abc', 'TOKEN_ENCRYPTION_KEY': 'def', 'FLASK_DEBUG': '0', 'FLASK_ENV': 'production',
        'APP_BASE_URL': 'https://xeanvi.com', 'SESSION_COOKIE_SECURE': '1',
        'WTF_CSRF_TRUSTED_ORIGINS': 'https://xeanvi.com,https://www.xeanvi.com', 'REDIS_URL': 'redis://x', 'RATELIMIT_STORAGE_URI': 'redis://x',
        'STRIPE_PUBLIC_KEY': 'pk_live_real', 'STRIPE_SECRET_KEY': 'sk_live_real', 'STRIPE_WEBHOOK_SECRET': 'whsec_real',
        'STRIPE_PRICE_ID_MONTHLY': 'price_123', 'STRIPE_PRICE_ID_ANNUAL': 'price_456', 'BREVO_API_KEY': 'brevo',
        'BREVO_RESET_PASSWORD_TEMPLATE_ID': '12', 'BREVO_SENDER_EMAIL': 'support@xeanvi.com', 'BREVO_SIGNUP_LIST_ID': '5',
        'ALPACA_CLIENT_ID': 'id', 'ALPACA_CLIENT_SECRET': 'sec', 'ALPACA_REDIRECT_URI': 'https://alpaca/cb',
        'FINNHUB_API_KEY': 'f', 'GEMINI_API_KEY': 'g', 'DATABASE_URL': 'postgresql+psycopg://u:p@localhost:5432/db'
    }.items():
        monkeypatch.setenv(key, value)
    assert config_check.validate_required_production_config(strict=True) == []


def test_config_check_rejects_sqlite_database_url(monkeypatch):
    monkeypatch.setenv('FLASK_ENV', 'production')
    monkeypatch.setenv('FLASK_DEBUG', '0')
    monkeypatch.setenv('DATABASE_URL', 'sqlite:////tmp/test.db')
    errs = config_check.validate_required_production_config(strict=True)
    assert any('sqlite' in err.lower() for err in errs)


def test_config_check_strict_rejects_missing_database_url(monkeypatch):
    monkeypatch.setenv('FLASK_ENV', 'production')
    monkeypatch.setenv('FLASK_DEBUG', '0')
    monkeypatch.delenv('DATABASE_URL', raising=False)
    errs = config_check.validate_required_production_config(strict=True)
    assert any('DATABASE_URL is required' in err for err in errs)


def test_config_check_strict_rejects_sqlite_database_url_explicit(monkeypatch):
    monkeypatch.setenv('FLASK_ENV', 'production')
    monkeypatch.setenv('FLASK_DEBUG', '0')
    monkeypatch.setenv('DATABASE_URL', 'sqlite:////tmp/prod.db')
    errs = config_check.validate_required_production_config(strict=True)
    assert any('sqlite is not allowed' in err for err in errs)


def test_ensure_schema_migrations_uses_safe_boolean_defaults():
    source = Path(app_module.__file__).read_text()
    fn = source[source.index('def ensure_schema_migrations()'):source.index('ensure_db_initialized()')]
    assert 'BOOLEAN NOT NULL DEFAULT 0' not in fn
    assert 'BOOLEAN NOT NULL DEFAULT 1' not in fn


def test_watch_candidate_is_imported_and_migrated():
    source = Path(app_module.__file__).read_text()
    assert 'from models import BlogPost, BlogPublishingPlan, User, UserEvent, StripeEvent, Trade, DailyReportEmailLog, WatchCandidate' in source
    fn = source[source.index('def ensure_schema_migrations()'):source.index('ensure_db_initialized()')]
    assert 'WatchCandidate.__table__.create(bind=db.engine, checkfirst=True)' in fn

def test_run_scan_user_attribution_helper_sets_version():
    src = Path("scanner.py").read_text()
    assert "def apply_scan_attribution" in src
    assert "scan_attribution_version" in src
    assert "return _finalize_scan_result(result, user=user, source='run_scan')" in src


def test_settings_template_includes_reconnect_disclosure_hooks():
    src = Path("templates/settings.html").read_text()
    assert "scanner_health.alpaca_user_action_reconnect_required" in src
    assert "scanner_health.latest_alpaca_asset_metadata_reconnect_required" not in src
    assert "Reconnect required" in src
    assert "scanner_health.alpaca_metadata_fallback_notice" in src
    assert "partials/alpaca_authorization_disclosure.html" in src
