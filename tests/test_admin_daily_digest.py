import sys
import types

import pytest

import admin_daily_digest as digest
from app import app
from models import AdminDailyDigestEmailLog, User, UserEvent, db
from time_utils import utc_now_naive


@pytest.fixture
def app_context():
    with app.app_context():
        db.drop_all()
        db.create_all()
        yield
        db.session.remove()


def _configure_digest(monkeypatch):
    monkeypatch.setattr(digest.config, "ADMIN_DAILY_DIGEST_ENABLED", True)
    monkeypatch.setattr(digest.config, "ADMIN_DAILY_DIGEST_DRY_RUN", True)
    monkeypatch.setattr(digest.config, "ADMIN_DAILY_DIGEST_RECIPIENT", "admin@example.com")
    monkeypatch.setattr(digest.config, "BREVO_SENDER_EMAIL", "support@example.com")


def _mock_requests_post(monkeypatch, post_callable):
    fake_requests = types.SimpleNamespace(post=post_callable)
    monkeypatch.setitem(sys.modules, "requests", fake_requests)


def test_build_abandoned_logic(app_context):
    u1 = User(email="a@example.com", password_hash="x", subscription_status="free", created_at=utc_now_naive())
    u2 = User(email="b@example.com", password_hash="x", subscription_status="free", created_at=utc_now_naive())
    u3 = User(email="c@example.com", password_hash="x", subscription_status="pro", created_at=utc_now_naive())
    db.session.add_all([u1, u2, u3])
    db.session.commit()
    db.session.add(UserEvent(user_id=u1.id, event_name="checkout.started"))
    db.session.add(UserEvent(user_id=u2.id, event_name="checkout.started"))
    db.session.add(UserEvent(user_id=u2.id, event_name="checkout.completed"))
    db.session.add(UserEvent(user_id=u3.id, event_name="checkout.started"))
    db.session.commit()
    out = digest.build_admin_daily_digest()
    assert out["checkout_started_count"] == 3
    assert out["checkout_completed_count"] == 1
    assert out["checkout_abandoned_count"] == 1


def test_dry_run_twice_same_row_no_crash(app_context, monkeypatch):
    _configure_digest(monkeypatch)
    first = digest.send_admin_daily_digest(report_date="2026-05-08", dry_run=True)
    second = digest.send_admin_daily_digest(report_date="2026-05-08", dry_run=True)
    assert first["status"] == "dry_run"
    assert second["status"] == "dry_run"
    assert AdminDailyDigestEmailLog.query.count() == 1


def test_send_twice_second_skips_without_brevo_call(app_context, monkeypatch):
    _configure_digest(monkeypatch)
    monkeypatch.setattr(digest.config, "ADMIN_DAILY_DIGEST_DRY_RUN", False)
    calls = []

    class Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"messageId": "msg-1"}

    def fake_post(*args, **kwargs):
        calls.append((args, kwargs))
        return Resp()

    _mock_requests_post(monkeypatch, fake_post)
    first = digest.send_admin_daily_digest(report_date="2026-05-08", dry_run=False)
    second = digest.send_admin_daily_digest(report_date="2026-05-08", dry_run=False, force=False)
    assert first["status"] == "sent"
    assert second["status"] == "skipped"
    assert second["reason"] == "already sent"
    assert len(calls) == 1


def test_force_send_after_sent_updates_same_row(app_context, monkeypatch):
    _configure_digest(monkeypatch)
    monkeypatch.setattr(digest.config, "ADMIN_DAILY_DIGEST_DRY_RUN", False)
    message_ids = iter(["msg-1", "msg-2"])
    calls = []

    class Resp:
        def __init__(self, message_id):
            self.message_id = message_id

        def raise_for_status(self):
            return None

        def json(self):
            return {"messageId": self.message_id}

    def fake_post(*args, **kwargs):
        calls.append((args, kwargs))
        return Resp(next(message_ids))

    _mock_requests_post(monkeypatch, fake_post)
    digest.send_admin_daily_digest(report_date="2026-05-08", dry_run=False)
    forced = digest.send_admin_daily_digest(report_date="2026-05-08", dry_run=False, force=True)
    log = AdminDailyDigestEmailLog.query.one()
    assert forced["status"] == "sent"
    assert forced["brevo_message_id"] == "msg-2"
    assert len(calls) == 2
    assert AdminDailyDigestEmailLog.query.count() == 1
    assert log.brevo_message_id == "msg-2"


def test_dry_run_then_real_send_updates_same_row(app_context, monkeypatch):
    _configure_digest(monkeypatch)
    monkeypatch.setattr(digest.config, "ADMIN_DAILY_DIGEST_DRY_RUN", False)

    class Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"messageId": "msg-real"}

    _mock_requests_post(monkeypatch, lambda *a, **k: Resp())
    dry = digest.send_admin_daily_digest(report_date="2026-05-08", dry_run=True)
    sent = digest.send_admin_daily_digest(report_date="2026-05-08", dry_run=False, force=False)
    log = AdminDailyDigestEmailLog.query.one()
    assert dry["status"] == "dry_run"
    assert sent["status"] == "sent"
    assert AdminDailyDigestEmailLog.query.count() == 1
    assert log.status == "sent"


def test_failed_row_can_retry(app_context, monkeypatch):
    _configure_digest(monkeypatch)
    monkeypatch.setattr(digest.config, "ADMIN_DAILY_DIGEST_DRY_RUN", False)
    log = AdminDailyDigestEmailLog(report_date="2026-05-08", recipient_email="admin@example.com", status="failed", reason="prior")
    db.session.add(log)
    db.session.commit()

    class Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"messageId": "retry-ok"}

    _mock_requests_post(monkeypatch, lambda *a, **k: Resp())
    out = digest.send_admin_daily_digest(report_date="2026-05-08", dry_run=False)
    assert out["status"] == "sent"
    assert AdminDailyDigestEmailLog.query.count() == 1
    assert AdminDailyDigestEmailLog.query.one().brevo_message_id == "retry-ok"


def test_admin_only_recipient_payload(app_context, monkeypatch):
    _configure_digest(monkeypatch)
    monkeypatch.setattr(digest.config, "ADMIN_DAILY_DIGEST_DRY_RUN", False)
    captured = {}

    class Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"messageId": "admin-only"}

    def fake_post(*args, **kwargs):
        captured["payload"] = kwargs["json"]
        return Resp()

    _mock_requests_post(monkeypatch, fake_post)
    digest.send_admin_daily_digest(report_date="2026-05-08", dry_run=False)
    payload = captured["payload"]
    assert payload["to"] == [{"email": "admin@example.com"}]
    assert "cc" not in payload
    assert "bcc" not in payload


def test_dry_run_after_sent_does_not_downgrade(app_context, monkeypatch):
    _configure_digest(monkeypatch)
    monkeypatch.setattr(digest.config, "ADMIN_DAILY_DIGEST_DRY_RUN", False)

    class Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"messageId": "sent-msg-1"}

    _mock_requests_post(monkeypatch, lambda *a, **k: Resp())
    first = digest.send_admin_daily_digest(report_date="2026-05-08", dry_run=False)
    dry = digest.send_admin_daily_digest(report_date="2026-05-08", dry_run=True, force=False)
    log = AdminDailyDigestEmailLog.query.one()

    assert first["status"] == "sent"
    assert dry["status"] == "skipped"
    assert dry["reason"] == "already sent"
    assert dry["brevo_called"] is False
    assert AdminDailyDigestEmailLog.query.count() == 1
    assert log.status == "sent"
    assert log.brevo_message_id == "sent-msg-1"


def test_new_brevo_failure_persists_failed_log(app_context, monkeypatch):
    _configure_digest(monkeypatch)
    monkeypatch.setattr(digest.config, "ADMIN_DAILY_DIGEST_DRY_RUN", False)

    class Resp:
        def raise_for_status(self):
            raise RuntimeError("brevo failed")

        def json(self):
            return {}

    _mock_requests_post(monkeypatch, lambda *a, **k: Resp())
    result = digest.send_admin_daily_digest(report_date="2026-05-08", dry_run=False)
    log = AdminDailyDigestEmailLog.query.one()

    assert result["status"] == "failed"
    assert AdminDailyDigestEmailLog.query.count() == 1
    assert log.status == "failed"
    assert log.reason
    assert log.raw_json
    assert (log.brevo_message_id or "") == ""


def test_existing_sent_real_send_still_skips(app_context, monkeypatch):
    _configure_digest(monkeypatch)
    monkeypatch.setattr(digest.config, "ADMIN_DAILY_DIGEST_DRY_RUN", False)
    calls = []

    log = AdminDailyDigestEmailLog(report_date="2026-05-08", recipient_email="admin@example.com", status="sent", brevo_message_id="keep-me")
    db.session.add(log)
    db.session.commit()

    def fake_post(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("Brevo should not be called")

    _mock_requests_post(monkeypatch, fake_post)
    out = digest.send_admin_daily_digest(report_date="2026-05-08", dry_run=False, force=False)
    refreshed = AdminDailyDigestEmailLog.query.one()

    assert out["status"] == "skipped"
    assert out["reason"] == "already sent"
    assert out["brevo_called"] is False
    assert len(calls) == 0
    assert refreshed.status == "sent"
    assert refreshed.brevo_message_id == "keep-me"


def test_force_after_sent_failure_updates_existing_row(app_context, monkeypatch):
    _configure_digest(monkeypatch)
    monkeypatch.setattr(digest.config, "ADMIN_DAILY_DIGEST_DRY_RUN", False)

    log = AdminDailyDigestEmailLog(report_date="2026-05-08", recipient_email="admin@example.com", status="sent", brevo_message_id="msg-ok")
    db.session.add(log)
    db.session.commit()

    class Resp:
        def raise_for_status(self):
            raise RuntimeError("forced resend failed")

        def json(self):
            return {}

    _mock_requests_post(monkeypatch, lambda *a, **k: Resp())
    out = digest.send_admin_daily_digest(report_date="2026-05-08", dry_run=False, force=True)
    refreshed = AdminDailyDigestEmailLog.query.one()

    assert out["status"] == "failed"
    assert AdminDailyDigestEmailLog.query.count() == 1
    assert refreshed.status == "failed"
    assert refreshed.reason


def test_admin_digest_module_avoids_datetime_utcnow_usage():
    import inspect

    src = inspect.getsource(digest)
    assert "datetime.utcnow(" not in src
