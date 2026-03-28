import json
import sqlite3
from collections import defaultdict
from datetime import datetime
from statistics import mean
from zoneinfo import ZoneInfo

from config import DB_PATH, TIMEZONE_LABEL


def _load_rows():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        try:
            trades = conn.execute(
                '''
                SELECT id, created_at, scan_id, outcome, symbol
                FROM trades
                WHERE scan_id IS NOT NULL
                ORDER BY id ASC
                '''
            ).fetchall()
            scans = conn.execute(
                '''
                SELECT id, payload_json
                FROM scans
                '''
            ).fetchall()
        except sqlite3.OperationalError:
            return [], {}
        return trades, {int(row['id']): json.loads(row['payload_json']) for row in scans}
    finally:
        conn.close()


def _is_win(outcome: str) -> bool:
    return outcome in {'win', 'partial_win', 'working_or_filled'}


def main():
    trades, scans = _load_rows()
    if not trades:
        print('No trades found in veteran_trades.db')
        return

    by_confidence = defaultdict(lambda: {'wins': 0, 'total': 0})
    by_grade_bucket = defaultdict(lambda: {'fails': 0, 'total': 0})
    outcomes = []

    for trade in trades:
        scan = scans.get(int(trade['scan_id']) or 0, {})
        best = (scan or {}).get('best_pick') or {}
        details = best.get('details') or {}
        confidence = (details.get('catalyst') or {}).get('confidence', 'unknown')
        grade = best.get('setup_grade', 'UNKNOWN')
        outcome = (trade['outcome'] or 'open').lower()
        created_at = datetime.fromisoformat(str(trade['created_at'])).astimezone(ZoneInfo(TIMEZONE_LABEL))
        minute_bucket = f'{created_at.hour:02d}:{created_at.minute:02d}'

        by_confidence[confidence]['total'] += 1
        by_confidence[confidence]['wins'] += 1 if _is_win(outcome) else 0

        if outcome in {'loss', 'stopped_out', 'rejected', 'failed'}:
            by_grade_bucket[(grade, minute_bucket)]['fails'] += 1
        by_grade_bucket[(grade, minute_bucket)]['total'] += 1
        outcomes.append(1 if _is_win(outcome) else 0)

    print('=== CONFIDENCE VS WIN RATE ===')
    for confidence, stats in sorted(by_confidence.items(), key=lambda x: x[1]['total'], reverse=True):
        total = stats['total']
        win_rate = (stats['wins'] / total * 100.0) if total else 0.0
        print(f'- {confidence:>8}: {stats["wins"]}/{total} wins ({win_rate:.1f}%)')

    print('\n=== SETUP GRADE / TIME FAILURE HOTSPOTS ===')
    lockouts = []
    for (grade, minute_bucket), stats in sorted(by_grade_bucket.items(), key=lambda x: x[1]['total'], reverse=True):
        total = stats['total']
        fail_rate = (stats['fails'] / total * 100.0) if total else 0.0
        if total >= 5 and fail_rate >= 60.0:
            lockouts.append((grade, minute_bucket, total, fail_rate))
            print(f'- LOCKOUT candidate: grade={grade} @ {minute_bucket} -> fail {fail_rate:.1f}% on n={total}')

    if not lockouts:
        print('- No lockout windows met the >=60% failure rule with n>=5.')
    else:
        print('\nSuggested hard lockouts:')
        for grade, minute_bucket, total, fail_rate in lockouts:
            print(f'  * Block {grade} setups near {minute_bucket} ET (fail={fail_rate:.1f}% across {total} trades).')

    print(f'\nOverall estimated win rate: {mean(outcomes) * 100.0:.1f}% on {len(outcomes)} trades.')


if __name__ == '__main__':
    main()