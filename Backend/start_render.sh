#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "DATABASE_URL is required on Render. Refusing to start with ephemeral SQLite."
  exit 1
fi

python -m alembic -c alembic.ini upgrade head

# One-shot, fail-closed recovery for the account-scoped 15m indicator streams.
# The recovery service is market-data + database only, requires LIVE Auto OFF,
# validates authoritative CLOSED cTrader history, and refuses irreversible rows.
if [[ "${V3B_STREAM_RECOVERY_ON_STARTUP:-}" == "48869794" ]]; then
  echo "V3B_STREAM_RECOVERY_START account=48869794 timeframe=15m"
  recovery_failed=0
  for spec in     "EURUSD EURUSD~5E9BDF6606"     "XAUUSD XAUUSD~0EE2E21E3D"
  do
    read -r symbol storage_key <<< "$spec"
    echo "V3B_STREAM_RECOVERY_DRY_RUN symbol=$symbol storage_key=$storage_key"
    if python -m scripts.v3b_5m_stream_recovery       --account-id 48869794       --symbol "$symbol"       --timeframe 15m       --storage-key "$storage_key"       --dry-run       --lookback-candles 250
    then
      echo "V3B_STREAM_RECOVERY_APPLY symbol=$symbol storage_key=$storage_key"
      if ! python -m scripts.v3b_5m_stream_recovery         --account-id 48869794         --symbol "$symbol"         --timeframe 15m         --storage-key "$storage_key"         --apply         --lookback-candles 250
      then
        echo "V3B_STREAM_RECOVERY_APPLY_FAILED symbol=$symbol"
        recovery_failed=1
      fi
    else
      echo "V3B_STREAM_RECOVERY_DRY_RUN_BLOCKED symbol=$symbol"
      recovery_failed=1
    fi
  done
  echo "V3B_STREAM_RECOVERY_DONE account=48869794 failed=$recovery_failed"
fi

exec uvicorn closed_market_bootstrap:app --host 0.0.0.0 --port "${PORT:-10000}"
