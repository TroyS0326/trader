#!/usr/bin/env bash
set -euo pipefail

TARGET_DIR="/var/www/stock/trader/stock"
if [[ -d "$TARGET_DIR" ]]; then
  cd "$TARGET_DIR"
else
  cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fi

if [[ -f "venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source "venv/bin/activate"
fi

python scripts/backup_postgres.py "$@"
