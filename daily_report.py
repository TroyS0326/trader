import argparse
import json
import logging
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from sqlalchemy import or_

import config
from app import app
from models import DailyReportEmailLog, Scan, Trade, User, UserEvent, db

logger = logging.getLogger(__name__)
NY_TZ = ZoneInfo(config.TIMEZONE_LABEL)


def _market_date_bounds(report_date: date):
    start_local = datetime.combine(report_date, time.min, tzinfo=NY_TZ)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc).replace(tzinfo=None), end_local.astimezone(timezone.utc).replace(tzinfo=None)


def _parse_json(value):
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value)
    except Exception:
        return {}


def _safe_float(v):
    try:
        return float(v)
    except Exception:
        return None


def generate_daily_report(user: User, report_date: date):
    start_utc, end_utc = _market_date_bounds(report_date)
    trades = Trade.query.filter(Trade.user_id == user.id, Trade.created_at >= start_utc, Trade.created_at < end_utc).order_by(Trade.created_at.asc()).all()
    scans = Scan.query.filter(Scan.created_at >= start_utc, Scan.created_at < end_utc).all()
    wins = losses = breakeven = open_pending = 0
    rr_values = []
    realized = []
    skip_reasons = Counter()
    for t in trades:
        pnl = _safe_float(getattr(t, 'pnl', None))
        state = f"{(t.outcome or '')} {(t.status or '')} {(t.order_status or '')}".lower()
        if pnl is not None:
            if pnl > 0: wins += 1
            elif pnl < 0: losses += 1
            else: breakeven += 1
            realized.append((t.created_at, pnl))
        elif 'breakeven' in state:
            breakeven += 1
        elif any(x in state for x in ['open', 'pending', 'working', 'accepted', 'new']):
            open_pending += 1

        rr = _safe_float(t.rr_ratio_2)
        if rr is None:
            rr = _safe_float(t.rr_ratio_1)
        if rr is not None:
            rr_values.append(rr)

        raw = _parse_json(t.raw_json)
        for key in ('skip_reason', 'blocked_reason', 'rejection_reason'):
            reason = raw.get(key)
            if reason:
                skip_reasons[str(reason)] += 1

    for s in scans:
        payload = _parse_json(s.payload_json)
        rejects = payload.get('rejections') or payload.get('blocked_reasons') or payload.get('skipped_reasons') or []
        if isinstance(rejects, dict):
            rejects = [f"{k}: {v}" for k,v in rejects.items()]
        for r in rejects:
            skip_reasons[str(r)] += 1

    realized.sort(key=lambda x: x[0] or datetime.min)
    eq = peak = 0.0
    max_dd = 0.0
    for _, pnl in realized:
        eq += pnl
        peak = max(peak, eq)
        max_dd = max(max_dd, peak - eq)

    best_trade = max(trades, key=lambda t: _safe_float(getattr(t, 'score_total', None)) or -1, default=None)
    best_setup = "No qualifying setup data was recorded today."
    if best_trade and best_trade.symbol:
        best_setup = f"{best_trade.symbol}: score {best_trade.score_total or 'n/a'}, entry {best_trade.entry_price}, stop {best_trade.stop_price}, target {best_trade.target_1}/{best_trade.target_2}."
    elif scans:
        top_scan = max(scans, key=lambda s: s.best_score or -1)
        if top_scan.best_symbol:
            best_setup = f"{top_scan.best_symbol}: scan score {top_scan.best_score or 'n/a'}."

    top_skip = skip_reasons.most_common(1)[0][0] if skip_reasons else ''
    total_realized = round(sum(x[1] for x in realized), 2)
    trades_taken = len(trades)
    decided = wins + losses + breakeven
    win_rate = round((wins / decided) * 100, 2) if decided else 0.0

    improvement = "Keep rules consistent and review entries before execution."
    if trades_taken == 0:
        improvement = "Stay patient. No valid A+ setup met your rules today."
    elif losses >= max(2, wins + 1):
        improvement = "Tighten confirmation before entry and review stop placement."
    elif rr_values and sum(rr_values)/len(rr_values) < 1.5:
        improvement = "Focus tomorrow on setups offering cleaner reward-to-risk before entry."
    elif top_skip:
        improvement = f"Review top skipped/block reason: {top_skip}."
    elif total_realized > 0:
        improvement = "Keep the same rules. Do not increase risk just because the day was green."

    discipline = max(0, min(100, 70 + (10 if not losses else -min(20, losses * 5)) + (10 if skip_reasons else 0)))
    rr_bonus = 10 if (rr_values and (sum(rr_values) / len(rr_values) >= 1.8)) else -5
    dd_bonus = -10 if max_dd > abs(total_realized) and max_dd > 0 else 5
    risk_score = max(0, min(100, 65 + rr_bonus + dd_bonus))
    execution = max(0, min(100, 70 + (10 if wins >= losses else -10) + (5 if open_pending == 0 else -5)))

    first_name = ((user.full_name or '').strip().split(' ')[0] if user.full_name else '') or 'Trader'
    skip_text = "No skipped-trade reasons were recorded today. XeanVI will continue tracking scanner decisions as your playbook history grows."
    if skip_reasons:
        skip_text = "\n".join([f"- {reason} ({count})" for reason, count in skip_reasons.most_common(5)])

    events = UserEvent.query.filter(UserEvent.user_id == user.id, UserEvent.created_at >= start_utc, UserEvent.created_at < end_utc).all()
    blocked_events = [e for e in events if 'block' in (e.event_name or '').lower() or 'reject' in (e.event_name or '').lower()]
    mistakes_blocked = f"{len(blocked_events)} rule-protection events were recorded and prevented." if blocked_events else "No rule-breaking executions were detected in today’s paper-trading log."

    return {
        'report_date': report_date.isoformat(), 'user_id': user.id, 'user_email': user.email, 'first_name': first_name,
        'trading_mode': user.trading_mode, 'trades_taken_count': trades_taken, 'wins_count': wins, 'losses_count': losses,
        'breakeven_count': breakeven, 'open_or_pending_count': open_pending, 'skipped_or_blocked_count': sum(skip_reasons.values()),
        'win_rate_percent': win_rate, 'total_realized_pnl': total_realized, 'average_risk_reward': round(sum(rr_values)/len(rr_values),2) if rr_values else 0.0,
        'best_risk_reward': round(max(rr_values),2) if rr_values else 0.0, 'max_drawdown': round(max_dd,2), 'mistakes_blocked': mistakes_blocked,
        'trades_skipped_and_why': skip_text, 'best_setup_of_day': best_setup, 'playbook_improvement_tomorrow': improvement,
        'discipline_score': discipline, 'risk_score': risk_score, 'execution_score': execution,
        'summary_headline': f"{report_date.isoformat()}: {wins}W/{losses}L, realized P&L {total_realized}",
    }


def send_daily_paper_report_email(user: User, report: dict):
    template_id = str(config.BREVO_DAILY_REPORT_TEMPLATE_ID or '').strip()
    if not template_id.isdigit():
        logger.warning('BREVO_DAILY_REPORT_TEMPLATE_ID missing or non-numeric; skipping daily report send.')
        return {'status': 'skipped', 'reason': 'missing_template_id'}
    if config.DAILY_REPORT_DRY_RUN:
        return {'status': 'dry_run', 'reason': 'dry_run_enabled'}

    to_email = config.DAILY_REPORT_TEST_RECIPIENT or user.email
    payload = {
        'sender': {'email': config.BREVO_SENDER_EMAIL, 'name': config.BREVO_SENDER_NAME},
        'to': [{'email': to_email, 'name': user.full_name or report.get('first_name') or 'Trader'}],
        'templateId': int(template_id),
        'params': {**report, 'app_url': config.APP_BASE_URL, 'support_email': config.BREVO_SENDER_EMAIL,
                   'disclaimer': 'XeanVI is trading workflow and execution-support software. This report is for educational and performance-review purposes only and is not financial advice. Trading involves risk and past performance does not guarantee future results.'},
        'tags': ['daily-paper-report', 'paper-trading', 'xeanvi-report-card'],
        'headers': {'X-XeanVI-Email-Type': 'daily-paper-report'},
    }
    if config.DAILY_REPORT_TEST_RECIPIENT:
        payload['subject'] = f"[TEST] Your XeanVI Paper-Trading Report Card — {report.get('report_date')}"

    response = requests.post('https://api.brevo.com/v3/smtp/email', headers={'accept':'application/json','api-key':config.BREVO_API_KEY,'content-type':'application/json'}, json=payload, timeout=15)
    if response.status_code >= 400:
        return {'status': 'failed', 'reason': f'brevo_{response.status_code}', 'raw': response.text[:250]}
    body = response.json() if response.content else {}
    return {'status': 'sent', 'brevo_message_id': str(body.get('messageId', ''))}


def run_daily_reports(report_date: date, send: bool, user_id=None, send_all=False, dry_run=False, force=False):
    users_q = User.query.filter(User.email.isnot(None))
    if user_id:
        users_q = users_q.filter(User.id == user_id)
    elif not send_all:
        users_q = users_q.filter(User.subscription_status == 'pro')
    users = users_q.all()
    out = {'attempted': 0, 'sent': 0, 'skipped': 0, 'failed': 0}
    for user in users:
        out['attempted'] += 1
        existing = DailyReportEmailLog.query.filter_by(user_id=user.id, report_date=report_date.isoformat()).first()
        if existing and not force:
            out['skipped'] += 1
            continue
        report = generate_daily_report(user, report_date)
        if dry_run:
            result = {'status': 'dry_run', 'reason': 'cli_dry_run'}
        elif send:
            result = send_daily_paper_report_email(user, report)
        else:
            result = {'status': 'skipped', 'reason': 'send_disabled'}

        log = existing or DailyReportEmailLog(user_id=user.id, report_date=report_date.isoformat(), email=(config.DAILY_REPORT_TEST_RECIPIENT or user.email), status=result.get('status','skipped'))
        log.status = result.get('status', 'skipped')
        log.reason = result.get('reason')
        log.brevo_message_id = result.get('brevo_message_id')
        log.raw_json = json.dumps(result)
        db.session.add(log)
        if log.status == 'sent': out['sent'] += 1
        elif log.status in {'failed'}: out['failed'] += 1
        else: out['skipped'] += 1
    db.session.commit()
    logger.info('daily_report summary attempted=%s sent=%s skipped=%s failed=%s', out['attempted'], out['sent'], out['skipped'], out['failed'])
    return out


def cli():
    p = argparse.ArgumentParser()
    p.add_argument('--date', default=date.today().isoformat())
    p.add_argument('--user-id', type=int)
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--send', action='store_true')
    p.add_argument('--send-all', action='store_true')
    p.add_argument('--force', action='store_true')
    a = p.parse_args()
    report_date = date.fromisoformat(a.date)
    with app.app_context():
        summary = run_daily_reports(report_date, send=a.send, user_id=a.user_id, send_all=a.send_all, dry_run=a.dry_run, force=a.force)
        print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    cli()
