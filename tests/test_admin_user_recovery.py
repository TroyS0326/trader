import os
from pathlib import Path
import sys

os.environ["FLASK_ENV"] = "testing"
os.environ["TESTING"] = "1"
os.environ["DATABASE_URL"] = "sqlite:////tmp/trader_admin_recovery_tests.sqlite3"

for k in ["SECRET_KEY","TOKEN_ENCRYPTION_KEY","ALPACA_CLIENT_ID","ALPACA_CLIENT_SECRET","ALPACA_REDIRECT_URI","FINNHUB_API_KEY","GEMINI_API_KEY"]:
    os.environ.setdefault(k, "test")

sys.path.append(str(Path(__file__).resolve().parents[1]))
import app as app_module
import config as config_module
from models import db, User, UserEvent


def assert_test_database_is_sqlite() -> None:
    assert config_module.IS_TESTING is True
    driver = db.engine.url.drivername
    engine_url = str(db.engine.url).lower()
    assert driver.startswith("sqlite"), f"Refusing destructive setup on non-sqlite driver: {driver}"
    assert "postgres" not in engine_url, f"Refusing destructive setup on postgres URL: {engine_url}"


def _mk_user(email, **kwargs):
    u = User(email=email, password_hash='hash', **kwargs)
    db.session.add(u)
    db.session.commit()
    return u


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user_id)


def test_admin_user_recovery_authz_search_and_masking(monkeypatch):
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    monkeypatch.setattr(app_module, 'ADMIN_EMAIL', 'admin@test.com')
    with app_module.app.app_context():
        assert_test_database_is_sqlite()
        db.drop_all(); db.create_all()
        admin = _mk_user('admin@test.com')
        user = _mk_user('user@test.com', full_name='User Name', stripe_customer_id='cus_abc123456', stripe_subscription_id='sub_secret123456', _alpaca_paper_access_token='enc_value')
        uid = user.id

    c = app_module.app.test_client()
    assert c.get('/admin/user-recovery').status_code in (301, 302)

    _login(c, uid)
    assert c.get('/admin/user-recovery').status_code == 403

    _login(c, admin.id)
    page = c.get('/admin/user-recovery').get_data(as_text=True)
    assert 'Max 25 results' in page

    email_page = c.get('/admin/user-recovery?q=user@test.com').get_data(as_text=True)
    assert 'user@test.com' in email_page

    id_page = c.get(f'/admin/user-recovery?q={uid}').get_data(as_text=True)
    assert 'user@test.com' in id_page

    stripe_page = c.get('/admin/user-recovery?q=cus_abc123456').get_data(as_text=True)
    assert 'user@test.com' in stripe_page
    assert 'cus_abc123456' not in stripe_page
    assert 'sub_secret123456' not in stripe_page
    assert 'password_hash' not in stripe_page
    assert '_alpaca_paper_access_token' not in stripe_page
    assert 'enc_value' not in stripe_page


def test_admin_user_recovery_detail_actions_and_logging(monkeypatch):
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    monkeypatch.setattr(app_module, 'ADMIN_EMAIL', 'admin@test.com')
    with app_module.app.app_context():
        assert_test_database_is_sqlite()
        db.drop_all(); db.create_all()
        admin = _mk_user('admin@test.com')
        user = _mk_user(
            'member@test.com', full_name='Member', subscription_status='pro', subscription_plan='monthly',
            stripe_customer_id='cus_x12345', stripe_subscription_id='sub_x12345',
            trading_mode='paper', _alpaca_paper_access_token='enc_secret',
            onboarding_completed=True, paper_bankroll_set=True, first_scan_completed=True,
            scan_preview_completed=True, playbook_reviewed=True, transparency_reviewed=True,
            broker_connection_started=True,
        )
        uid = user.id

    c = app_module.app.test_client()
    _login(c, admin.id)

    detail = c.get(f'/admin/user-recovery/{uid}').get_data(as_text=True)
    assert 'member@test.com' in detail
    assert 'raw_json' not in detail
    assert 'password_hash' not in detail
    assert '_alpaca_paper_access_token' not in detail
    assert 'enc_secret' not in detail
    assert 'sub_x12345' not in detail
    assert 'cus_x12345' not in detail

    sent = {'called': False, 'reset_url_seen': None}
    monkeypatch.setattr(app_module, 'build_password_reset_url', lambda u: 'https://example/reset/token-value')
    monkeypatch.setattr(app_module, 'send_password_reset_email', lambda u, url: sent.update(called=True, reset_url_seen=url) or True)
    c.post(f'/admin/user-recovery/{uid}/send-reset')
    assert sent['called'] is True
    assert 'token-value' in sent['reset_url_seen']
    reset_page = c.get(f'/admin/user-recovery/{uid}').get_data(as_text=True)
    assert 'token-value' not in reset_page

    with app_module.app.app_context():
        before = User.query.get(uid)
        original_subscription_status = before.subscription_status
        original_subscription_plan = before.subscription_plan
        original_customer = before.stripe_customer_id
        original_sub = before.stripe_subscription_id
        original_mode = before.trading_mode
        original_token = before._alpaca_paper_access_token

    c.post(f'/admin/user-recovery/{uid}/clear-onboarding')
    with app_module.app.app_context():
        u = User.query.get(uid)
        assert not u.onboarding_completed and not u.paper_bankroll_set and not u.first_scan_completed
        assert not u.scan_preview_completed and not u.playbook_reviewed and not u.transparency_reviewed
        assert not u.broker_connection_started
        assert u.subscription_status == original_subscription_status
        assert u.subscription_plan == original_subscription_plan
        assert u.stripe_customer_id == original_customer
        assert u.stripe_subscription_id == original_sub
        assert u.trading_mode == original_mode
        assert u._alpaca_paper_access_token == original_token

    c.post(f'/admin/user-recovery/{uid}/mark-onboarding-complete')
    with app_module.app.app_context():
        u = User.query.get(uid)
        assert u.onboarding_completed and u.paper_bankroll_set and u.first_scan_completed
        assert u.scan_preview_completed and u.playbook_reviewed and u.transparency_reviewed
        assert u.broker_connection_started is True

    with app_module.app.app_context():
        u = User.query.get(uid)
        u.alpaca_paper_account_id = None
        u.alpaca_live_account_id = None
        u._alpaca_paper_access_token = None
        u._alpaca_live_access_token = None
        u.broker_connection_started = True
        db.session.commit()

    c.post(f'/admin/user-recovery/{uid}/mark-onboarding-complete')
    with app_module.app.app_context():
        assert User.query.get(uid).broker_connection_started is False

    for status in ('free', 'pro', 'canceled', 'past_due'):
        c.post(f'/admin/user-recovery/{uid}/set-subscription-status', data={'subscription_status': status})
        with app_module.app.app_context():
            assert User.query.get(uid).subscription_status == status

    c.post(f'/admin/user-recovery/{uid}/set-subscription-status', data={'subscription_status': 'bad'})
    with app_module.app.app_context():
        assert User.query.get(uid).subscription_status == 'past_due'

    with app_module.app.app_context():
        audit_events = UserEvent.query.filter(UserEvent.event_name.like('admin_user_recovery.%')).all()
        assert len(audit_events) >= 4
        serialized = ' '.join((e.event_context or '') for e in audit_events)
        assert 'reset_url' not in serialized
        assert 'token-value' not in serialized
        assert 'enc_secret' not in serialized
