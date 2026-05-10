#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${DATABASE_URL:-}" ]]; then
  DATABASE_URL="$(python - <<'PY'
from dotenv import dotenv_values
vals = dotenv_values('/etc/xeanvi/xeanvi.env')
print((vals.get('DATABASE_URL') or '').strip())
PY
)"
fi

if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "Refusing backup: DATABASE_URL is required." >&2
  exit 1
fi
if [[ "$DATABASE_URL" != postgresql+psycopg://* ]]; then
  echo "Refusing backup: DATABASE_URL must use postgresql+psycopg://." >&2
  exit 1
fi

PG_DUMP_URL="postgresql://${DATABASE_URL#postgresql+psycopg://}"
backup_dir="/var/backups/xeanvi-db"
mkdir -p "$backup_dir"
stamp="$(date -u +%Y%m%d-%H%M%S)"
out="$backup_dir/${stamp}-xeanvi.dump"

if ! pg_dump --format=custom --file="$out" "$PG_DUMP_URL"; then
  rm -f "$out"
  echo "PostgreSQL backup failed; incomplete dump removed." >&2
  exit 1
fi

if [[ ! -s "$out" ]]; then
  rm -f "$out"
  echo "PostgreSQL backup failed; dump output is empty." >&2
  exit 1
fi

echo "PostgreSQL backup created: $out"
