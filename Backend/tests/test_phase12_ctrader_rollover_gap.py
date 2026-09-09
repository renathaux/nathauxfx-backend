import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db import Base
from models import IndicatorCandle, IndicatorStreamState
from services import indicator_event_stream_service as stream
from services import paper_live_entry_service as paper_entry


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


def _ctrader_frame(start="2026-09-03T14:00:00Z", periods=8, freq="5min"):
    index = pd.date_range(start, periods=periods, freq=freq)
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


def test_ctrader_provider_sparse_policy_accepts_large_no_tick_holes_without_synthesis():
    Session, engine = _session_factory()
    base = _ctrader_frame(periods=12)
    missing = base.index[[2, 3, 4, 5]]
    data = base.drop(missing)

    assert stream._ctrader_sparse_frame_allowed(data, "EURUSD", "5m") is True
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
        assert all(value not in stored for value in missing)
        assert session.query(IndicatorCandle).count() == len(data)
    finally:
        session.close()
        engine.dispose()


def test_explicit_strict_mode_still_blocks_ctrader_shaped_gap():
    Session, engine = _session_factory()
    base = _ctrader_frame()
    data = base.drop(base.index[[2, 3]])

    with pytest.raises(stream.IndicatorStreamUnavailable, match="missing closed candles"):
        stream.initialize_indicator_stream(
            data,
            "EURUSD",
            "5m",
            0.00001,
            analyzer=_analysis,
            session_factory=Session,
            allow_sparse_trendbars=False,
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


def test_ctrader_15m_waits_one_5m_slot_before_immutable_persistence(monkeypatch):
    Session, engine = _session_factory()
    base = _ctrader_frame(
        start="2026-09-09T15:30:00Z",
        periods=3,
        freq="15min",
    )

    monkeypatch.setattr(
        stream,
        "_ctrader_now",
        lambda: pd.Timestamp("2026-09-09T16:19:59Z"),
    )
    first = stream.initialize_indicator_stream(
        base,
        "EURUSD",
        "15m",
        0.00001,
        analyzer=_analysis,
        session_factory=Session,
    )
    assert first["stream_status"] == "READY"
    assert first["canonical_candle_count"] == 2

    revised = base.copy()
    revised.loc[pd.Timestamp("2026-09-09T16:00:00Z"), "Close"] = 1.1008
    monkeypatch.setattr(
        stream,
        "_ctrader_now",
        lambda: pd.Timestamp("2026-09-09T16:20:01Z"),
    )
    second = stream.get_authoritative_structure(
        revised,
        "EURUSD",
        "15m",
        0.00001,
        analyzer=_analysis,
        session_factory=Session,
    )
    assert second["stream_status"] == "READY"
    assert second["canonical_candle_count"] == 3

    session = Session()
    try:
        stored = session.query(IndicatorCandle).filter(
            IndicatorCandle.symbol == "EURUSD",
            IndicatorCandle.timeframe == "15m",
            IndicatorCandle.candle_timestamp == pd.Timestamp(
                "2026-09-09T16:00:00Z"
            ).to_pydatetime(),
        ).one()
        assert stored.close_price == pytest.approx(1.1008)
    finally:
        session.close()
        engine.dispose()


def test_paper_entry_legacy_wait_shapes_fail_closed_without_exception():
    result = paper_entry.build_paper_entry_result(
        "EURUSD",
        {
            "signal": "WAIT",
            "strategy_setup_complete": False,
            "fifteen_m_swing_break": "WAIT",
            "confirmation_5m": "WAIT",
            "blocked_reason": "WAIT_INDICATOR_EVENT_STREAM_UNAVAILABLE",
        },
        None,
        None,
        strict_trader_module=None,
    )

    assert result["signal"] == "WAIT"
    assert result["paper_entry_ready"] is False
    assert result["paper_entry_reason"] == "WAIT_AUTHORITATIVE_INDICATOR_EVENT"
