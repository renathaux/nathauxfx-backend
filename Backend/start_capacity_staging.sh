#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
export CAPACITY_STAGING=1
export PYTHONPATH="$PWD/capacity_probe:$PWD"
export SIMULATOR_FAST_JOBS_ENABLED=1
export WEB_CONCURRENCY=1
exec python capacity_probe/server.py
