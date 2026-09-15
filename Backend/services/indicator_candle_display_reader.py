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
)


_VALID_SCOPE = re.compile(r"^CTRADER:(?:DEMO|LIVE):[^:\s]+$")
_TIMEFRAME_MINUTES = {"5m": 5, "15m": 15, "1h": 60}


def _valid_scope(scope):
    value = str(scope or "").strip().upper()
    return value if _VALID_SCOPE.fullmatch(value) else None


def _is_closed(candle_timestamp, minutes, closed_before):
    timestamp = pd.Timestamp(candle_timestamp)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    else:
        timestamp = timestamp.tz_convert("UTC")
    return timestamp + pd.Timedelta(minutes=minutes) <= pd.Timestamp(closed_before)


def load_closed_indicator_candles(
    symbols,
    timeframes,
    *,
    limit=500,
    now=None,
    session_factory=SessionLocal,
    stream_scope=None,
):
    """Return latest closed, account-scoped immutable candles without writes.

    An absent or malformed scope deliberately returns no data.  Display fallback
    must never borrow legacy/unscoped rows from another cTrader account.
    """
    scope = _valid_scope(stream_scope or active_ctrader_stream_scope())
    if not scope:
        return {}

    maximum = max(1, min(int(limit or 500), 500))
    closed_before = now or datetime.now(timezone.utc)
    output = {}
    with session_factory() as session:
        for public_symbol in symbols:
            symbol_output = {}
            storage_symbol = storage_symbol_for_scope(public_symbol, scope)
            for timeframe in timeframes:
                minutes = _TIMEFRAME_MINUTES.get(str(timeframe).lower())
                if minutes is None:
                    continue
                rows = (
                    session.query(IndicatorCandle)
                    .filter(
                        IndicatorCandle.symbol == storage_symbol,
                        IndicatorCandle.timeframe == timeframe,
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
                symbol_output[timeframe] = pd.DataFrame(
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
