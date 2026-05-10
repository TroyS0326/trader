#!/usr/bin/env bash
set -euo pipefail

DB_PATH="${DB_PATH:-veteran_trades.db}"
if [[ ! -f "$DB_PATH" ]]; then
  echo "Refusing backup: sqlite database file not found at $DB_PATH" >&2
  exit 1
fi

mkdir -p backups/db
name="$(basename "$DB_PATH")"
stamp="$(date -u +%Y%m%d-%H%M%S)"
out="backups/db/${stamp}-${name}.sqlite3"
cp "$DB_PATH" "$out"
echo "SQLite backup created: $out"
