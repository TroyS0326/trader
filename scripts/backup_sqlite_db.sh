#!/usr/bin/env bash
set -euo pipefail

DB_PATH="${DB_PATH:-veteran_trades.db}"
FLASK_ENV_VALUE="${FLASK_ENV:-}"

if [[ "$FLASK_ENV_VALUE" == "production" && "${ALLOW_PRODUCTION_SQLITE_BACKUP:-0}" != "1" ]]; then
  echo "Refusing backup: FLASK_ENV=production and ALLOW_PRODUCTION_SQLITE_BACKUP is not 1." >&2
  exit 1
fi

if [[ "$DB_PATH" == *"veteran_trades.db" && "${ALLOW_LEGACY_VETERAN_TRADES_BACKUP:-0}" != "1" ]]; then
  echo "Refusing backup: veteran_trades.db is decommissioned; set ALLOW_LEGACY_VETERAN_TRADES_BACKUP=1 to override." >&2
  exit 1
fi

if [[ ! -f "$DB_PATH" ]]; then
  echo "Refusing backup: sqlite database file not found at $DB_PATH" >&2
  exit 1
fi

size_bytes=$(stat -c%s "$DB_PATH")
if (( size_bytes < 1048576 )) && [[ "${ALLOW_SMALL_SQLITE_BACKUP:-0}" != "1" ]]; then
  echo "Refusing backup: sqlite database is smaller than 1MB (${size_bytes} bytes); set ALLOW_SMALL_SQLITE_BACKUP=1 to override." >&2
  exit 1
fi

integrity_output=$(sqlite3 "$DB_PATH" 'PRAGMA integrity_check;' || true)
if [[ "$integrity_output" != "ok" ]]; then
  echo "Refusing backup: sqlite integrity_check failed for $DB_PATH." >&2
  echo "integrity_check output: $integrity_output" >&2
  exit 1
fi

backup_dir="/var/backups/xeanvi-db"
mkdir -p "$backup_dir"
name="$(basename "$DB_PATH")"
stamp="$(date -u +%Y%m%d-%H%M%S)"
out="$backup_dir/${stamp}-${name}.sqlite3"
cp "$DB_PATH" "$out"
echo "SQLite backup created: $out"
