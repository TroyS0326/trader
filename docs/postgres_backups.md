# Postgres Backups (Production)

Production backup coverage is Postgres-only for database `xeanvi`.

## Schedule
Backups run **twice daily** at **03:00 UTC** and **15:00 UTC** via root cron.

## Install the cron job
```bash
sudo bash scripts/install_postgres_backup_cron.sh
```

This installer:
- backs up the previous root crontab to `/root/root-crontab-before-postgres-backup-install-YYYYmmdd-HHMMSS.txt`
- removes any existing `scripts/backup_postgres.py` cron lines
- installs exactly one twice-daily backup line
- refuses installation if SQLite or `veteran_trades.db` backup references are present

## Manual backup
```bash
/var/www/stock/trader/stock/venv/bin/python scripts/backup_postgres.py
```

Requirements enforced by the script:
- loads `/etc/xeanvi/xeanvi.env` via `python-dotenv`
- `FLASK_ENV=production`
- `DATABASE_URL` must be Postgres and target database `xeanvi`

Backups are written to:
- `/var/backups/xeanvi-db/xeanvi-postgres-YYYYmmdd-HHMMSS.dump`

## Verify backups
```bash
python scripts/verify_postgres_backups.py
```

The verifier checks backup listing details (size, timestamp, `pg_restore -l` validity) and exits nonzero if no valid backup exists within the last 13 hours.

## Retention and pruning
- default retention: 14 days
- override with `POSTGRES_BACKUP_RETENTION_DAYS`
- only files matching `xeanvi-postgres-*.dump` are pruned
- manual `manual-before-recovery*.dump` files are preserved
- historical `veteran_trades` SQLite backup artifacts are preserved for audit/decommission history

## SQLite decommission notice
`veteran_trades.db` is decommissioned for production. Do not create or install any SQLite-based production backup jobs.
