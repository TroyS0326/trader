# Decommission: veteran_trades.db

`veteran_trades.db` is decommissioned for production use.

## Production database policy
- Production **must** use Postgres via `DATABASE_URL=postgresql+psycopg://.../xeanvi`.
- Production must not use SQLite, in-memory SQLite, relative SQLite paths, or any URI/path referencing `veteran_trades.db`.
- Historical SQLite backups are recovery archives only; they are not active production databases.

## Legacy cron policy
- Do not run old SQLite backup cron jobs in production.
- `/usr/local/bin/backup-xeanvi-db.sh` is a legacy path and should remain disabled.

## Validate production DB env
```bash
python scripts/validate_production_db_env.py
```

## Audit legacy references
```bash
bash scripts/audit_legacy_sqlite_artifacts.sh
```

## Safely quarantine a live veteran_trades.db on server
```bash
sudo install -d -m 700 /var/backups/xeanvi-legacy-quarantine
sudo test -f /var/www/stock/trader/stock/veteran_trades.db && \
  sudo mv /var/www/stock/trader/stock/veteran_trades.db \
  /var/backups/xeanvi-legacy-quarantine/veteran_trades.db.$(date -u +%Y%m%d-%H%M%S)
```
