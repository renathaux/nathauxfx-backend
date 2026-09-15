import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db import Base
from models import IndicatorCandle, IndicatorStreamState
from services import indicator_event_stream_service as stream


def make_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine), engine


def frame(start, count):
    index = pd.date_range(start, periods=count, freq="5min")
    return pd.DataFrame(
        {
            "Open": [4300.0] * count,
            "High": [4301.0] * count,
            "Low": [4299.0] * count,
            "Close": [4300.5] * count,
        },
        index=index,
    )


def event(data, index=1):
    return {
        "event_type": "BOS",
        "direction": "BULLISH",
        "timestamp": data.index[index].isoformat(),
        "close": 4300.5,
        "broken_swing_timestamp": data.index[max(0, index - 1)].isoformat(),
        "broken_level": 4300.0,
        "structure_start_index": max(0, index - 1),
        "break_index": index,
        "event_invalidation_swing": {
            "type": "LOW",
            "price": 4299.0,
            "swing_time": data.index[max(0, index - 1)].isoformat(),
            "source": "LEGACY_CURRENT_STRUCTURE",
        },
    }


def analyzer(data, **_kwargs):
    return {
        "bias": "BULLISH",
        "events": [event(data)],
        "current_structure": {"bias": "BULLISH"},
        "swings": [],
        "fib_levels": [],
    }


def test_restart_history_before_durable_origin_cannot_expand_stream_backwards():
    Session, engine = make_session()
    storage_key = "XAUUSD~93C0AAE3E8"
    initial = frame("2026-09-13T22:40:00Z", 6)

    first = stream.initialize_indicator_stream(
        initial,
        storage_key,
        "5m",
        0.01,
        analyzer=analyzer,
        session_factory=Session,
    )
    original_event_id = first["events"][0]["event_id"]

    restart_history = pd.concat(
        [
            frame("2026-09-13T22:20:00Z", 4),
            initial,
            frame("2026-09-13T23:10:00Z", 1),
        ]
    ).sort_index()

    restarted = stream.get_authoritative_structure(
        restart_history,
        storage_key,
        "5m",
        0.01,
        analyzer=analyzer,
        session_factory=Session,
    )

    assert restarted["stream_status"] == "READY"
    assert restarted["events"][0]["event_id"] == original_event_id

    session = Session()
    try:
        state = session.query(IndicatorStreamState).filter_by(
            symbol=storage_key,
            timeframe="5m",
        ).one()
        candles = session.query(IndicatorCandle).filter_by(
            symbol=storage_key,
            timeframe="5m",
        ).order_by(IndicatorCandle.candle_timestamp.asc()).all()
        assert pd.Timestamp(state.origin_candle, tz="UTC") == initial.index[0]
        assert pd.Timestamp(candles[0].candle_timestamp, tz="UTC") == initial.index[0]
        assert len(candles) == 7
    finally:
        session.close()
        engine.dispose()
