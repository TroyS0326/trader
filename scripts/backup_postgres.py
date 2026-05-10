#!/usr/bin/env python3
import datetime as dt
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from dotenv import load_dotenv

ENV_FILE = Path('/etc/xeanvi/xeanvi.env')
BACKUP_DIR = Path('/var/backups/xeanvi-db')
BACKUP_PREFIX = 'xeanvi-postgres-'
BACKUP_SUFFIX = '.dump'
MIN_VALID_SIZE_BYTES = 10 * 1024


def load_production_env() -> None:
    if not load_dotenv(ENV_FILE, override=False):
        raise RuntimeError(f'Missing required environment file: {ENV_FILE}')


def normalize_for_pg_dump(database_url: str) -> str:
    raw = (database_url or '').strip()
    if not raw:
        raise ValueError('DATABASE_URL is required.')
    if raw.startswith('postgresql+psycopg://'):
        return 'postgresql://' + raw[len('postgresql+psycopg://'):]
    if raw.startswith('postgresql://'):
        return raw
    raise ValueError('DATABASE_URL must start with postgresql+psycopg:// or postgresql://.')


def require_xeanvi_db(pg_url: str) -> None:
    db_name = urlsplit(pg_url).path.lstrip('/')
    if db_name != 'xeanvi':
        raise ValueError('DATABASE_URL must target database xeanvi.')


def backup_filename(now: dt.datetime | None = None) -> str:
    ts = (now or dt.datetime.now(dt.timezone.utc)).strftime('%Y%m%d-%H%M%S')
    return f'{BACKUP_PREFIX}{ts}{BACKUP_SUFFIX}'


def run_pg_dump(pg_url: str, output_file: Path) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    cmd = ['pg_dump', '-Fc', '-f', str(output_file), pg_url]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        if output_file.exists():
            output_file.unlink()
        stderr = (proc.stderr or '').strip()
        raise RuntimeError(f'pg_dump failed: {stderr or "no stderr"}')


def validate_backup_file(path: Path) -> None:
    if not path.exists():
        raise RuntimeError('Backup validation failed: file does not exist.')
    if path.stat().st_size <= MIN_VALID_SIZE_BYTES:
        raise RuntimeError('Backup validation failed: file size must be greater than 10 KB.')
    check = subprocess.run(['pg_restore', '-l', str(path)], capture_output=True, text=True)
    if check.returncode != 0:
        raise RuntimeError('Backup validation failed: pg_restore -l failed.')


def prune_old_backups(backup_dir: Path, retention_days: int) -> list[Path]:
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=retention_days)
    deleted: list[Path] = []
    for path in backup_dir.glob(f'{BACKUP_PREFIX}*{BACKUP_SUFFIX}'):
        if not path.is_file():
            continue
        mtime = dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc)
        if mtime < cutoff:
            path.unlink()
            deleted.append(path)
    return deleted


def main() -> int:
    try:
        load_production_env()
        flask_env = os.getenv('FLASK_ENV', '').strip().lower()
        if flask_env != 'production':
            raise RuntimeError('Refusing backup: FLASK_ENV must be production.')

        database_url = os.getenv('DATABASE_URL', '')
        pg_url = normalize_for_pg_dump(database_url)
        require_xeanvi_db(pg_url)

        retention_days = int(os.getenv('POSTGRES_BACKUP_RETENTION_DAYS', '14'))

        backup_path = BACKUP_DIR / backup_filename()
        print(f'Starting backup to {backup_path}')
        run_pg_dump(pg_url, backup_path)
        validate_backup_file(backup_path)
        removed = prune_old_backups(BACKUP_DIR, retention_days)

        print(f'SUCCESS: created and validated {backup_path.name}')
        print(f'Pruned {len(removed)} old backup(s).')
        return 0
    except Exception as exc:
        print(f'FAILURE: {exc}')
        return 1


if __name__ == '__main__':
    sys.exit(main())
