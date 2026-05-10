#!/usr/bin/env python3
import os
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values

ENV_PATH = Path('/etc/xeanvi/xeanvi.env')
LEGACY_DB = Path('/var/www/stock/trader/stock/veteran_trades.db')
LEGACY_BACKUP_SCRIPT = Path('/usr/local/bin/backup-xeanvi-db.sh')


def redact(url: str) -> str:
    if not url:
        return ''
    p = urlsplit(url)
    user = p.username or ''
    pw = ':***' if p.password is not None else ''
    at = '@' if user else ''
    host = p.hostname or ''
    port = f':{p.port}' if p.port else ''
    return f"{p.scheme}://{user}{pw}{at}{host}{port}{p.path}"


def main() -> int:
    env = dotenv_values(str(ENV_PATH)) if ENV_PATH.exists() else {}
    flask_env = (env.get('FLASK_ENV') or os.getenv('FLASK_ENV', '')).strip()
    flask_debug = (env.get('FLASK_DEBUG') or os.getenv('FLASK_DEBUG', '')).strip()
    db_url = (env.get('DATABASE_URL') or os.getenv('DATABASE_URL', '')).strip()

    parsed = urlsplit(db_url) if db_url else None
    db_name = (parsed.path.lstrip('/') if parsed else '')
    scheme = parsed.scheme if parsed else ''

    print(f"FLASK_ENV={flask_env}")
    print(f"FLASK_DEBUG={flask_debug}")
    print(f"DATABASE_URL={redact(db_url)}")
    print(f"DB dialect={scheme.split('+')[0] if scheme else ''}")
    print(f"DB name={db_name}")

    if flask_env == 'production':
        if not db_url:
            raise RuntimeError('Production must use Postgres; veteran_trades.db is decommissioned.')
        lowered = db_url.lower()
        if lowered.startswith('sqlite') or 'veteran_trades.db' in lowered or not db_url.startswith('postgresql+psycopg://'):
            raise RuntimeError('Production must use Postgres; veteran_trades.db is decommissioned.')
        if db_name != 'xeanvi':
            raise RuntimeError('Production database name must be xeanvi.')

    if LEGACY_DB.exists():
        print(f"WARNING: legacy DB exists at {LEGACY_DB}")
    if LEGACY_BACKUP_SCRIPT.exists():
        print(f"WARNING: legacy backup script exists at {LEGACY_BACKUP_SCRIPT}")

    for label, cmd in [
        ('root crontab', ['crontab', '-l']),
        ('xeanvi_user crontab', ['crontab', '-u', 'xeanvi_user', '-l']),
    ]:
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, check=False)
            txt = (out.stdout or '') + (out.stderr or '')
        except Exception as exc:
            print(f"WARNING: unable to read {label}: {exc}")
            continue
        if 'veteran_trades.db' in txt or 'backup-xeanvi-db.sh' in txt:
            print(f"WARNING: {label} references legacy sqlite artifact")

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
