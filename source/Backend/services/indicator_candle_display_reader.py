"""Read-only closed-candle snapshots for the fenced dashboard display."""
from __future__ import annotations

import re
from datetime import datetime, timezone

import pandas as pd

from db import SessionLocal
from models import IndicatorCandle
from services.indicator_stream_account_scope import (
    active_ctrader_stream_scope,
    storage_symbol_for_scope,
    active_storage_symbol,
)


_VALID_SCOPE = re.compile(r"^CTRADER:(?:DEMO|LIVE):[^:\s]+$")
_TIMEFRAME_MINUTES = {"5m": 5, "15m": 15, "1h": 60}
_MEMORY_MAX_AGE_SECONDS = {"5m": 15 * 60, "15m": 45 * 60, "1h": 150 * 60}
_MEMORY_SOURCE = "in_memory_ctrader_closed_candles"
_DURABLE_SOURCE = "persisted_ctrader_closed_candles"


def _valid_scope(scope):
    value = str(scope or "").strip().upper()
    return value if _VALID_SCOPE.fullmatch(value) else None


def _utc(value):
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        return timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


def _is_closed(candle_timestamp, minutes, closed_before):
    return _utc(candle_timestamp) + pd.Timedelta(minutes=minutes) <= _utc(closed_before)


def _closed_frame_copy(frame, minutes, closed_before, maximum):
    """Copy, canonicalize, and bound one frame without mutating its owner."""
    if frame is None or not isinstance(frame, pd.DataFrame) or frame.empty:
        return None
    required = {"Open", "High", "Low", "Close"}
    if not required.issubset(frame.columns):
        return None

    data = frame.copy(deep=True)
    data.index = pd.DatetimeIndex([_utc(value) for value in data.index])
    data = data[~data.index.duplicated(keep="last")].sort_index()
    data = data[
        data.index.map(lambda value: _is_closed(value, minutes, closed_before))
    ].tail(maximum).copy(deep=True)
    return data if not data.empty else None


def _latest_iso(frame):
    if frame is None or frame.empty:
        return None
    return _utc(frame.index[-1]).isoformat()


def _closed_frame_age_seconds(frame, closed_before):
    if frame is None or frame.empty:
        return None
    return max(
        0.0,
        (_utc(closed_before) - _utc(frame.index[-1])).total_seconds(),
    )


def load_durable_indicator_candles(
    symbols,
    timeframes,
    *,
    limit=500,
    now=None,
    session_factory=SessionLocal,
    stream_scope=None,
):
    """Return latest closed, account-scoped immutable candles without writes."""
    scope = _valid_scope(stream_scope or active_ctrader_stream_scope())
    if not scope:
        return {}

    maximum = max(1, min(int(limit or 500), 500))
    closed_before = now or datetime.now(timezone.utc)
    output = {}
    with session_factory() as session:
        for public_symbol in symbols:
            symbol_output = {}
            for timeframe in timeframes:
                normalized_timeframe = str(timeframe or "").lower()
                minutes = _TIMEFRAME_MINUTES.get(normalized_timeframe)
                if minutes is None:
                    continue
                storage_symbol = active_storage_symbol(public_symbol, scope, normalized_timeframe, session_factory=session_factory)
                rows = (
                    session.query(IndicatorCandle)
                    .filter(
                        IndicatorCandle.symbol == storage_symbol,
                        IndicatorCandle.timeframe == normalized_timeframe,
                    )
                    .order_by(IndicatorCandle.candle_timestamp.desc(), IndicatorCandle.id.desc())
                    .limit(maximum)
                    .all()
                )
                rows.reverse()
                closed_rows = [
                    row for row in rows
                    if _is_closed(row.candle_timestamp, minutes, closed_before)
                ]
                if not closed_rows:
                    continue
                index = pd.to_datetime(
                    [row.candle_timestamp for row in closed_rows], utc=True
                )
                symbol_output[normalized_timeframe] = pd.DataFrame(
                    {
                        "Open": [float(row.open_price) for row in closed_rows],
                        "High": [float(row.high_price) for row in closed_rows],
                        "Low": [float(row.low_price) for row in closed_rows],
                        "Close": [float(row.close_price) for row in closed_rows],
                    },
                    index=index,
                )
            if symbol_output:
                output[public_symbol] = symbol_output
    return output


def load_dashboard_display_candles(
    symbols,
    timeframes,
    *,
    limit=500,
    now=None,
    session_factory=SessionLocal,
    stream_scope=None,
    candle_cache=None,
    cache_health_reader=None,
):
    """Return display-only candles, preferring usable in-memory cTrader frames.

    No cTrader market-data function is called here. The function snapshots only
    existing process memory, strips forming candles, then validates freshness on
    the remaining CLOSED frame before filling unavailable streams from the
    account-scoped immutable candle table.
    """
    scope = _valid_scope(stream_scope or active_ctrader_stream_scope())
    if not scope:
        return {"frames": {}, "streams": {}}

    import ctrader_connector

    maximum = max(1, min(int(limit or 500), 500))
    closed_before = now or datetime.now(timezone.utc)
    cache = candle_cache if candle_cache is not None else ctrader_connector.CTRADER_CANDLE_CACHE
    health_reader = cache_health_reader or ctrader_connector.get_ctrader_candle_health

    normalized_symbols = [str(symbol or "").upper().replace("/", "") for symbol in symbols]
    normalized_timeframes = [str(timeframe or "").lower() for timeframe in timeframes]
    frames = {}
    streams = {}
    missing = {}

    for public_symbol in normalized_symbols:
        for timeframe in normalized_timeframes:
            minutes = _TIMEFRAME_MINUTES.get(timeframe)
            if minutes is None:
                continue
            # Once cut over, canonical generation bars take precedence over caches.
            from stream_generations import resolve, generation_for_storage
            with session_factory() as selection_session:
                key = resolve(selection_session, storage_symbol_for_scope(public_symbol, scope), timeframe)
                generation = generation_for_storage(selection_session, key, timeframe)
                if generation and generation.generation > 1:
                    missing.setdefault(public_symbol, []).append(timeframe)
                    continue
            cache_key = f"{scope}:{public_symbol}:{timeframe}"
            cached = cache.get(cache_key) if isinstance(cache, dict) else None
            health = health_reader(public_symbol, timeframe) or {}
            if health.get("cache_key") and health["cache_key"] != cache_key:
                health = {}
            frame = None
            closed_age_seconds = None
            max_age_seconds = float(
                health.get("max_recovery_age_seconds")
                or _MEMORY_MAX_AGE_SECONDS[timeframe]
            )
            if isinstance(cached, dict) and bool(health.get("usable")):
                candidate = _closed_frame_copy(
                    cached.get("data"), minutes, closed_before, maximum
                )
                closed_age_seconds = _closed_frame_age_seconds(candidate, closed_before)
                if (
                    candidate is not None
                    and closed_age_seconds is not None
                    and closed_age_seconds <= max_age_seconds
                ):
                    frame = candidate
            if frame is None:
                missing.setdefault(public_symbol, []).append(timeframe)
                continue

            frames.setdefault(public_symbol, {})[timeframe] = frame
            streams.setdefault(public_symbol, {})[timeframe] = {
                "source": _MEMORY_SOURCE,
                "latest_candle_time": _latest_iso(frame),
                "usable": True,
                "last_candle_age_seconds": round(closed_age_seconds, 1),
                "max_recovery_age_seconds": max_age_seconds,
                "recovery_mode": bool(health.get("recovery_mode", False)),
            }

    for public_symbol, missing_timeframes in missing.items():
        durable = load_durable_indicator_candles(
            (public_symbol,),
            tuple(missing_timeframes),
            limit=maximum,
            now=closed_before,
            session_factory=session_factory,
            stream_scope=scope,
        )
        durable_frames = durable.get(public_symbol) or {}
        for timeframe in missing_timeframes:
            frame = durable_frames.get(timeframe)
            if frame is None or frame.empty:
                continue
            frames.setdefault(public_symbol, {})[timeframe] = frame
            streams.setdefault(public_symbol, {})[timeframe] = {
                "source": _DURABLE_SOURCE,
                "latest_candle_time": _latest_iso(frame),
            }

    return {"frames": frames, "streams": streams}


def load_closed_indicator_candles(
    symbols,
    timeframes,
    *,
    limit=500,
    now=None,
    session_factory=SessionLocal,
    stream_scope=None,
):
    """Compatibility entry point used by the dashboard route.

    The route already imports this name. Returning only ``frames`` preserves its
    existing contract while upgrading the source selection to memory-first,
    durable-second without touching strategy or broker paths.
    """
    return load_dashboard_display_candles(
        symbols,
        timeframes,
        limit=limit,
        now=now,
        session_factory=session_factory,
        stream_scope=stream_scope,
    )["frames"]
