"""Pure in-memory candle bundle builder for Strategy Simulator.

This module deliberately has no database imports. Simulator history arrives
from the frontend static replay JSON dataset and is aggregated locally.
"""
from __future__ import annotations

import math

import pandas as pd


_BASE_INTERVAL = pd.Timedelta(minutes=5)
_AGGREGATION = {
    "15m": ("15min", pd.Timedelta(minutes=15)),
    "1h": ("1h", pd.Timedelta(hours=1)),
    "4h": ("4h", pd.Timedelta(hours=4)),
}


def _utc(value) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        return stamp.tz_localize("UTC")
    return stamp.tz_convert("UTC")


def _value(row, name):
    if isinstance(row, dict):
        return row.get(name)
    return getattr(row, name, None)


def _canonical_frame(candles, start, end) -> pd.DataFrame:
    start_utc = _utc(start)
    end_utc = _utc(end)
    if end_utc <= start_utc:
        raise ValueError("SIMULATION_RANGE_INVALID")

    records = []
    timestamps = []
    seen = set()
    for candle in candles or []:
        timestamp = _utc(_value(candle, "timestamp"))
        # Keep client-supplied candles before the requested start as warm-up
        # history. They seed BOS/CHOCH, swing structure and EMAs, but the
        # simulator will not open/count trades before the requested start.
        if timestamp >= end_utc:
            continue
        if timestamp in seen:
            raise ValueError("STATIC_SIMULATION_HISTORY_INVALID: duplicate candle timestamp")
        seen.add(timestamp)

        values = {}
        for source, target in (
            ("open", "Open"),
            ("high", "High"),
            ("low", "Low"),
            ("close", "Close"),
        ):
            try:
                number = float(_value(candle, source))
            except (TypeError, ValueError):
                raise ValueError(
                    f"STATIC_SIMULATION_HISTORY_INVALID: invalid {source}"
                )
            if not math.isfinite(number):
                raise ValueError(
                    f"STATIC_SIMULATION_HISTORY_INVALID: invalid {source}"
                )
            values[target] = number

        volume_raw = _value(candle, "volume")
        try:
            volume = float(volume_raw) if volume_raw is not None else 0.0
        except (TypeError, ValueError):
            volume = 0.0
        values["Volume"] = volume if math.isfinite(volume) else 0.0
        timestamps.append(timestamp)
        records.append(values)

    if not records:
        raise ValueError("STATIC_SIMULATION_HISTORY_UNAVAILABLE")

    frame = pd.DataFrame(records, index=pd.DatetimeIndex(timestamps))
    frame = frame.sort_index()
    if not (frame.index >= start_utc).any():
        raise ValueError("STATIC_SIMULATION_HISTORY_UNAVAILABLE")
    return frame[["Open", "High", "Low", "Close", "Volume"]]


def _complete_bucket_starts(data, duration, cutoff):
    required_count = int(duration / _BASE_INTERVAL)
    available = set(data.index)
    starts = []
    for bucket_start in data.index.floor(duration).unique():
        bucket_start = _utc(bucket_start)
        if bucket_start + duration > cutoff:
            continue
        expected = [
            bucket_start + index * _BASE_INTERVAL
            for index in range(required_count)
        ]
        if all(timestamp in available for timestamp in expected):
            starts.append(bucket_start)
    return sorted(starts)


def _aggregate(frame_5m, timeframe, end_exclusive):
    cutoff = _utc(end_exclusive)
    if timeframe == "5m":
        return frame_5m.loc[frame_5m.index < cutoff].copy()

    rule, duration = _AGGREGATION[timeframe]
    result = (
        frame_5m.resample(rule, label="left", closed="left", origin="epoch")
        .agg({
            "Open": "first",
            "High": "max",
            "Low": "min",
            "Close": "last",
            "Volume": "sum",
        })
        .dropna(subset=["Open", "High", "Low", "Close"])
    )
    complete = _complete_bucket_starts(frame_5m, duration, cutoff)
    return result.loc[result.index.isin(complete)]


def build_static_market_bundle(candles_5m, start, end):
    """Build the simulator's 5m/15m/1h/4h bundle without touching Neon."""
    frame_5m = _canonical_frame(candles_5m, start, end)
    return {
        "5m": frame_5m,
        "15m": _aggregate(frame_5m, "15m", end),
        "1h": _aggregate(frame_5m, "1h", end),
        "4h": _aggregate(frame_5m, "4h", end),
    }
