#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "Refusing backup: DATABASE_URL is required." >&2
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
stamp="$(date -u +%Y%m%d-%H%M%S)"
out="$backup_dir/${stamp}-xeanvi.dump"
pg_dump --format=custom --file="$out" "$DATABASE_URL"
echo "PostgreSQL backup created: $out"
