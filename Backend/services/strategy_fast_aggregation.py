"""FAST-only aggregation without per-candle Python Timestamp membership sets.

Keep pandas' aggregation operations identical to the static replay path so OHLC
and floating-point volume sums remain exact. Completeness is numeric: every
expected five-minute timestamp must exist, even if a bucket has extra off-grid
candles. Replay and LIVE continue to use their existing aggregation modules.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from services.strategy_simulator_static_data import _AGGREGATION, _utc


def _aggregate_chunk(frame_5m: pd.DataFrame, timeframe: str, end_exclusive) -> pd.DataFrame:
    cutoff = _utc(end_exclusive)
    if timeframe == "5m":
        return frame_5m.loc[frame_5m.index < cutoff].copy()
    rule, duration = _AGGREGATION[timeframe]
    result = (
        frame_5m.resample(rule, label="left", closed="left", origin="epoch")
        .agg({"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"})
    )
    # Preserve the source index's numeric unit: converting five years of us
    # timestamps to ns would allocate another full-size timestamp buffer.
    unit = frame_5m.index.unit
    available = frame_5m.index.asi8
    if not frame_5m.index.is_monotonic_increasing:
        available = np.sort(available)
    starts = result.index.as_unit(unit).asi8
    duration_ticks = int(duration.asm8.astype(f"timedelta64[{unit}]").view(np.int64))
    cutoff_ticks = int(cutoff.asm8.astype(f"datetime64[{unit}]").view(np.int64))
    complete = starts <= cutoff_ticks - duration_ticks
    step = int(pd.Timedelta(minutes=5).asm8.astype(f"timedelta64[{unit}]").view(np.int64))
    for offset in range(0, duration_ticks, step):
        expected = starts + offset
        positions = np.searchsorted(available, expected)
        present = positions < len(available)
        present[present] &= available[positions[present]] == expected[present]
        complete &= present
    # Apply both filters once. dropna followed by loc retains another full
    # aggregated OHLCV frame while computing the completeness mask.
    for column in ("Open", "High", "Low", "Close"):
        complete &= result[column].notna().to_numpy()
    return result.loc[complete]


def aggregate_fast(frame_5m: pd.DataFrame, timeframe: str, end_exclusive) -> pd.DataFrame:
    # All supported buckets divide a UTC day. Calendar-aligned chunks preserve
    # the exact rows and pandas reduction order in every bucket, while limiting
    # resampling/grouping/filter temporary arrays to one month at a time.
    if (timeframe == "5m" or len(frame_5m) < 20000
            or not frame_5m.index.is_monotonic_increasing
            or str(frame_5m.index.tz) != "UTC"):
        return _aggregate_chunk(frame_5m, timeframe, end_exclusive)
    # Canonical history is float64. Write aggregate chunks into their final
    # numeric buffer so concat never holds two complete OHLCV outputs at once.
    numeric = all(dtype == np.dtype("float64") for dtype in frame_5m.dtypes)
    required_count = int(_AGGREGATION[timeframe][1] / pd.Timedelta(minutes=5))
    values = np.empty((len(frame_5m) // required_count + 1, 5), dtype=np.float64) if numeric else None
    pieces = []
    indexes = []
    position = 0
    columns = None
    first = 0
    boundary = frame_5m.index[0].floor("D") + pd.Timedelta(days=31)
    while first < len(frame_5m):
        stop = frame_5m.index.searchsorted(boundary)
        if stop > first:
            piece = _aggregate_chunk(frame_5m.iloc[first:stop], timeframe, end_exclusive)
            if numeric:
                values[position:position + len(piece)] = piece.to_numpy(copy=False)
                position += len(piece)
                indexes.append(piece.index)
                columns = piece.columns
                del piece
            else:
                pieces.append(piece)
        first = stop
        boundary += pd.Timedelta(days=31)
    if not numeric:
        return pd.concat(pieces)
    values.resize((position, 5), refcheck=False)
    index = indexes[0].append(indexes[1:])
    return pd.DataFrame(values, index=index, columns=columns, copy=False)


def build_fast_market_bundle(frame_5m: pd.DataFrame, timeframes, end_exclusive) -> dict[str, pd.DataFrame]:
    """Build only requested timeframes; reuse canonical history when unfiltered."""
    bundle = {}
    cutoff = _utc(end_exclusive)
    for timeframe in dict.fromkeys(timeframes):
        if timeframe == "5m" and (frame_5m.empty or frame_5m.index.max() < cutoff):
            bundle[timeframe] = frame_5m
        else:
            bundle[timeframe] = aggregate_fast(frame_5m, timeframe, cutoff)
    return bundle
