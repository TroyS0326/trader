#!/usr/bin/env bash
set -euo pipefail

CRON_LINE='0 3,15 * * * cd /var/www/stock/trader/stock && /var/www/stock/trader/stock/venv/bin/python scripts/backup_postgres.py >> /var/log/xeanvi-postgres-backup.log 2>&1'
STAMP="$(date -u +%Y%m%d-%H%M%S)"
CRON_BACKUP="/root/root-crontab-before-postgres-backup-install-${STAMP}.txt"

TMP_EXISTING="$(mktemp)"
TMP_NEW="$(mktemp)"

if crontab -l > "$TMP_EXISTING" 2>/dev/null; then
  cp "$TMP_EXISTING" "$CRON_BACKUP"
else
  : > "$TMP_EXISTING"
  cp "$TMP_EXISTING" "$CRON_BACKUP"
fi

echo "Saved existing root crontab to $CRON_BACKUP"

if rg -n 'veteran_trades\.db|sqlite' "$TMP_EXISTING" >/dev/null 2>&1; then
  echo 'Refusing to install: existing root crontab contains SQLite/veteran_trades backup references.'
  exit 1
fi

sed '/scripts\/backup_postgres\.py/d' "$TMP_EXISTING" > "$TMP_NEW"
printf '%s\n' "$CRON_LINE" >> "$TMP_NEW"

if rg -n 'veteran_trades\.db|sqlite' "$TMP_NEW" >/dev/null 2>&1; then
  echo 'Refusing to install: generated crontab contains SQLite/veteran_trades references.'
  exit 1
fi

crontab "$TMP_NEW"
echo 'Installed root crontab for twice-daily Postgres backups.'
echo 'Final root crontab:'
crontab -l

rm -f "$TMP_EXISTING" "$TMP_NEW"
