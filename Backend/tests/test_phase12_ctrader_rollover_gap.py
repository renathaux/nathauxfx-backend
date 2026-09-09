import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db import Base
from models import IndicatorCandle, IndicatorStreamState
from services import indicator_event_stream_service as stream


def _session_factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine), engine


def _analysis(_source, **_kwargs):
    return {
        "bias": "NEUTRAL",
        "events": [],
        "current_structure": {"bias": "NEUTRAL"},
        "swings": [],
        "fib_levels": [],
    }


def _ctrader_frame(start="2026-09-03T14:00:00Z", periods=8):
    index = pd.date_range(start, periods=periods, freq="5min")
    return pd.DataFrame(
        {
            "Open": [1.1000] * periods,
            "High": [1.1010] * periods,
            "Low": [1.0990] * periods,
            "Close": [1.1005] * periods,
            "Volume": [10] * periods,
        },
        index=index,
    )


def test_ctrader_sparse_no_tick_bars_are_accepted_without_synthetic_ohlc():
    Session, engine = _session_factory()
    base = _ctrader_frame()
    missing = base.index[[2, 3]]
    data = base.drop(missing)

    result = stream.initialize_indicator_stream(
        data,
        "EURUSD",
        "5m",
        0.00001,
        analyzer=_analysis,
        session_factory=Session,
    )

    assert result["stream_status"] == "READY"
    assert result["allow_sparse_trendbars"] is True
    assert result["canonical_candle_count"] == len(data)

    session = Session()
    try:
        stored = {
            pd.Timestamp(row.candle_timestamp, tz="UTC")
            if pd.Timestamp(row.candle_timestamp).tzinfo is None
            else pd.Timestamp(row.candle_timestamp).tz_convert("UTC")
            for row in session.query(IndicatorCandle).all()
        }
        assert missing[0] not in stored
        assert missing[1] not in stored
        assert session.query(IndicatorCandle).count() == len(data)
    finally:
        session.close()
        engine.dispose()


def test_non_ctrader_frame_keeps_strict_gap_blocking():
    Session, engine = _session_factory()
    base = _ctrader_frame()
    data = base.drop(base.index[[2, 3]]).drop(columns=["Volume"])

    with pytest.raises(stream.IndicatorStreamUnavailable, match="missing closed candles"):
        stream.initialize_indicator_stream(
            data,
            "EURUSD",
            "5m",
            0.00001,
            analyzer=_analysis,
            session_factory=Session,
        )

    session = Session()
    try:
        state = session.query(IndicatorStreamState).one()
        assert state.status == "GAP_BLOCKED"
        assert state.last_processed_candle is None
    finally:
        session.close()
        engine.dispose()


def test_ctrader_sparse_policy_still_blocks_large_unexplained_holes():
    Session, engine = _session_factory()
    base = _ctrader_frame(periods=12)
    data = base.drop(base.index[[2, 3, 4, 5]])

    assert stream._ctrader_sparse_frame_allowed(data, "EURUSD", "5m") is False
    with pytest.raises(stream.IndicatorStreamUnavailable, match="missing closed candles"):
        stream.initialize_indicator_stream(
            data,
            "EURUSD",
            "5m",
            0.00001,
            analyzer=_analysis,
            session_factory=Session,
        )
    engine.dispose()


def test_sparse_policy_does_not_bypass_conflicting_duplicate_candles():
    Session, engine = _session_factory()
    data = _ctrader_frame(periods=4)
    duplicate = data.iloc[[1]].copy()
    duplicate.iloc[0, duplicate.columns.get_loc("Close")] += 0.0002
    conflicting = pd.concat([data, duplicate]).sort_index()

    with pytest.raises(stream.IncomingCandleConflict, match="conflicting incoming closed candles"):
        stream.initialize_indicator_stream(
            conflicting,
            "EURUSD",
            "5m",
            0.00001,
            analyzer=_analysis,
            session_factory=Session,
        )
    engine.dispose()
