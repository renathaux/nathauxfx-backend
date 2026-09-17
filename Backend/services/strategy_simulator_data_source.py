"""Read-only, account-scoped candle source for Strategy Studio simulation.

The simulator intentionally reads the durable closed 5-minute indicator candles
already stored for the selected cTrader account. Higher timeframes are derived
locally and deterministically. This module has no broker execution or LIVE state
mutation responsibilities.
"""
from __future__ import annotations

from contextlib import closing

import pandas as pd

from db import SessionLocal
from models import IndicatorCandle
from services.indicator_stream_account_scope import storage_symbol_for_scope


_BASE_TIMEFRAME = "5m"
_AGGREGATION_RULES = {
    "15m": ("15min", pd.Timedelta(minutes=15)),
    "1h": ("1h", pd.Timedelta(hours=1)),
    "4h": ("4h", pd.Timedelta(hours=4)),
}
_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]


def _utc_timestamp(value) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        return timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


def _empty_frame() -> pd.DataFrame:
    frame = pd.DataFrame(columns=_COLUMNS)
    frame.index = pd.DatetimeIndex([], tz="UTC")
    return frame


def load_simulation_5m(
    symbol,
    start,
    end,
    *,
    stream_scope,
    session_factory=None,
) -> pd.DataFrame:
    """Load exact-account durable closed 5m candles in ``[start, end)``.

    ``IndicatorCandle`` predates stored broker volume and therefore contains
    OHLC only. Strategy Studio Stage 1 has no volume-based rule, so the simulator
    exposes a neutral ``Volume=0.0`` column rather than inventing market volume or
    adding a production schema migration.
    """
    start_utc = _utc_timestamp(start)
    end_utc = _utc_timestamp(end)
    if end_utc <= start_utc:
        raise ValueError("SIMULATION_HISTORY_UNAVAILABLE")

    scope = str(stream_scope or "").strip().upper()
    if not scope:
        raise ValueError("SIMULATION_HISTORY_UNAVAILABLE")

    storage_symbol = storage_symbol_for_scope(symbol, scope)
    Session = session_factory or SessionLocal
    db = Session()
    try:
        rows = (
            db.query(IndicatorCandle)
            .filter(
                IndicatorCandle.symbol == storage_symbol,
                IndicatorCandle.timeframe == _BASE_TIMEFRAME,
                IndicatorCandle.candle_timestamp >= start_utc.to_pydatetime(),
                IndicatorCandle.candle_timestamp < end_utc.to_pydatetime(),
            )
            .order_by(IndicatorCandle.candle_timestamp.asc())
            .all()
        )
    finally:
        db.close()

    if not rows:
        raise ValueError("SIMULATION_HISTORY_UNAVAILABLE")

    index = pd.to_datetime([row.candle_timestamp for row in rows], utc=True)
    frame = pd.DataFrame(
        {
            "Open": [float(row.open_price) for row in rows],
            "High": [float(row.high_price) for row in rows],
            "Low": [float(row.low_price) for row in rows],
            "Close": [float(row.close_price) for row in rows],
            "Volume": [0.0] * len(rows),
        },
        index=index,
    )
    frame.index.name = None
    return frame[_COLUMNS].sort_index()


def aggregate_closed(frame_5m, timeframe, *, end_exclusive) -> pd.DataFrame:
    """Aggregate closed 5m facts into deterministic closed higher-timeframe bars."""
    normalized = str(timeframe or "").strip().lower()
    if normalized not in _AGGREGATION_RULES:
        raise ValueError(f"UNSUPPORTED_SIMULATION_TIMEFRAME:{timeframe}")

    if frame_5m is None or frame_5m.empty:
        return _empty_frame()

    frame = frame_5m.copy().sort_index()
    frame.index = pd.to_datetime(frame.index, utc=True)
    for column in _COLUMNS:
        if column not in frame.columns:
            if column == "Volume":
                frame[column] = 0.0
            else:
                raise ValueError(f"SIMULATION_HISTORY_MISSING_COLUMN:{column}")

    rule, duration = _AGGREGATION_RULES[normalized]
    aggregated = frame[_COLUMNS].resample(
        rule,
        label="left",
        closed="left",
        origin="epoch",
    ).agg(
        {
            "Open": "first",
            "High": "max",
            "Low": "min",
            "Close": "last",
            "Volume": "sum",
        }
    )
    aggregated = aggregated.dropna(subset=["Open", "High", "Low", "Close"])

    cutoff = _utc_timestamp(end_exclusive)
    complete = (aggregated.index + duration) <= cutoff
    aggregated = aggregated.loc[complete]
    aggregated.index.name = None
    return aggregated[_COLUMNS]


def load_market_bundle(
    symbol,
    start,
    end,
    *,
    stream_scope,
    session_factory=None,
) -> dict[str, pd.DataFrame]:
    """Load one account-scoped 5m history and derive all supported timelines."""
    frame_5m = load_simulation_5m(
        symbol,
        start,
        end,
        stream_scope=stream_scope,
        session_factory=session_factory,
    )
    return {
        "5m": frame_5m,
        "15m": aggregate_closed(frame_5m, "15m", end_exclusive=end),
        "1h": aggregate_closed(frame_5m, "1h", end_exclusive=end),
        "4h": aggregate_closed(frame_5m, "4h", end_exclusive=end),
    }
