# Production data recovery (one-time, manual)

This repository includes `scripts/recover_production_data.py` for **manual**, non-destructive recovery merges into production PostgreSQL.

## Safety model
- Default mode is `dry-run`.
- No blind dump restore/import is used.
- Apply mode requires `FLASK_ENV=production` and `DATABASE_URL` with `postgresql+psycopg://` pointing at `xeanvi`.
- Apply mode creates a `pg_dump` backup first unless `--skip-backup` is explicitly passed.

## Commands

### Dry-run from SQLite backup
```bash
python scripts/recover_production_data.py \
  --mode dry-run \
  --sqlite-path /var/backups/xeanvi-db/veteran_trades-2026-05-09-030000.db
```

### Dry-run from secondary Postgres (`xeanvi_restore_check`)
```bash
python scripts/recover_production_data.py \
  --mode dry-run \
  --source-postgres-db xeanvi_restore_check
```

### Apply from SQLite backup
```bash
FLASK_ENV=production DATABASE_URL='postgresql+psycopg://USER:PASS@HOST:5432/xeanvi' \
python scripts/recover_production_data.py \
  --mode apply \
  --sqlite-path /var/backups/xeanvi-db/veteran_trades-2026-05-09-030000.db
```

### Apply from secondary Postgres (`xeanvi_restore_check`)
```bash
FLASK_ENV=production DATABASE_URL='postgresql+psycopg://USER:PASS@HOST:5432/xeanvi' \
python scripts/recover_production_data.py \
  --mode apply \
  --source-postgres-db xeanvi_restore_check
```

### Verify after dry-run/apply
```bash
python scripts/db_diagnose.py
```
