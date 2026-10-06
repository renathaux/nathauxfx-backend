"""Shared closed-bucket aggregation. No IO or historical simulation."""
import pandas as pd
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
