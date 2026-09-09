from datetime import datetime, timezone

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import api
import ctrader_connector as ctrader
import services
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


def _provider_cache_frame(times, closes):
    index = pd.DatetimeIndex(pd.to_datetime(times, utc=True), name="Datetime")
    return pd.DataFrame(
        {
            "Open": closes,
            "High": closes,
            "Low": closes,
            "Close": closes,
            "Volume": [1] * len(closes),
        },
        index=index,
    )


def _prepare_provider_cache_guard(monkeypatch, fake_get):
    monkeypatch.setattr(ctrader, "get_ctrader_market_data", fake_get)
    monkeypatch.setattr(
        ctrader,
        "_PROVIDER_ONLY_CANDLE_CACHE_GUARD_INSTALLED",
        False,
        raising=False,
    )
    monkeypatch.setattr(
        ctrader,
        "_PROVIDER_ONLY_CANDLE_CACHE_ORIGINAL_GET",
        None,
        raising=False,
    )
    monkeypatch.setattr(api, "get_ctrader_market_data", fake_get)
    assert services._install_ctrader_provider_cache_guard() is True
    assert api.get_ctrader_market_data is ctrader.get_ctrader_market_data


def test_cache_hit_returns_synthetic_candle_without_persisting_it(monkeypatch):
    provider = _provider_cache_frame(["2026-09-09T19:15:00Z"], [1.1627])
    synthetic_time = pd.Timestamp("2026-09-09T19:30:00Z")
    fetched_at = datetime(2026, 9, 9, 19, 35, tzinfo=timezone.utc)
    cache_key = ctrader.get_ctrader_candle_cache_key("EURUSD", "15m")
    monkeypatch.setitem(
        ctrader.CTRADER_CANDLE_CACHE,
        cache_key,
        {
            "data": provider.copy(deep=True),
            "fetched_at": fetched_at,
            "source": "ctrader_cache",
            "symbol": "EURUSD",
            "timeframe": "15m",
        },
    )

    def legacy_cache_hit(symbol, timeframe, *args, **kwargs):
        cached = ctrader.CTRADER_CANDLE_CACHE[cache_key]
        synthetic = cached["data"].copy(deep=True)
        synthetic.loc[synthetic_time] = {
            "Open": 1.1628,
            "High": 1.1629,
            "Low": 1.1628,
            "Close": 1.1629,
            "Volume": 1,
        }
        cached["data"] = synthetic.copy(deep=True)
        return synthetic

    _prepare_provider_cache_guard(monkeypatch, legacy_cache_hit)
    returned = api.get_ctrader_market_data("EURUSD", "15m")

    assert list(returned.index) == [provider.index[0], synthetic_time]
    cached_after = ctrader.CTRADER_CANDLE_CACHE[cache_key]["data"]
    assert list(cached_after.index) == [provider.index[0]]
    assert float(cached_after.iloc[-1]["Close"]) == 1.1627


def test_real_provider_refresh_replaces_cache_and_is_not_rolled_back(monkeypatch):
    old_provider = _provider_cache_frame(["2026-09-09T19:15:00Z"], [4399.5])
    refreshed_provider = _provider_cache_frame(
        ["2026-09-09T19:15:00Z", "2026-09-09T19:30:00Z"],
        [4399.5, 4397.2],
    )
    synthetic_time = pd.Timestamp("2026-09-09T19:45:00Z")
    old_fetched_at = datetime(2026, 9, 9, 19, 35, tzinfo=timezone.utc)
    new_fetched_at = datetime(2026, 9, 9, 19, 50, tzinfo=timezone.utc)
    cache_key = ctrader.get_ctrader_candle_cache_key("XAUUSD", "15m")
    monkeypatch.setitem(
        ctrader.CTRADER_CANDLE_CACHE,
        cache_key,
        {
            "data": old_provider.copy(deep=True),
            "fetched_at": old_fetched_at,
            "source": "ctrader_cache",
            "symbol": "XAUUSD",
            "timeframe": "15m",
        },
    )

    def real_refresh(symbol, timeframe, *args, **kwargs):
        cached = ctrader.CTRADER_CANDLE_CACHE[cache_key]
        cached["data"] = refreshed_provider.copy(deep=True)
        cached["fetched_at"] = new_fetched_at
        cached["source"] = "ctrader"
        returned = refreshed_provider.copy(deep=True)
        returned.loc[synthetic_time] = {
            "Open": 4397.2,
            "High": 4398.0,
            "Low": 4396.8,
            "Close": 4397.8,
            "Volume": 1,
        }
        return returned

    _prepare_provider_cache_guard(monkeypatch, real_refresh)
    returned = ctrader.get_ctrader_market_data(
        "XAUUSD", "15m", force_refresh=True
    )

    assert list(returned.index) == [
        old_provider.index[0],
        pd.Timestamp("2026-09-09T19:30:00Z"),
        synthetic_time,
    ]
    cached_after = ctrader.CTRADER_CANDLE_CACHE[cache_key]["data"]
    assert list(cached_after.index) == list(refreshed_provider.index)
    assert float(cached_after.iloc[-1]["Close"]) == 4397.2
    assert ctrader.CTRADER_CANDLE_CACHE[cache_key]["fetched_at"] == new_fetched_at
