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
from services.strategy_engine import market_facts as market_facts_module
from services.strategy_engine.market_facts import build_market_facts


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


def _frame(periods=12, *, start=START, freq="5min", slope=0.0001):
    index = pd.date_range(start, periods=periods, freq=freq)
    rows = []
    for i, _timestamp in enumerate(index):
        base = 1.1000 + i * slope
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


def _fake_analysis(frame, *, timeframe=None, point_size=None, **_kwargs):
    if timeframe == "5m":
        trigger_time = frame.index[-1]
        return {
            "bias": "BULLISH",
            "events": [
                {
                    "timestamp": trigger_time.isoformat(),
                    "direction": "BULLISH",
                    "event_type": "BOS",
                    "broken_level": 1.1010,
                    "event_invalidation_swing": {"price": 1.0990},
                }
            ],
            "swings": [],
            "current_structure": {"bias": "BULLISH"},
        }

    times = list(frame.index)
    swing_rows = [
        ("HIGH", 0, 1.1000),
        ("LOW", 0, 0.9000),
        ("HIGH", 1, 1.2000),
        ("LOW", 1, 1.0000),
        ("HIGH", 3, 1.1000),
        ("LOW", 3, 0.8000),
        ("HIGH", 5, 1.3000),
        ("LOW", 5, 0.7000),
    ]
    swings = [
        {
            "type": swing_type,
            "price": price,
            "confirmed_timestamp": times[index].isoformat(),
            "timestamp": times[index].isoformat(),
        }
        for swing_type, index, price in swing_rows
    ]
    return {
        "bias": "BULLISH",
        "events": [],
        "swings": swings,
        "current_structure": {"bias": "BULLISH"},
    }


def _facts_bundle():
    trading = _frame(periods=24, start=pd.Timestamp("2026-09-17T00:00:00Z"), freq="5min")
    trend = _frame(periods=8, start=pd.Timestamp("2026-09-17T00:00:00Z"), freq="15min", slope=0.001)
    return {"5m": trading, "15m": trend, "1h": pd.DataFrame(), "4h": pd.DataFrame()}


def test_bos_and_choch_are_exposed_as_one_directional_trigger(monkeypatch):
    monkeypatch.setattr(market_facts_module, "analyze_structure", _fake_analysis)
    bundle = _facts_bundle()
    timeline = build_market_facts(bundle, "EURUSD", "5m", "15m")
    event_time = bundle["5m"].index[-1]

    event = timeline.structure_event(event_time)

    assert event.direction == "BUY"
    assert event.event_type == "BOS"
    assert event.broken_level == 1.1010
    assert event.invalidation_price == 1.0990


def test_ema_direction_is_close_relative_to_ema(monkeypatch):
    monkeypatch.setattr(market_facts_module, "analyze_structure", _fake_analysis)
    bundle = _facts_bundle()
    timeline = build_market_facts(bundle, "EURUSD", "5m", "15m")

    trend = timeline.trend(bundle["15m"].index[-1])

    assert trend.ema50_direction == "BUY"
    assert trend.ema200_direction == "BUY"


def test_swing_structure_requires_hh_hl_or_lh_ll(monkeypatch):
    monkeypatch.setattr(market_facts_module, "analyze_structure", _fake_analysis)
    bundle = _facts_bundle()
    timeline = build_market_facts(bundle, "EURUSD", "5m", "15m")
    times = bundle["15m"].index

    assert timeline.trend(times[1]).swing_structure_direction == "BUY"
    assert timeline.trend(times[3]).swing_structure_direction == "SELL"
    assert timeline.trend(times[5]).swing_structure_direction is None


def test_candle_facts_body_percent_is_body_over_full_range(monkeypatch):
    monkeypatch.setattr(market_facts_module, "analyze_structure", _fake_analysis)
    bundle = _facts_bundle()
    timeline = build_market_facts(bundle, "EURUSD", "5m", "15m")
    timestamp = bundle["5m"].index[0]

    facts = timeline.candle(timestamp)
    expected = abs(facts.close - facts.open) / (facts.high - facts.low) * 100.0
    assert facts.body_percent == pytest.approx(expected)
