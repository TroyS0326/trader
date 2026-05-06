# PostgreSQL Backups

## Install required packages
```bash
sudo apt update
sudo apt install -y postgresql-client gzip rclone
```

## Create backup directory
```bash
sudo mkdir -p /var/backups/xeanvi/postgres
sudo chown -R xeanvi_user:xeanvi_user /var/backups/xeanvi
sudo chmod -R 700 /var/backups/xeanvi
```

## Environment variables (/etc/xeanvi/xeanvi.env)
```env
BACKUP_DIR=/var/backups/xeanvi/postgres
BACKUP_RETENTION_DAYS=14
BACKUP_RCLONE_REMOTE=
BACKUP_HEALTHCHECK_URL=
BACKUP_HEALTHCHECK_FAIL_URL=
```

## Run backup dry run
```bash
python scripts/backup_postgres.py --dry-run
```

## Run manual backup
```bash
python scripts/backup_postgres.py
```

## Cron example (VPS path)
```cron
15 3 * * * cd /var/www/stock/trader/stock && /var/www/stock/trader/stock/venv/bin/python scripts/backup_postgres.py >> /var/log/xeanvi-postgres-backup.log 2>&1
```

## Restore example
```bash
gunzip -c /var/backups/xeanvi/postgres/backup-file.sql.gz | psql "$DATABASE_URL"
```

## Security note
Backups contain user, account, subscription, and trading data. Treat backup files as highly sensitive and protect access to storage and transport.
The backup script passes the database password via `PGPASSWORD` (not command-line args) to reduce exposure in process listings.
Keep backup permissions locked down: backup directories should be `chmod 700`, and backup files should be `chmod 600`.
