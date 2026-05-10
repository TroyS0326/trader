import os
import subprocess
from pathlib import Path


def test_backup_sqlite_refuses_production(tmp_path):
    db = tmp_path / 'live.db'
    db.write_bytes(b'x' * (1024 * 1024 + 1))
    out = subprocess.run(
        ['bash', 'scripts/backup_sqlite_db.sh'],
        env={**os.environ, 'FLASK_ENV': 'production', 'DB_PATH': str(db)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode != 0
    assert 'ALLOW_PRODUCTION_SQLITE_BACKUP' in (out.stderr + out.stdout)


def test_backup_sqlite_refuses_small_db(tmp_path):
    db = tmp_path / 'db.sqlite3'
    db.write_bytes(b'x' * 100)
    out = subprocess.run(
        ['bash', 'scripts/backup_sqlite_db.sh'],
        env={**os.environ, 'FLASK_ENV': 'development', 'DB_PATH': str(db), 'ALLOW_LEGACY_VETERAN_TRADES_BACKUP': '1'},
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode != 0
    assert 'smaller than 1MB' in (out.stderr + out.stdout)


def test_backup_postgres_contains_conversion_logic():
    src = Path('scripts/backup_postgres_db.sh').read_text()
    assert 'postgresql+psycopg://' in src
    assert 'PG_DUMP_URL="postgresql://${DATABASE_URL#postgresql+psycopg://}"' in src
