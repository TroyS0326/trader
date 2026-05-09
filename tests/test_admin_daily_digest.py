import pytest
from app import app
from datetime import datetime

@pytest.fixture
def app_context():
    with app.app_context():
        db.drop_all(); db.create_all()
        yield
        db.session.remove()

import admin_daily_digest as digest
from models import db, User, UserEvent, AdminDailyDigestEmailLog


def seed_users():
    u1=User(email='a@example.com', password_hash='x', subscription_status='free', created_at=datetime.utcnow())
    u2=User(email='b@example.com', password_hash='x', subscription_status='free', created_at=datetime.utcnow())
    u3=User(email='c@example.com', password_hash='x', subscription_status='pro', created_at=datetime.utcnow())
    db.session.add_all([u1,u2,u3]); db.session.commit()
    return u1,u2,u3


def test_build_abandoned_logic(app_context):
    u1,u2,u3=seed_users()
    db.session.add(UserEvent(user_id=u1.id,event_name='checkout.started'))
    db.session.add(UserEvent(user_id=u2.id,event_name='checkout.started'))
    db.session.add(UserEvent(user_id=u2.id,event_name='checkout.completed'))
    db.session.add(UserEvent(user_id=u3.id,event_name='checkout.started'))
    db.session.commit()
    out=digest.build_admin_daily_digest()
    assert out['checkout_started_count']==3
    assert out['checkout_completed_count']==1
    assert out['checkout_abandoned_count']==1


def test_send_dry_run_and_duplicate(app_context, monkeypatch):
    monkeypatch.setattr(digest.config,'ADMIN_DAILY_DIGEST_ENABLED',True)
    monkeypatch.setattr(digest.config,'ADMIN_DAILY_DIGEST_DRY_RUN',True)
    monkeypatch.setattr(digest.config,'ADMIN_DAILY_DIGEST_RECIPIENT','admin@example.com')
    monkeypatch.setattr(digest.config,'BREVO_SENDER_EMAIL','support@example.com')
    res=digest.send_admin_daily_digest(report_date='2026-05-08')
    assert res['status']=='dry_run'
    assert AdminDailyDigestEmailLog.query.count()==1
    res2=digest.send_admin_daily_digest(report_date='2026-05-08')
    assert res2['status']=='skipped'


def test_missing_recipient_skips(app_context, monkeypatch):
    monkeypatch.setattr(digest.config,'ADMIN_DAILY_DIGEST_ENABLED',True)
    monkeypatch.setattr(digest.config,'ADMIN_DAILY_DIGEST_RECIPIENT','')
    import os; os.environ['ADMIN_EMAIL']=''
    res=digest.send_admin_daily_digest(report_date='2026-05-07', dry_run=True)
    assert res['status']=='skipped'
