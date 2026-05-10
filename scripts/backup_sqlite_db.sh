#!/usr/bin/env bash
set -euo pipefail

DB_PATH="${DB_PATH:-veteran_trades.db}"
if [[ ! -f "$DB_PATH" ]]; then
  echo "Refusing backup: sqlite database file not found at $DB_PATH" >&2
  exit 1
fi

preferred="/var/backups/xeanvi-db"
fallback="backups/db"
if mkdir -p "$preferred" 2>/dev/null && [[ -w "$preferred" ]]; then
  backup_dir="$preferred"
else
  mkdir -p "$fallback"
  backup_dir="$fallback"
fi
name="$(basename "$DB_PATH")"
stamp="$(date -u +%Y%m%d-%H%M%S)"
out="$backup_dir/${stamp}-${name}.sqlite3"
cp "$DB_PATH" "$out"
echo "SQLite backup created: $out"
