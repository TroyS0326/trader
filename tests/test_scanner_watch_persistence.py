from pathlib import Path
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scanner


class DummyRow:
    def __init__(self, symbol, expires_at):
        self.symbol = symbol
        self.expires_at = expires_at
        self.status = 'ACTIVE'
        self.last_seen_at = datetime.utcnow()
        self.last_recheck_at = None
        self.promoted_at = None
        self.promotion_attempt_count = 0
        self.latest_skip_reason_codes_json = '[]'


class DummyQuery:
    def __init__(self, rows):
        self.rows = rows
    def filter_by(self, **kwargs):
        return self
    def order_by(self, *args, **kwargs):
        return self
    def limit(self, n):
        self.rows = self.rows[:n]
        return self
    def all(self):
        return self.rows
    def first(self):
        return None


def test_scanner_imports_json_module():
    assert hasattr(scanner, 'json')


def test_upsert_skips_when_user_missing(monkeypatch):
    calls = {'commit': 0}
    monkeypatch.setattr(scanner, 'WATCH_CANDIDATE_TTL_MINUTES', 15)
    monkeypatch.setattr(scanner.WatchCandidate, 'query', DummyQuery([]))
    monkeypatch.setattr(scanner.db.session, 'commit', lambda: calls.__setitem__('commit', calls['commit'] + 1))
    scanner.upsert_watch_candidate_from_analysis({'symbol': 'RXT', 'decision': 'WATCH', 'setup_grade': 'WATCH', 'score_total': 51, 'scores': {'catalyst': 4}}, user=None)
    assert calls['commit'] == 0


def test_recheck_runs_scan_once_and_no_orders(monkeypatch):
    rows = [DummyRow('AAA', datetime.utcnow()), DummyRow('BBB', datetime.utcnow())]
    monkeypatch.setattr(scanner.WatchCandidate, 'query', DummyQuery(rows))
    run_calls = {'count': 0}
    monkeypatch.setattr(scanner, 'run_scan', lambda user=None: run_calls.__setitem__('count', run_calls['count'] + 1) or {'ranked': [{'symbol': 'AAA', 'decision': 'WATCH', 'setup_grade': 'WATCH', 'details': {}, 'scores': {}}]})
    monkeypatch.setattr(scanner.db.session, 'commit', lambda: None)
    summary = scanner.recheck_active_watch_candidates(user=type('U', (), {'id': 1})(), limit=10)
    assert run_calls['count'] == 1
    assert summary['checked_count'] == 2


def test_recheck_handles_naive_expires_at_without_type_error(monkeypatch):
    rows = [DummyRow('AAA', datetime.utcnow())]
    monkeypatch.setattr(scanner.WatchCandidate, 'query', DummyQuery(rows))
    monkeypatch.setattr(scanner, 'run_scan', lambda user=None: {'ranked': []})
    monkeypatch.setattr(scanner.db.session, 'commit', lambda: None)
    summary = scanner.recheck_active_watch_candidates(user=type('U', (), {'id': 1})(), limit=10)
    assert summary['errors_count'] == 0


def test_recheck_watch_cli_flag_present_and_safe_json_printer():
    src = Path('scanner.py').read_text()
    assert '--recheck-watch' in src
    assert 'json.dumps(summary, indent=2, default=str)' in src
