from datetime import date

import pytest

import config
from daily_report import generate_daily_report, run_daily_reports, send_daily_paper_report_email
from models import DailyReportEmailLog, Scan, Trade, User, UserEvent, db


def _app(tmp_path):
    from flask import Flask
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = f"sqlite:///{tmp_path/'dr.db'}"
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)
    return app


def _user(subscription_status='pro', trading_mode='paper', email='u@test.com'):
    return User(email=email, password_hash='x', full_name='Test User', subscription_status=subscription_status, trading_mode=trading_mode)


class DummyResp:
    def __init__(self, status_code=201, body=None, text=''):
        self.status_code = status_code
        self._body = body
        self.text = text
        self.content = b'1'

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body or {}


def test_dry_run_log_does_not_block_real_send(monkeypatch, tmp_path):
    monkeypatch.setattr(config, 'BREVO_DAILY_REPORT_TEMPLATE_ID', '12')
    monkeypatch.setattr(config, 'BREVO_API_KEY', 'k')
    monkeypatch.setattr(config, 'DAILY_REPORT_DRY_RUN', False)
    monkeypatch.setattr('daily_report.requests.post', lambda *a, **k: DummyResp(body={'messageId': 'm1'}))
    app = _app(tmp_path)
    with app.app_context():
        db.create_all(); u = _user(); db.session.add(u); db.session.commit()
        out1 = run_daily_reports(date.today(), send=False, user_id=u.id, dry_run=True)
        out2 = run_daily_reports(date.today(), send=True, user_id=u.id)
        assert out1['dry_run'] == 1
        assert out2['sent'] == 1


def test_skipped_missing_template_does_not_block_later_send(monkeypatch, tmp_path):
    app = _app(tmp_path)
    with app.app_context():
        db.create_all(); u = _user(); db.session.add(u); db.session.commit()
        monkeypatch.setattr(config, 'BREVO_DAILY_REPORT_TEMPLATE_ID', '')
        out1 = run_daily_reports(date.today(), send=True, user_id=u.id)
        monkeypatch.setattr(config, 'BREVO_DAILY_REPORT_TEMPLATE_ID', '12')
        monkeypatch.setattr(config, 'BREVO_API_KEY', 'k')
        monkeypatch.setattr('daily_report.requests.post', lambda *a, **k: DummyResp(body={'messageId': 'm2'}))
        out2 = run_daily_reports(date.today(), send=True, user_id=u.id)
        assert out1['skipped'] == 1
        assert out2['sent'] == 1


def test_failed_send_does_not_block_retry(monkeypatch, tmp_path):
    app = _app(tmp_path)
    with app.app_context():
        db.create_all(); u = _user(); db.session.add(u); db.session.commit()
        monkeypatch.setattr(config, 'BREVO_DAILY_REPORT_TEMPLATE_ID', '12')
        monkeypatch.setattr(config, 'BREVO_API_KEY', 'k')
        monkeypatch.setattr('daily_report.requests.post', lambda *a, **k: DummyResp(status_code=500, text='err'))
        out1 = run_daily_reports(date.today(), send=True, user_id=u.id)
        monkeypatch.setattr('daily_report.requests.post', lambda *a, **k: DummyResp(body={'messageId': 'm3'}))
        out2 = run_daily_reports(date.today(), send=True, user_id=u.id)
        assert out1['failed'] == 1
        assert out2['sent'] == 1


def test_sent_blocks_duplicate_unless_force(monkeypatch, tmp_path):
    app = _app(tmp_path)
    with app.app_context():
        db.create_all(); u = _user(); db.session.add(u); db.session.commit()
        monkeypatch.setattr(config, 'BREVO_DAILY_REPORT_TEMPLATE_ID', '12')
        monkeypatch.setattr(config, 'BREVO_API_KEY', 'k')
        monkeypatch.setattr('daily_report.requests.post', lambda *a, **k: DummyResp(body={'messageId': 'm4'}))
        out1 = run_daily_reports(date.today(), send=True, user_id=u.id)
        out2 = run_daily_reports(date.today(), send=True, user_id=u.id)
        out3 = run_daily_reports(date.today(), send=True, user_id=u.id, force=True)
        assert out1['sent'] == 1
        assert out2['skipped'] == 1
        assert out3['sent'] == 1


def test_scheduled_flags_and_require_activity(monkeypatch, tmp_path):
    app = _app(tmp_path)
    with app.app_context():
        db.create_all()
        pro_u = _user(subscription_status='pro', email='pro@test.com')
        free_u = _user(subscription_status='free', email='free@test.com')
        db.session.add_all([pro_u, free_u]); db.session.commit()
        monkeypatch.setattr(config, 'BREVO_DAILY_REPORT_TEMPLATE_ID', '12')
        monkeypatch.setattr(config, 'BREVO_API_KEY', 'k')
        monkeypatch.setattr(config, 'DAILY_REPORT_SEND_TO_PRO_USERS', False)
        monkeypatch.setattr(config, 'DAILY_REPORT_SEND_TO_FREE_USERS', True)
        monkeypatch.setattr(config, 'DAILY_REPORT_REQUIRE_ACTIVITY', True)
        monkeypatch.setattr('daily_report.requests.post', lambda *a, **k: DummyResp(body={'messageId': 'm5'}))
        out = run_daily_reports(date.today(), send=True)
        # free user has no activity so skipped due to requirement; pro not considered by config
        assert out['users_considered'] == 1
        assert out['skipped'] == 1
        assert out['reasons'].get('no_activity') == 1


def test_scan_privacy_filter(monkeypatch, tmp_path):
    app = _app(tmp_path)
    with app.app_context():
        db.create_all(); u = _user(); db.session.add(u); db.session.commit()
        db.session.add(Scan(payload_json='{"rejections": ["global-reason"]}', best_symbol='GLOB', best_score=99))
        db.session.commit()
        r = generate_daily_report(u, date.today())
        assert 'global-reason' not in r['trades_skipped_and_why']
        assert r['best_setup_of_day'] == 'No qualifying setup data was recorded today.'


def test_user_specific_scan_allowed(tmp_path):
    app = _app(tmp_path)
    with app.app_context():
        db.create_all(); u = _user(); db.session.add(u); db.session.commit()
        db.session.add(Scan(payload_json=f'{{"user_id": {u.id}, "rejections": ["my-reason"]}}', best_symbol='ME', best_score=88))
        db.session.commit()
        r = generate_daily_report(u, date.today())
        assert 'my-reason' in r['trades_skipped_and_why']
        assert 'ME' in r['best_setup_of_day']


def test_missing_brevo_api_key_returns_skipped(monkeypatch, tmp_path):
    monkeypatch.setattr(config, 'BREVO_DAILY_REPORT_TEMPLATE_ID', '12')
    monkeypatch.setattr(config, 'BREVO_API_KEY', '')
    monkeypatch.setattr(config, 'DAILY_REPORT_DRY_RUN', False)
    app = _app(tmp_path)
    with app.app_context():
        db.create_all(); u = _user(); db.session.add(u); db.session.commit()
        r = send_daily_paper_report_email(u, {'report_date': date.today().isoformat()})
        assert r['status'] == 'skipped'
        assert r['reason'] == 'missing_brevo_api_key'


def test_brevo_network_exception_is_failed_and_batch_safe(monkeypatch, tmp_path):
    app = _app(tmp_path)
    with app.app_context():
        db.create_all(); u1 = _user(email='a@test.com'); u2 = _user(email='b@test.com')
        db.session.add_all([u1, u2]); db.session.commit()
        monkeypatch.setattr(config, 'BREVO_DAILY_REPORT_TEMPLATE_ID', '12')
        monkeypatch.setattr(config, 'BREVO_API_KEY', 'k')
        calls = {'n': 0}
        def _post(*args, **kwargs):
            calls['n'] += 1
            if calls['n'] == 1:
                raise RuntimeError('net down')
            return DummyResp(body={'messageId': 'ok'})
        monkeypatch.setattr('daily_report.requests.post', _post)
        out = run_daily_reports(date.today(), send=True, send_all=True)
        assert out['failed'] == 1
        assert out['sent'] == 1
        assert DailyReportEmailLog.query.count() == 2


def test_report_user_id_scan_is_used(tmp_path):
    app = _app(tmp_path)
    with app.app_context():
        db.create_all(); u = _user(); db.session.add(u); db.session.commit()
        db.session.add(Scan(payload_json=f'{{"report_user_id": {u.id}, "rejections": ["report-id-reason"]}}', best_symbol='RID', best_score=77))
        db.session.commit()
        r = generate_daily_report(u, date.today())
        assert 'report-id-reason' in r['trades_skipped_and_why']
        assert 'RID' in r['best_setup_of_day']


def test_mismatched_report_user_id_scan_is_ignored(tmp_path):
    app = _app(tmp_path)
    with app.app_context():
        db.create_all(); u = _user(); db.session.add(u); db.session.commit()
        db.session.add(Scan(payload_json=f'{{"report_user_id": {u.id + 1}, "rejections": ["wrong-user"]}}', best_symbol='NOPE', best_score=91))
        db.session.commit()
        r = generate_daily_report(u, date.today())
        assert 'wrong-user' not in r['trades_skipped_and_why']
        assert 'NOPE' not in r['best_setup_of_day']


def test_force_resend_failure_does_not_downgrade_existing_sent_log(monkeypatch, tmp_path):
    app = _app(tmp_path)
    with app.app_context():
        db.create_all(); u = _user(); db.session.add(u); db.session.commit()
        monkeypatch.setattr(config, 'BREVO_DAILY_REPORT_TEMPLATE_ID', '12')
        monkeypatch.setattr(config, 'BREVO_API_KEY', 'k')
        monkeypatch.setattr('daily_report.requests.post', lambda *a, **k: DummyResp(body={'messageId': 'm6'}))
        out1 = run_daily_reports(date.today(), send=True, user_id=u.id)
        monkeypatch.setattr('daily_report.requests.post', lambda *a, **k: DummyResp(status_code=500, text='err'))
        out2 = run_daily_reports(date.today(), send=True, user_id=u.id, force=True)
        log = DailyReportEmailLog.query.filter_by(user_id=u.id, report_date=date.today().isoformat()).first()
        assert out1['sent'] == 1
        assert out2['failed'] == 1
        assert log.status == 'sent'
