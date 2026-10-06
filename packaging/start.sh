#!/bin/sh
set -eu
: "${DATABASE_URL:?DATABASE_URL is required; refusing ephemeral production database}"
python -B -m startup_recovery.migrate
exec uvicorn closed_market_bootstrap:create_app --factory --host 0.0.0.0 --port "${PORT:-10000}"
