#!/usr/bin/env python3
import datetime as dt
import subprocess
import sys
from pathlib import Path

BACKUP_DIR = Path('/var/backups/xeanvi-db')
PATTERN = 'xeanvi-postgres-*.dump'
MAX_AGE_HOURS = 13


def is_valid_dump(path: Path) -> bool:
    result = subprocess.run(['pg_restore', '-l', str(path)], capture_output=True, text=True)
    return result.returncode == 0


def main() -> int:
    now = dt.datetime.now(dt.timezone.utc)
    files = sorted(BACKUP_DIR.glob(PATTERN), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        print('No postgres backup files found.')
        return 1

    print('Recent backups:')
    recent_valid = False
    for path in files[:20]:
        mtime = dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc)
        age_hours = (now - mtime).total_seconds() / 3600
        valid = is_valid_dump(path)
        status = 'valid' if valid else 'invalid'
        print(f'- {path.name} | size={path.stat().st_size} bytes | ts={mtime.isoformat()} | {status}')
        if valid and age_hours <= MAX_AGE_HOURS:
            recent_valid = True

    if not recent_valid:
        print('FAIL: no valid backup exists within the last 13 hours.')
        return 1

    print('SUCCESS: at least one valid backup exists within the last 13 hours.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
