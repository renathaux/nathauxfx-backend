from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db import Base
from models import IndicatorCandle
from services.indicator_stream_account_scope import storage_symbol_for_scope
from services.strategy_simulator_data_source import (
    aggregate_closed,
    load_market_bundle,
    load_simulation_5m,
)


START = pd.Timestamp("2026-09-17T00:00:00Z")
END = pd.Timestamp("2026-09-17T01:00:00Z")
SCOPE_A = "CTRADER:DEMO:47784297"
SCOPE_B = "CTRADER:DEMO:47810571"


def _session_factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine), engine


def _frame(periods=12):
    index = pd.date_range(START, periods=periods, freq="5min")
    rows = []
    for i, _timestamp in enumerate(index):
        base = 1.1000 + i * 0.0001
        rows.append(
            {
                "Open": base,
                "High": base + 0.0003,
                "Low": base - 0.0002,
                "Close": base + 0.0001,
                "Volume": 0.0,
            }
        )
    return pd.DataFrame(rows, index=index)


def _insert_scope(Session, scope, *, close_shift=0.0):
    storage_symbol = storage_symbol_for_scope("EURUSD", scope)
    frame = _frame()
    db = Session()
    try:
        for timestamp, row in frame.iterrows():
            db.add(
                IndicatorCandle(
                    symbol=storage_symbol,
                    timeframe="5m",
                    candle_timestamp=timestamp.to_pydatetime(),
                    open_price=float(row.Open),
                    high_price=float(row.High),
                    low_price=float(row.Low),
                    close_price=float(row.Close + close_shift),
                    created_at=datetime.now(timezone.utc),
                )
            )
        db.commit()
    finally:
        db.close()


def test_load_uses_storage_symbol_for_exact_scope():
    Session, engine = _session_factory()
    try:
        _insert_scope(Session, SCOPE_A)
        _insert_scope(Session, SCOPE_B, close_shift=0.0005)

        a = load_simulation_5m(
            "EURUSD", START, END, stream_scope=SCOPE_A, session_factory=Session
        )
        b = load_simulation_5m(
            "EURUSD", START, END, stream_scope=SCOPE_B, session_factory=Session
        )

        assert len(a) == len(b) == 12
        assert not a.equals(b)
        assert list(a.columns) == ["Open", "High", "Low", "Close", "Volume"]
        assert (a["Volume"] == 0.0).all()
    finally:
        engine.dispose()


def test_missing_scope_history_is_rejected():
    Session, engine = _session_factory()
    try:
        with pytest.raises(ValueError, match="SIMULATION_HISTORY_UNAVAILABLE"):
            load_simulation_5m(
                "EURUSD", START, END, stream_scope=SCOPE_A, session_factory=Session
            )
    finally:
        engine.dispose()


def test_5m_to_15m_ohlc_is_deterministic():
    frame_5m = _frame(periods=6)
    result = aggregate_closed(
        frame_5m,
        "15m",
        end_exclusive=pd.Timestamp("2026-09-17T00:30:00Z"),
    )

    first = result.iloc[0]
    assert first.Open == frame_5m.iloc[0].Open
    assert first.High == frame_5m.iloc[:3].High.max()
    assert first.Low == frame_5m.iloc[:3].Low.min()
    assert first.Close == frame_5m.iloc[2].Close
    assert first.Volume == 0.0


def test_partial_final_bucket_is_excluded():
    frame_5m = _frame(periods=4)
    result = aggregate_closed(
        frame_5m,
        "15m",
        end_exclusive=pd.Timestamp("2026-09-17T00:20:00Z"),
    )

    assert list(result.index) == [pd.Timestamp("2026-09-17T00:00:00Z")]


def test_market_bundle_derives_only_closed_supported_timeframes():
    Session, engine = _session_factory()
    try:
        _insert_scope(Session, SCOPE_A)
        bundle = load_market_bundle(
            "EURUSD", START, END, stream_scope=SCOPE_A, session_factory=Session
        )
        assert set(bundle) == {"5m", "15m", "1h", "4h"}
        assert len(bundle["5m"]) == 12
        assert len(bundle["15m"]) == 4
        assert len(bundle["1h"]) == 1
        assert bundle["4h"].empty
    finally:
        engine.dispose()
