#!/usr/bin/env bash
set -euo pipefail

if [ -d "/var/www/stock/trader/stock" ]; then
  cd /var/www/stock/trader/stock
else
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  cd "$(dirname "$SCRIPT_DIR")"
fi

if [ -f "venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  source "venv/bin/activate"
fi

python scripts/uptime_check.py "$@"
