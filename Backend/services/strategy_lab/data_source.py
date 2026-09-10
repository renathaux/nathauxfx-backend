"""Read-only access to immutable indicator candles."""
from __future__ import annotations

from datetime import timedelta

import pandas as pd

from db import SessionLocal
from models import IndicatorCandle


def load_candles(symbol, timeframe, start, end, *, session_factory=SessionLocal):
    # Warm-up is needed for EMA, ATR and SMC structure. It remains historical
    # context only; results are still constrained to the requested range.
    warmup_start = start - timedelta(days=45)
    with session_factory() as session:
        rows = (
            session.query(IndicatorCandle)
            .filter(
                IndicatorCandle.symbol == symbol,
                IndicatorCandle.timeframe == timeframe,
                IndicatorCandle.candle_timestamp >= warmup_start,
                IndicatorCandle.candle_timestamp <= end,
            )
            .order_by(IndicatorCandle.candle_timestamp.asc())
            .all()
        )
    index = pd.to_datetime([row.candle_timestamp for row in rows], utc=True)
    return pd.DataFrame(
        {
            "Open": [float(row.open_price) for row in rows],
            "High": [float(row.high_price) for row in rows],
            "Low": [float(row.low_price) for row in rows],
            "Close": [float(row.close_price) for row in rows],
        },
        index=index,
    )
