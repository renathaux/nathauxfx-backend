"""Read-only historical candle source for Strategy Studio simulation."""
from __future__ import annotations

import pandas as pd

from db import SessionLocal
from models import IndicatorCandle
from services.indicator_stream_account_scope import storage_symbol_for_scope


_AGGREGATION = {
    "15m": ("15min", pd.Timedelta(minutes=15)),
    "1h": ("1h", pd.Timedelta(hours=1)),
    "4h": ("4h", pd.Timedelta(hours=4)),
}
_BASE_INTERVAL = pd.Timedelta(minutes=5)


def _utc(value) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        return stamp.tz_localize("UTC")
    return stamp.tz_convert("UTC")


def _factory(session_factory=None):
    return session_factory or SessionLocal


def _canonical_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or not isinstance(frame, pd.DataFrame):
        raise ValueError("SIMULATION_HISTORY_UNAVAILABLE")
    required = {"Open", "High", "Low", "Close"}
    if not required.issubset(frame.columns):
        missing = ", ".join(sorted(required - set(frame.columns)))
        raise ValueError(f"SIMULATION_HISTORY_INVALID: missing {missing}")
    data = frame.copy().sort_index()
    data.index = pd.to_datetime(data.index, utc=True)
    data = data[~data.index.duplicated(keep="last")]
    data = data.dropna(subset=["Open", "High", "Low", "Close"])
    if "Volume" not in data.columns:
        # The durable IndicatorCandle schema intentionally stores OHLC only.
        # Volume is not used by the shared evaluator; a deterministic zero keeps
        # resampling shape stable without altering the production schema.
        data["Volume"] = 0.0
    return data[["Open", "High", "Low", "Close", "Volume"]]


def load_simulation_5m(symbol: str, start, end, *, stream_scope: str, session_factory=None) -> pd.DataFrame:
    public_symbol = str(symbol or "").upper().replace("/", "")
    scope = str(stream_scope or "").strip().upper()
    if public_symbol not in {"EURUSD", "XAUUSD"} or not scope:
        raise ValueError("SIMULATION_HISTORY_UNAVAILABLE")
    start_utc = _utc(start)
    end_utc = _utc(end)
    if end_utc <= start_utc:
        raise ValueError("SIMULATION_RANGE_INVALID")

    storage_symbol = storage_symbol_for_scope(public_symbol, scope)
    factory = _factory(session_factory)
    with factory() as session:
        rows = (
            session.query(IndicatorCandle)
            .filter(
                IndicatorCandle.symbol == storage_symbol,
                IndicatorCandle.timeframe == "5m",
                IndicatorCandle.candle_timestamp >= start_utc.to_pydatetime(),
                IndicatorCandle.candle_timestamp < end_utc.to_pydatetime(),
            )
            .order_by(IndicatorCandle.candle_timestamp.asc())
            .all()
        )

    if not rows:
        raise ValueError("SIMULATION_HISTORY_UNAVAILABLE")

    frame = pd.DataFrame(
        {
            "Open": [float(row.open_price) for row in rows],
            "High": [float(row.high_price) for row in rows],
            "Low": [float(row.low_price) for row in rows],
            "Close": [float(row.close_price) for row in rows],
            "Volume": [0.0 for _ in rows],
        },
        index=pd.DatetimeIndex([_utc(row.candle_timestamp) for row in rows]),
    )
    frame.attrs.update({
        "public_symbol": public_symbol,
        "stream_scope": scope,
        "storage_symbol": storage_symbol,
    })
    return _canonical_frame(frame)


def _complete_bucket_starts(data: pd.DataFrame, duration: pd.Timedelta, cutoff: pd.Timestamp) -> list[pd.Timestamp]:
    required_count = int(duration / _BASE_INTERVAL)
    available = set(data.index)
    starts = []
    for bucket_start in data.index.floor(duration).unique():
        bucket_start = _utc(bucket_start)
        if bucket_start + duration > cutoff:
            continue
        expected = [bucket_start + index * _BASE_INTERVAL for index in range(required_count)]
        if all(timestamp in available for timestamp in expected):
            starts.append(bucket_start)
    return sorted(starts)


def aggregate_closed(frame_5m: pd.DataFrame, timeframe: str, *, end_exclusive) -> pd.DataFrame:
    data = _canonical_frame(frame_5m)
    cutoff = _utc(end_exclusive)
    tf = str(timeframe or "").strip().lower()
    if tf == "5m":
        return data.loc[data.index < cutoff].copy()
    if tf not in _AGGREGATION:
        raise ValueError("SIMULATION_TIMEFRAME_UNSUPPORTED")

    rule, duration = _AGGREGATION[tf]
    result = (
        data.resample(rule, label="left", closed="left", origin="epoch")
        .agg({
            "Open": "first",
            "High": "max",
            "Low": "min",
            "Close": "last",
            "Volume": "sum",
        })
        .dropna(subset=["Open", "High", "Low", "Close"])
    )
    complete_starts = _complete_bucket_starts(data, duration, cutoff)
    return result.loc[result.index.isin(complete_starts)]


def load_market_bundle(symbol: str, start, end, *, stream_scope: str, session_factory=None) -> dict[str, pd.DataFrame]:
    end_utc = _utc(end)
    frame_5m = load_simulation_5m(
        symbol,
        start,
        end,
        stream_scope=stream_scope,
        session_factory=session_factory,
    )
    return {
        "5m": frame_5m,
        "15m": aggregate_closed(frame_5m, "15m", end_exclusive=end_utc),
        "1h": aggregate_closed(frame_5m, "1h", end_exclusive=end_utc),
        "4h": aggregate_closed(frame_5m, "4h", end_exclusive=end_utc),
    }
