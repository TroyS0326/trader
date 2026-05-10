#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "Refusing backup: DATABASE_URL is required." >&2
  exit 1
fi

mkdir -p backups/db
stamp="$(date -u +%Y%m%d-%H%M%S)"
out="backups/db/${stamp}-xeanvi.dump"
pg_dump --format=custom --file="$out" "$DATABASE_URL"
echo "PostgreSQL backup created: $out"
