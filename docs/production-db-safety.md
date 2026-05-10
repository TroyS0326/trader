# Production DB Safety

Required production environment:
- `FLASK_ENV=production`
- `FLASK_DEBUG=0`
- `DATABASE_URL=postgresql+psycopg://...`

SQLite must never be used in production because local files can be accidentally recreated, leading to silent empty databases.

## Diagnostics

- `python scripts/db_diagnose.py`

This prints redacted DB identity, table list, row counts for key tables, and latest timestamp fields without printing credentials.

## Backups

- `bash scripts/backup_postgres_db.sh`
- `bash scripts/backup_sqlite_db.sh`

Both scripts are backup-only and non-destructive.

## Restore policy

Restore is manual. Always take a backup of current state before any restore operation.
