#!/usr/bin/env bash
set -euo pipefail

echo "== root crontab =="
crontab -l 2>&1 || true

echo "== xeanvi_user crontab =="
crontab -u xeanvi_user -l 2>&1 || true

echo "== systemd services/timers references =="
rg -n "veteran_trades\.db|backup-xeanvi-db\.sh|DB_PATH|sqlite.*backup" /etc/systemd/system /lib/systemd/system 2>/dev/null || true

echo "== /usr/local/bin references =="
rg -n "veteran_trades\.db|backup-xeanvi-db\.sh|DB_PATH|sqlite.*backup" /usr/local/bin 2>/dev/null || true

echo "== repo references =="
rg -n "veteran_trades\.db|backup-xeanvi-db\.sh|DB_PATH|sqlite.*backup" .
