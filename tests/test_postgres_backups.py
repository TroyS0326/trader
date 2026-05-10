import datetime as dt
import os
import subprocess
from pathlib import Path

import importlib.util
import types
import sys
import pytest

ROOT = Path(__file__).resolve().parents[1]

sys.modules.setdefault('dotenv', types.SimpleNamespace(load_dotenv=lambda *args, **kwargs: True))
bp_spec = importlib.util.spec_from_file_location('backup_postgres', ROOT / 'scripts' / 'backup_postgres.py')
bp = importlib.util.module_from_spec(bp_spec)
assert bp_spec and bp_spec.loader
bp_spec.loader.exec_module(bp)

vp_spec = importlib.util.spec_from_file_location('verify_postgres_backups', ROOT / 'scripts' / 'verify_postgres_backups.py')
vp = importlib.util.module_from_spec(vp_spec)
assert vp_spec and vp_spec.loader
vp_spec.loader.exec_module(vp)


def test_backup_loads_database_url_via_dotenv(tmp_path, monkeypatch):
    envfile = tmp_path / 'xeanvi.env'
    envfile.write_text('FLASK_ENV=production\nDATABASE_URL=postgresql+psycopg://u:p@localhost:5432/xeanvi\n')
    monkeypatch.setattr(bp, 'ENV_FILE', envfile)
    monkeypatch.delenv('DATABASE_URL', raising=False)
    bp.load_production_env()
    assert os.getenv('DATABASE_URL', '').startswith('postgresql+psycopg://')


def test_psycopg_url_converted_to_pg_dump_compatible():
    out = bp.normalize_for_pg_dump('postgresql+psycopg://u:p@h:5432/xeanvi')
    assert out == 'postgresql://u:p@h:5432/xeanvi'


def test_failed_pg_dump_removes_incomplete_output(tmp_path, monkeypatch):
    out = tmp_path / 'bad.dump'

    def fake_run(*_args, **_kwargs):
        out.write_bytes(b'partial')
        return subprocess.CompletedProcess(args=['pg_dump'], returncode=1, stdout='', stderr='err')

    monkeypatch.setattr(bp.subprocess, 'run', fake_run)
    with pytest.raises(RuntimeError):
        bp.run_pg_dump('postgresql://u:p@localhost:5432/xeanvi', out)
    assert not out.exists()


def test_prune_only_deletes_xeanvi_dump_files(tmp_path, monkeypatch):
    old_match = tmp_path / 'xeanvi-postgres-20000101-000000.dump'
    old_match.write_text('x')
    keep_manual = tmp_path / 'manual-before-recovery-1.dump'
    keep_manual.write_text('x')
    keep_sqlite = tmp_path / 'veteran_trades-20200101.db.gz'
    keep_sqlite.write_text('x')

    old_ts = (dt.datetime.now().timestamp() - 20 * 86400)
    for p in (old_match, keep_manual, keep_sqlite):
        os.utime(p, (old_ts, old_ts))

    deleted = bp.prune_old_backups(tmp_path, 14)
    assert old_match in deleted
    assert not old_match.exists()
    assert keep_manual.exists()
    assert keep_sqlite.exists()


def test_verify_fails_if_no_recent_valid_backup(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(vp, 'BACKUP_DIR', tmp_path)
    old = tmp_path / 'xeanvi-postgres-20200101-000000.dump'
    old.write_bytes(b'x' * 20000)
    old_ts = (dt.datetime.now().timestamp() - 20 * 3600)
    os.utime(old, (old_ts, old_ts))

    monkeypatch.setattr(vp, 'is_valid_dump', lambda _p: True)
    rc = vp.main()
    captured = capsys.readouterr()
    assert rc == 1
    assert 'last 13 hours' in captured.out


def test_install_cron_removes_old_lines_and_adds_single_schedule():
    script = (ROOT / 'scripts' / 'install_postgres_backup_cron.sh').read_text()
    assert 'sed \'/scripts\\/backup_postgres\\.py/d\'' in script
    expected = '0 3,15 * * * cd /var/www/stock/trader/stock && /var/www/stock/trader/stock/venv/bin/python scripts/backup_postgres.py >> /var/log/xeanvi-postgres-backup.log 2>&1'
    assert expected in script


def test_no_active_veteran_trades_backup_behavior():
    for target in [
        ROOT / 'scripts' / 'backup_postgres.py',
        ROOT / 'scripts' / 'verify_postgres_backups.py',
        ROOT / 'scripts' / 'install_postgres_backup_cron.sh',
    ]:
        text = target.read_text().lower()
        assert 'veteran_trades.db' not in text
        assert 'backup_sqlite_db.sh' not in text
