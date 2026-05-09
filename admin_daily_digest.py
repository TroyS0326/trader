import argparse, json
from collections import Counter
from datetime import datetime, date, time, timedelta, timezone
from zoneinfo import ZoneInfo
import requests
import config
from app import app
from models import db, User, UserEvent, Trade, Scan, MarketRegime, WatchCandidate, AdminDailyDigestEmailLog


def get_report_window(report_date=None, timezone_name='America/New_York'):
    tz = ZoneInfo(timezone_name)
    d = datetime.strptime(report_date, '%Y-%m-%d').date() if isinstance(report_date, str) else (report_date or datetime.now(tz).date())
    start_et = datetime.combine(d, time.min, tzinfo=tz)
    end_et = start_et + timedelta(days=1)
    return {'report_date': d.isoformat(), 'start_et': start_et, 'end_et': end_et, 'start_utc': start_et.astimezone(timezone.utc).replace(tzinfo=None), 'end_utc': end_et.astimezone(timezone.utc).replace(tzinfo=None)}


def _q_between(model_col, w):
    return model_col >= w['start_utc'], model_col < w['end_utc']


def build_admin_daily_digest(report_date=None):
    w = get_report_window(report_date)
    signups = User.query.filter(*_q_between(User.created_at, w)).all()
    started = UserEvent.query.filter(UserEvent.event_name.in_(['checkout.started','checkout_started']), *_q_between(UserEvent.created_at, w)).all()
    completed = UserEvent.query.filter(UserEvent.event_name.in_(['checkout.completed','checkout_completed']), *_q_between(UserEvent.created_at, w)).all()
    expired = UserEvent.query.filter(UserEvent.event_name=='checkout.expired', *_q_between(UserEvent.created_at, w)).all()
    completed_ids={e.user_id for e in completed if e.user_id}
    abandoned=[e for e in started if e.user_id and e.user_id not in completed_ids and ((db.session.get(User,e.user_id).subscription_status or '').lower()!='pro')]
    trades=Trade.query.filter(*_q_between(Trade.created_at,w)).all()
    scans=Scan.query.filter(*_q_between(Scan.created_at,w)).all()
    watch=WatchCandidate.query.order_by(WatchCandidate.last_seen_at.desc()).limit(10).all()
    top_symbols=dict(Counter([t.symbol for t in trades]).most_common(5))
    warnings=[]
    if not config.BREVO_API_KEY: warnings.append('BREVO_API_KEY missing')
    return {'report_date': w['report_date'], 'new_signups_count': len(signups), 'checkout_started_count': len(started), 'checkout_completed_count': len(completed), 'checkout_expired_count': len(expired), 'checkout_abandoned_count': len(abandoned), 'new_signups':[{'email':u.email,'full_name':u.full_name or '', 'created_at':(u.created_at.isoformat() if u.created_at else ''),'subscription_status':u.subscription_status} for u in signups], 'abandoned_checkout_users':[{'user_id':e.user_id} for e in abandoned], 'scans_created_count':len(scans), 'trades_created_count':len(trades), 'top_symbols_by_trade_count':top_symbols, 'latest_market_regime_status': (MarketRegime.query.order_by(MarketRegime.updated_at.desc()).first().regime_status if MarketRegime.query.first() else 'unavailable'), 'top_watch_candidates':[{'symbol':w.symbol,'latest_decision':w.latest_decision,'latest_setup_grade':w.latest_setup_grade,'latest_score_total':w.latest_score_total,'source':w.source,'status':w.status} for w in watch], 'warnings':warnings, 'digest_generated_at':datetime.utcnow().isoformat(), 'summary_headline': f"XeanVI Daily Admin Digest — {w['report_date']}"}


def render_admin_digest_html(params):
    return f"<html><body><h2>{params['summary_headline']}</h2><p>Signups: {params['new_signups_count']} | Checkout started: {params['checkout_started_count']} | Completed: {params['checkout_completed_count']} | Abandoned: {params['checkout_abandoned_count']}</p></body></html>"


def send_admin_daily_digest(report_date=None, force=False, dry_run=None, recipient=None):
    dry_run = config.ADMIN_DAILY_DIGEST_DRY_RUN if dry_run is None else dry_run
    if not config.ADMIN_DAILY_DIGEST_ENABLED and not force:
        return {'status':'skipped','reason':'disabled'}
    params=build_admin_daily_digest(report_date)
    report_date = params['report_date']
    recipient = (recipient or config.ADMIN_DAILY_DIGEST_RECIPIENT or __import__('os').getenv('ADMIN_EMAIL','')).strip()
    if not recipient:
        db.session.add(AdminDailyDigestEmailLog(report_date=report_date, recipient_email='(missing)', status='skipped', reason='missing_recipient', raw_json=json.dumps(params))); db.session.commit(); return {'status':'skipped'}
    if not force and AdminDailyDigestEmailLog.query.filter_by(report_date=report_date, recipient_email=recipient).first():
        return {'status':'skipped','reason':'duplicate'}
    payload={'sender':{'name':config.BREVO_SENDER_NAME or 'XeanVI Admin','email':config.BREVO_SENDER_EMAIL},'to':[{'email':recipient}],'params':params}
    tid = config.ADMIN_DAILY_DIGEST_TEMPLATE_ID
    if tid and tid.isdigit(): payload['templateId']=int(tid)
    else: payload.update({'subject':f"XeanVI Daily Admin Digest — {report_date}",'htmlContent':render_admin_digest_html(params)})
    status='dry_run' if dry_run else 'sent'; msg_id=''
    if not dry_run:
        r=requests.post('https://api.brevo.com/v3/smtp/email',headers={'accept':'application/json','api-key':config.BREVO_API_KEY,'content-type':'application/json'},json=payload,timeout=20)
        r.raise_for_status(); msg_id=(r.json() or {}).get('messageId','')
    db.session.add(AdminDailyDigestEmailLog(report_date=report_date, recipient_email=recipient, status=status, brevo_message_id=msg_id, raw_json=json.dumps(payload))); db.session.commit()
    return {'status':status,'recipient':recipient}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--date')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--send', action='store_true')
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--recipient')
    args = parser.parse_args()
    with app.app_context():
        print(send_admin_daily_digest(report_date=args.date, dry_run=(args.dry_run or not args.send), force=args.force, recipient=args.recipient))
