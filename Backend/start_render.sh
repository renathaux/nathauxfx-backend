#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "DATABASE_URL is required on Render. Refusing to start with ephemeral SQLite."
  exit 1
fi

python -m alembic -c alembic.ini upgrade head
exec uvicorn closed_market_bootstrap:app --host 0.0.0.0 --port "${PORT:-10000}"
