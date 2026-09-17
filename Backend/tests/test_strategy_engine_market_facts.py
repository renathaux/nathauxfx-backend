from datetime import datetime, timezone

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db import Base
from models import IndicatorCandle
from services.indicator_stream_account_scope import storage_symbol_for_scope
from services.strategy_simulator_data_source import aggregate_closed, load_simulation_5m
from services.strategy_engine.market_facts import build_market_facts


def _frame(rows, start="2026-09-17T00:00:00Z", freq="5min"):
    index = pd.date_range(start=start, periods=len(rows), freq=freq, tz="UTC")
    return pd.DataFrame(rows, index=index, columns=["Open", "High", "Low", "Close"])


def _session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def test_load_uses_storage_symbol_for_exact_scope():
    factory = _session_factory()
    start = pd.Timestamp("2026-09-17T00:00:00Z")
    end = pd.Timestamp("2026-09-17T00:10:00Z")
    scope_a = "CTRADER:DEMO:47784297"
    scope_b = "CTRADER:DEMO:47810571"
    with factory() as session:
        for scope, close in ((scope_a, 1.1), (scope_b, 1.2)):
            session.add(IndicatorCandle(
                symbol=storage_symbol_for_scope("EURUSD", scope),
                timeframe="5m",
                candle_timestamp=start.to_pydatetime(),
                open_price=close - 0.001,
                high_price=close + 0.001,
                low_price=close - 0.002,
                close_price=close,
                created_at=datetime.now(timezone.utc),
            ))
        session.commit()

    a = load_simulation_5m("EURUSD", start, end, stream_scope=scope_a, session_factory=factory)
    b = load_simulation_5m("EURUSD", start, end, stream_scope=scope_b, session_factory=factory)
    assert a.iloc[0].Close == pytest.approx(1.1)
    assert b.iloc[0].Close == pytest.approx(1.2)
    assert not a.equals(b)


def test_5m_to_15m_ohlc_is_deterministic():
    frame = _frame([
        (1.00, 1.03, 0.99, 1.02),
        (1.02, 1.05, 1.01, 1.04),
        (1.04, 1.06, 1.03, 1.05),
        (1.05, 1.07, 1.04, 1.06),
    ])
    result = aggregate_closed(frame, "15m", end_exclusive=pd.Timestamp("2026-09-17T00:20:00Z"))
    assert list(result.index) == [pd.Timestamp("2026-09-17T00:00:00Z")]
    first = result.iloc[0]
    assert first.Open == pytest.approx(1.00)
    assert first.High == pytest.approx(1.06)
    assert first.Low == pytest.approx(0.99)
    assert first.Close == pytest.approx(1.05)


def test_partial_final_bucket_is_excluded():
    frame = _frame([
        (1.00, 1.03, 0.99, 1.02),
        (1.02, 1.05, 1.01, 1.04),
        (1.04, 1.06, 1.03, 1.05),
        (1.05, 1.07, 1.04, 1.06),
    ])
    result = aggregate_closed(frame, "15m", end_exclusive=pd.Timestamp("2026-09-17T00:20:00Z"))
    assert pd.Timestamp("2026-09-17T00:15:00Z") not in result.index


def test_market_facts_exposes_bos_or_choch_and_trend_values():
    # This fixture deliberately produces enough pivots and a later breakout for
    # the existing SMC engine.  The assertion is on the shared facts contract,
    # not on a reimplementation of SMC.
    closes = [1.00, 1.02, 1.04, 1.01, 0.99, 1.01, 1.05, 1.03, 1.02, 1.07, 1.08, 1.10]
    rows = []
    for close in closes:
        rows.append((close - 0.005, close + 0.01, close - 0.01, close))
    frame = _frame(rows)
    bundle = {
        "5m": frame,
        "15m": aggregate_closed(frame, "15m", end_exclusive=frame.index[-1] + pd.Timedelta(minutes=5)),
        "1h": aggregate_closed(frame, "1h", end_exclusive=frame.index[-1] + pd.Timedelta(minutes=5)),
        "4h": aggregate_closed(frame, "4h", end_exclusive=frame.index[-1] + pd.Timedelta(minutes=5)),
    }
    timeline = build_market_facts(bundle, "EURUSD", "5m", None)
    assert timeline.timestamps()
    last = timeline.candle(frame.index[-1])
    assert last is not None
    assert last.body_percent >= 0
    trend = timeline.trend(frame.index[-1])
    assert trend.ema50_direction in {"BUY", "SELL", None}
    events = [timeline.structure_event(ts) for ts in timeline.timestamps()]
    events = [event for event in events if event is not None]
    for event in events:
        assert event.direction in {"BUY", "SELL"}
        assert event.event_type in {"BOS", "CHOCH"}
