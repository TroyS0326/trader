#!/usr/bin/env python3
import argparse
import datetime as dt
import logging
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from dotenv import load_dotenv

try:
    import requests
except Exception:  # pragma: no cover
    requests = None

PROD_ENV_PATH = Path('/etc/xeanvi/xeanvi.env')
REPO_ROOT = Path(__file__).resolve().parent.parent


def load_environment() -> None:
    if PROD_ENV_PATH.exists():
        load_dotenv(PROD_ENV_PATH)
    else:
        load_dotenv(REPO_ROOT / '.env')


def normalize_database_url(raw_url: str) -> str:
    url = (raw_url or '').strip()
    if not url:
        raise ValueError('DATABASE_URL is required for PostgreSQL backups.')

    lower = url.lower()
    if lower.startswith('sqlite://'):
        raise ValueError('DATABASE_URL must point to PostgreSQL, not sqlite.')

    if lower.startswith('postgres://'):
        return 'postgresql://' + url[len('postgres://'):]
    if lower.startswith('postgresql+psycopg://'):
        return 'postgresql://' + url[len('postgresql+psycopg://'):]
    if lower.startswith('postgresql://'):
        return url

    raise ValueError('DATABASE_URL must use a PostgreSQL scheme (postgresql:// or postgres://).')


def redact_db_url(db_url: str) -> str:
    parsed = urlsplit(db_url)
    netloc = parsed.hostname or ''
    if parsed.port:
        netloc += f':{parsed.port}'
    if parsed.username:
        netloc = f'{parsed.username}:***@{netloc}'
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def run_backup(db_url: str, backup_file: Path) -> None:
    with backup_file.open('wb') as output:
        dump_proc = subprocess.Popen(['pg_dump', db_url], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        gzip_proc = subprocess.Popen(['gzip'], stdin=dump_proc.stdout, stdout=output, stderr=subprocess.PIPE)
        assert dump_proc.stdout is not None
        dump_proc.stdout.close()

        dump_stderr = dump_proc.stderr.read().decode('utf-8', errors='replace') if dump_proc.stderr else ''
        gzip_stderr = gzip_proc.stderr.read().decode('utf-8', errors='replace') if gzip_proc.stderr else ''

        dump_rc = dump_proc.wait()
        gzip_rc = gzip_proc.wait()

    if dump_rc != 0:
        raise RuntimeError(f'pg_dump failed with exit code {dump_rc}: {dump_stderr.strip()}')
    if gzip_rc != 0:
        raise RuntimeError(f'gzip failed with exit code {gzip_rc}: {gzip_stderr.strip()}')


def cleanup_old_backups(backup_dir: Path, retention_days: int) -> list[Path]:
    deleted: list[Path] = []
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=retention_days)
    patterns = ('xeanvi-postgres-*.sql.gz', 'xeanvi-postgres-*.dump.gz')

    for pattern in patterns:
        for path in backup_dir.glob(pattern):
            if not path.is_file():
                continue
            modified = dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc)
            if modified < cutoff:
                path.unlink()
                deleted.append(path)
    return deleted


def ping(url: str) -> None:
    if not url or not requests:
        return
    try:
        requests.get(url, timeout=10)
    except Exception as exc:  # pragma: no cover
        logging.warning('Healthcheck ping failed: %s', exc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Create compressed PostgreSQL backups.')
    parser.add_argument('--dry-run', action='store_true', help='Validate configuration without running backup')
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

    load_environment()

    fail_healthcheck_url = os.getenv('BACKUP_HEALTHCHECK_FAIL_URL', '').strip()

    try:
        db_url = normalize_database_url(os.getenv('DATABASE_URL', ''))
        backup_dir = Path(os.getenv('BACKUP_DIR', '/var/backups/xeanvi/postgres')).resolve()
        retention_days = int(os.getenv('BACKUP_RETENTION_DAYS', '14'))
        rclone_remote = os.getenv('BACKUP_RCLONE_REMOTE', '').strip()
        success_healthcheck_url = os.getenv('BACKUP_HEALTHCHECK_URL', '').strip()

        timestamp = dt.datetime.now().strftime('%Y%m%d-%H%M%S')
        backup_file = backup_dir / f'xeanvi-postgres-{timestamp}.sql.gz'

        logging.info('Starting PostgreSQL backup using %s', redact_db_url(db_url))
        logging.info('Backup directory: %s', backup_dir)

        if args.dry_run:
            logging.info('Dry run enabled; backup file would be: %s', backup_file)
            logging.info('Rclone upload would %srun', '' if rclone_remote else 'not ')
            return 0

        backup_dir.mkdir(parents=True, exist_ok=True)
        run_backup(db_url, backup_file)

        if not backup_file.exists() or backup_file.stat().st_size <= 0:
            raise RuntimeError('Backup file was not created correctly (missing or empty).')

        logging.info('Local backup created: %s', backup_file)

        if rclone_remote:
            subprocess.run(['rclone', 'copy', str(backup_file), rclone_remote], check=True)
            logging.info('Backup uploaded via rclone to %s', rclone_remote)
        else:
            logging.info('BACKUP_RCLONE_REMOTE not set; skipping off-server upload.')

        deleted = cleanup_old_backups(backup_dir, retention_days)
        for item in deleted:
            logging.info('Deleted old backup: %s', item.name)

        ping(success_healthcheck_url)
        return 0
    except Exception as exc:
        logging.error('Backup failed: %s', exc)
        ping(fail_healthcheck_url)
        return 1


if __name__ == '__main__':
    sys.exit(main())
