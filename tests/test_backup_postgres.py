import datetime as dt
from pathlib import Path

import importlib.util

_spec = importlib.util.spec_from_file_location("backup_postgres", Path(__file__).resolve().parents[1] / "scripts" / "backup_postgres.py")
bp = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(bp)


def test_missing_database_url_fails(monkeypatch, tmp_path):
    monkeypatch.setenv('DATABASE_URL', '')
    monkeypatch.setenv('BACKUP_DIR', str(tmp_path))
    assert bp.main([]) == 1


def test_sqlite_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv('DATABASE_URL', 'sqlite:///foo.db')
    monkeypatch.setenv('BACKUP_DIR', str(tmp_path))
    assert bp.main([]) == 1


def test_non_postgres_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv('DATABASE_URL', 'mysql://user:pass@host/db')
    monkeypatch.setenv('BACKUP_DIR', str(tmp_path))
    assert bp.main([]) == 1


def test_postgres_scheme_normalization():
    assert bp.normalize_database_url('postgres://u:p@h/db') == 'postgresql://u:p@h/db'
    assert bp.normalize_database_url('postgresql://u:p@h/db') == 'postgresql://u:p@h/db'
    assert bp.normalize_database_url('postgresql+psycopg://u:p@h/db') == 'postgresql://u:p@h/db'


def test_dry_run_no_subprocess(monkeypatch, tmp_path):
    called = {'value': False}

    def boom(*args, **kwargs):
        called['value'] = True
        raise AssertionError('subprocess should not be called in dry-run')

    monkeypatch.setenv('DATABASE_URL', 'postgres://user:secret@localhost/db')
    monkeypatch.setenv('BACKUP_DIR', str(tmp_path))
    monkeypatch.setattr(bp.subprocess, 'run', boom)
    monkeypatch.setattr(bp.subprocess, 'Popen', boom)
    assert bp.main(['--dry-run']) == 0
    assert called['value'] is False


def test_redaction_hides_password():
    redacted = bp.redact_db_url('postgresql://alice:supersecret@db.example.com:5432/app')
    assert 'supersecret' not in redacted
    assert '***' in redacted


def test_retention_deletes_only_matching_old_files(tmp_path):
    old_match = tmp_path / 'xeanvi-postgres-20200101-000000.sql.gz'
    old_match.write_text('x')
    old_dump = tmp_path / 'xeanvi-postgres-20200101-000000.dump.gz'
    old_dump.write_text('x')
    keep_other = tmp_path / 'notes.txt'
    keep_other.write_text('x')

    old_ts = (dt.datetime.now().timestamp() - 20 * 86400)
    for p in (old_match, old_dump, keep_other):
        p.chmod(0o600)
        import os
        os.utime(p, (old_ts, old_ts))

    deleted = bp.cleanup_old_backups(tmp_path, retention_days=14)
    names = {p.name for p in deleted}
    assert old_match.name in names
    assert old_dump.name in names
    assert keep_other.exists()


def test_rclone_skipped_when_missing(monkeypatch, tmp_path):
    monkeypatch.setenv('DATABASE_URL', 'postgres://user:secret@localhost/db')
    monkeypatch.setenv('BACKUP_DIR', str(tmp_path))
    monkeypatch.delenv('BACKUP_RCLONE_REMOTE', raising=False)

    def fake_run_backup(_db_url, backup_file):
        backup_file.write_bytes(b'abc')

    calls = []

    def fake_run(cmd, check):
        calls.append(cmd)

    monkeypatch.setattr(bp, 'run_backup', fake_run_backup)
    monkeypatch.setattr(bp.subprocess, 'run', fake_run)
    assert bp.main([]) == 0
    assert calls == []


def test_rclone_called_when_set(monkeypatch, tmp_path):
    monkeypatch.setenv('DATABASE_URL', 'postgres://user:secret@localhost/db')
    monkeypatch.setenv('BACKUP_DIR', str(tmp_path))
    monkeypatch.setenv('BACKUP_RCLONE_REMOTE', 'remote:bucket/path')

    def fake_run_backup(_db_url, backup_file):
        backup_file.write_bytes(b'abc')

    calls = []

    def fake_run(cmd, check):
        calls.append(cmd)

    monkeypatch.setattr(bp, 'run_backup', fake_run_backup)
    monkeypatch.setattr(bp.subprocess, 'run', fake_run)
    assert bp.main([]) == 0
    assert any(c[0] == 'rclone' for c in calls)


def test_healthcheck_failure_does_not_fail(monkeypatch, tmp_path):
    monkeypatch.setenv('DATABASE_URL', 'postgres://user:secret@localhost/db')
    monkeypatch.setenv('BACKUP_DIR', str(tmp_path))
    monkeypatch.setenv('BACKUP_HEALTHCHECK_URL', 'https://example.com/success')

    def fake_run_backup(_db_url, backup_file):
        backup_file.write_bytes(b'abc')

    class BoomRequests:
        @staticmethod
        def get(*_args, **_kwargs):
            raise RuntimeError('boom')

    monkeypatch.setattr(bp, 'run_backup', fake_run_backup)
    monkeypatch.setattr(bp, 'requests', BoomRequests)
    assert bp.main([]) == 0


class _FakeStream:
    def __init__(self, data: bytes = b''):
        self._data = data

    def read(self) -> bytes:
        return self._data

    def close(self) -> None:
        return None


class _FakeProc:
    def __init__(self, rc: int = 0, stderr: bytes = b'', stdout: bytes | None = b''):
        self.stderr = _FakeStream(stderr)
        self.stdout = _FakeStream(stdout) if stdout is not None else None
        self._rc = rc

    def wait(self) -> int:
        return self._rc


def test_run_backup_uses_pgpassword_and_sanitized_pg_dump_target(monkeypatch, tmp_path):
    captured = {}

    def fake_popen(cmd, **kwargs):
        if cmd[0] == 'pg_dump':
            captured['pg_cmd'] = cmd
            captured['pg_env'] = kwargs.get('env', {})
            return _FakeProc(rc=0, stderr=b'', stdout=b'dump')
        if cmd[0] == 'gzip':
            return _FakeProc(rc=0, stderr=b'', stdout=None)
        raise AssertionError('unexpected command')

    monkeypatch.setattr(bp.subprocess, 'Popen', fake_popen)
    backup_file = tmp_path / 'backup.sql.gz'
    db_url = 'postgresql://xeanvi_user:secret@127.0.0.1:5432/xeanvi?sslmode=require'
    bp.run_backup(db_url, backup_file)

    assert captured['pg_cmd'][1] == 'postgresql://xeanvi_user@127.0.0.1:5432/xeanvi?sslmode=require'
    assert 'secret' not in captured['pg_cmd'][1]
    assert captured['pg_env']['PGPASSWORD'] == 'secret'
    assert oct(backup_file.stat().st_mode & 0o777) == '0o600'


def test_run_backup_decodes_urlencoded_password_for_pgpassword(monkeypatch, tmp_path):
    captured = {}

    def fake_popen(cmd, **kwargs):
        if cmd[0] == 'pg_dump':
            captured['env'] = kwargs.get('env', {})
            return _FakeProc(rc=0, stderr=b'', stdout=b'dump')
        if cmd[0] == 'gzip':
            return _FakeProc(rc=0, stderr=b'', stdout=None)
        raise AssertionError('unexpected command')

    monkeypatch.setattr(bp.subprocess, 'Popen', fake_popen)
    db_url = 'postgresql://u:p%40ss%3Aword@localhost:5432/app'
    bp.run_backup(db_url, tmp_path / 'backup.sql.gz')
    assert captured['env']['PGPASSWORD'] == 'p@ss:word'


def test_run_backup_scrubs_pg_dump_failure(monkeypatch, tmp_path):
    def fake_popen(cmd, **kwargs):
        if cmd[0] == 'pg_dump':
            err = b'auth failed for postgresql://u:secret@localhost:5432/app and secret and postgresql://u@localhost:5432/app'
            return _FakeProc(rc=1, stderr=err, stdout=b'dump')
        if cmd[0] == 'gzip':
            return _FakeProc(rc=0, stderr=b'', stdout=None)
        raise AssertionError('unexpected command')

    monkeypatch.setattr(bp.subprocess, 'Popen', fake_popen)
    db_url = 'postgresql://u:secret@localhost:5432/app'
    with __import__('pytest').raises(RuntimeError) as excinfo:
        bp.run_backup(db_url, tmp_path / 'backup.sql.gz')

    msg = str(excinfo.value)
    assert 'secret' not in msg
    assert db_url not in msg
    assert 'postgresql://u@localhost:5432/app' not in msg
