import os
from pathlib import Path
import sys

for k in ["SECRET_KEY","TOKEN_ENCRYPTION_KEY","ALPACA_CLIENT_ID","ALPACA_CLIENT_SECRET","ALPACA_REDIRECT_URI","FINNHUB_API_KEY","GEMINI_API_KEY"]:
    os.environ.setdefault(k, "test")

sys.path.append(str(Path(__file__).resolve().parents[1]))
import app as app_module
from models import db, User


def _mk_user(email, **kwargs):
    u = User(email=email, password_hash='hash', **kwargs)
    db.session.add(u)
    db.session.commit()
    return u


def test_admin_user_recovery_authz_and_search(monkeypatch, tmp_path):
    db_file = tmp_path / 'admin_recovery.db'
    app_module.app.config.update(SQLALCHEMY_DATABASE_URI=f'sqlite:///{db_file}', TESTING=True, WTF_CSRF_ENABLED=False)
    monkeypatch.setattr(app_module, 'ADMIN_EMAIL', 'admin@test.com')
    with app_module.app.app_context():
        db.drop_all(); db.create_all()
        admin = _mk_user('admin@test.com')
        user = _mk_user('user@test.com', full_name='User', stripe_customer_id='cus_abc', stripe_subscription_id='sub_secret', _alpaca_paper_access_token='enc')
        uid = user.id
        admin_id = admin.id

    c = app_module.app.test_client()
    r = c.get('/admin/user-recovery')
    assert r.status_code in (301, 302)

    with c.session_transaction() as sess:
        sess['_user_id'] = str(uid)
    assert c.get('/admin/user-recovery').status_code == 403

    with c.session_transaction() as sess:
        sess['_user_id'] = str(admin_id)
    page = c.get('/admin/user-recovery?q=user@test.com').get_data(as_text=True)
    assert 'user@test.com' in page
    assert 'password_hash' not in page
    assert 'enc' not in page


def test_admin_user_recovery_actions(monkeypatch, tmp_path):
    db_file = tmp_path / 'admin_recovery_actions.db'
    app_module.app.config.update(SQLALCHEMY_DATABASE_URI=f'sqlite:///{db_file}', TESTING=True, WTF_CSRF_ENABLED=False)
    monkeypatch.setattr(app_module, 'ADMIN_EMAIL', 'admin@test.com')
    with app_module.app.app_context():
        db.drop_all(); db.create_all()
        admin = _mk_user('admin@test.com')
        user = _mk_user('member@test.com', subscription_status='pro', stripe_customer_id='cus_x', _alpaca_paper_access_token='enc', onboarding_completed=True, paper_bankroll_set=True, first_scan_completed=True, scan_preview_completed=True, playbook_reviewed=True, transparency_reviewed=True, broker_connection_started=True)
        uid = user.id
        admin_id = admin.id

    sent = {'called': False}
    monkeypatch.setattr(app_module, 'build_password_reset_url', lambda u: 'https://example/reset')
    monkeypatch.setattr(app_module, 'send_password_reset_email', lambda u, url: sent.update(called=True) or True)

    c = app_module.app.test_client()
    with c.session_transaction() as sess:
        sess['_user_id'] = str(admin_id)

    detail = c.get(f'/admin/user-recovery/{uid}').get_data(as_text=True)
    assert 'password_hash' not in detail
    assert 'enc' not in detail

    c.post(f'/admin/user-recovery/{uid}/send-reset')
    assert sent['called']

    c.post(f'/admin/user-recovery/{uid}/clear-onboarding')
    with app_module.app.app_context():
        u = User.query.get(uid)
        assert not u.onboarding_completed and not u.broker_connection_started
        assert u.subscription_status == 'pro'
        assert u._alpaca_paper_access_token == 'enc'

    c.post(f'/admin/user-recovery/{uid}/mark-onboarding-complete')
    with app_module.app.app_context():
        u = User.query.get(uid)
        assert u.onboarding_completed and u.paper_bankroll_set and u.first_scan_completed
        assert u.broker_connection_started is True

    c.post(f'/admin/user-recovery/{uid}/set-subscription-status', data={'subscription_status': 'past_due'})
    with app_module.app.app_context():
        assert User.query.get(uid).subscription_status == 'past_due'

    c.post(f'/admin/user-recovery/{uid}/set-subscription-status', data={'subscription_status': 'bad'})
    with app_module.app.app_context():
        assert User.query.get(uid).subscription_status == 'past_due'
