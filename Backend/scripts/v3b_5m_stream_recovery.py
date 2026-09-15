#!/usr/bin/env python3
"""Admin CLI for non-executing V3B account-scoped stream recovery."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

import pandas as pd

from ctrader_connector import fetch_ctrader_historical_candles
from models import IndicatorEvent, IndicatorStreamState
from services import v3b_5m_stream_recovery as recovery
from strategies import strict_trader


def _json_default(value):
    if isinstance(value, (datetime, pd.Timestamp)):
        return pd.Timestamp(value).tz_convert("UTC").isoformat()
    return str(value)


def _load_state(storage_key, timeframe):
    session = recovery.SessionLocal()
    try:
        state = session.query(IndicatorStreamState).filter_by(
            symbol=storage_key, timeframe=timeframe
        ).one_or_none()
        if state is None:
            raise SystemExit(f"stream {storage_key} {timeframe} is not initialized")
        events = session.query(IndicatorEvent).filter_by(
            symbol=storage_key, timeframe=timeframe
        ).all()
        earliest = recovery.infer_earliest_rebuild_timestamp(state, events)
        if state.last_processed_candle is None:
            raise SystemExit("stream has no durable watermark")
        return earliest, pd.Timestamp(state.last_processed_candle)
    finally:
        session.close()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Recover one active account-scoped V3B stream without broker execution."
    )
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--symbol", required=True, choices=["EURUSD", "XAUUSD"])
    parser.add_argument("--timeframe", default="5m", choices=["5m", "15m", "1h"])
    parser.add_argument("--storage-key", required=True)
    parser.add_argument("--earliest-required-at")
    parser.add_argument("--dry-run", action="store_true", default=False)
    parser.add_argument("--apply", action="store_true", default=False)
    parser.add_argument("--lookback-candles", type=int, default=250)
    args = parser.parse_args(argv)
    if args.dry_run == args.apply:
        raise SystemExit("choose exactly one of --dry-run or --apply")

    explicit = args.earliest_required_at
    inferred, old_watermark = _load_state(args.storage_key, args.timeframe)
    earliest = pd.Timestamp(explicit) if explicit else inferred
    start = recovery.history_start_for_recovery(
        earliest,
        old_watermark,
        lookback_candles=args.lookback_candles,
        timeframe=args.timeframe,
    )
    interval_minutes = recovery.SUPPORTED_TIMEFRAMES[args.timeframe]
    end = (
        pd.Timestamp.now(tz="UTC").floor(f"{interval_minutes}min")
        - pd.Timedelta(minutes=interval_minutes)
    )
    frame = fetch_ctrader_historical_candles(
        args.symbol,
        args.timeframe,
        start,
        end,
    )
    request = recovery.RecoveryRequest(
        account_id=args.account_id,
        symbol=args.symbol,
        timeframe=args.timeframe,
        storage_key=args.storage_key,
        dry_run=args.dry_run,
        earliest_required_at=explicit,
    )
    if args.dry_run:
        result = recovery.plan_recovery(request, frame)
    else:
        result = recovery.apply_recovery(
            request,
            frame,
            strict_trader.point_size(args.symbol),
        )
    print(json.dumps(result, indent=2, sort_keys=True, default=_json_default))


if __name__ == "__main__":
    main()
