from datetime import date, datetime
import os

from flask import Flask

from daily_report import generate_daily_report, run_daily_reports, send_daily_paper_report_email
from models import DailyReportEmailLog, Scan, Trade, User, db


def _app(tmp_path):
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = f"sqlite:///{tmp_path/'dr.db'}"
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)
    return app


def _user():
    return User(email='u@test.com', password_hash='x', full_name='Test User', subscription_status='pro', trading_mode='paper')


def test_report_generation_no_trades(tmp_path):
    app = _app(tmp_path)
    with app.app_context():
        db.create_all(); u = _user(); db.session.add(u); db.session.commit()
        r = generate_daily_report(u, date.today())
        assert r['trades_taken_count'] == 0
        assert 'No valid A+ setup' in r['playbook_improvement_tomorrow']


def test_report_with_wins_losses_rr(tmp_path):
    app = _app(tmp_path)
    with app.app_context():
        db.create_all(); u = _user(); db.session.add(u); db.session.commit()
        db.session.add_all([
            Trade(user_id=u.id,symbol='A',entry_price=10,stop_price=9,target_1=11,target_2=12,pnl=20,rr_ratio_2=2,status='closed'),
            Trade(user_id=u.id,symbol='B',entry_price=10,stop_price=9,target_1=11,target_2=12,pnl=-10,rr_ratio_1=1.2,status='closed'),
        ]); db.session.commit()
        r = generate_daily_report(u, date.today())
        assert r['wins_count'] == 1 and r['losses_count'] == 1
        assert r['average_risk_reward'] > 1


def test_max_drawdown(tmp_path):
    app = _app(tmp_path)
    with app.app_context():
        db.create_all(); u = _user(); db.session.add(u); db.session.commit()
        now = datetime.utcnow()
        db.session.add_all([
            Trade(user_id=u.id,symbol='A',entry_price=1,stop_price=1,target_1=1,target_2=1,pnl=50,created_at=now),
            Trade(user_id=u.id,symbol='B',entry_price=1,stop_price=1,target_1=1,target_2=1,pnl=-80,created_at=now),
        ]); db.session.commit()
        r = generate_daily_report(u, date.today())
        assert r['max_drawdown'] >= 30


def test_duplicate_prevention(tmp_path):
    app = _app(tmp_path)
    with app.app_context():
        db.create_all(); u = _user(); db.session.add(u); db.session.commit()
        out1 = run_daily_reports(date.today(), send=False, user_id=u.id, dry_run=True)
        out2 = run_daily_reports(date.today(), send=False, user_id=u.id, dry_run=True)
        assert out1['attempted'] == 1
        assert out2['skipped'] >= 1


def test_template_missing_skip(monkeypatch, tmp_path):
    import config
    monkeypatch.setattr(config, 'BREVO_DAILY_REPORT_TEMPLATE_ID', '')
    app = _app(tmp_path)
    with app.app_context():
        db.create_all(); u = _user(); db.session.add(u); db.session.commit()
        r = send_daily_paper_report_email(u, {'report_date': date.today().isoformat()})
        assert r['status'] == 'skipped'


def test_dry_run_and_test_recipient(monkeypatch, tmp_path):
    import config
    monkeypatch.setattr(config, 'BREVO_DAILY_REPORT_TEMPLATE_ID', '12')
    monkeypatch.setattr(config, 'DAILY_REPORT_DRY_RUN', True)
    monkeypatch.setattr(config, 'DAILY_REPORT_TEST_RECIPIENT', 'qa@test.com')
    app = _app(tmp_path)
    with app.app_context():
        db.create_all(); u = _user(); db.session.add(u); db.session.commit()
        r = send_daily_paper_report_email(u, {'report_date': date.today().isoformat()})
        assert r['status'] == 'dry_run'
