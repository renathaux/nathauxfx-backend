from datetime import datetime, timedelta, timezone

import pandas as pd
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db import Base
from models import IndicatorCandle
from services import indicator_candle_display_reader as reader
from services.indicator_stream_account_scope import storage_symbol_for_scope


ACTIVE_SCOPE = "CTRADER:DEMO:47810571"
SYMBOLS = ("EURUSD", "XAUUSD")
TIMEFRAMES = ("5m", "15m", "1h")


def _sessions():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine), engine


def _seed(session, symbol, timeframe, count=3, start=None):
    start = start or datetime(2026, 9, 1, tzinfo=timezone.utc)
    minutes = {"5m": 5, "15m": 15, "1h": 60}[timeframe]
    for position in range(count):
        price = 1.0 + position
        session.add(IndicatorCandle(
            symbol=symbol,
            timeframe=timeframe,
            candle_timestamp=start + timedelta(minutes=minutes * position),
            open_price=price,
            high_price=price + 0.2,
            low_price=price - 0.1,
            close_price=price + 0.1,
            created_at=start,
        ))


def _seed_all_streams(Session, scope=ACTIVE_SCOPE):
    with Session.begin() as session:
        for public_symbol in SYMBOLS:
            storage_symbol = storage_symbol_for_scope(public_symbol, scope)
            for timeframe in TIMEFRAMES:
                _seed(session, storage_symbol, timeframe)


def _memory_frame(start, periods=3, freq="5min", base=1.0):
    index = pd.date_range(start, periods=periods, freq=freq)
    return pd.DataFrame(
        {
            "Open": [base + i for i in range(periods)],
            "High": [base + i + 0.2 for i in range(periods)],
            "Low": [base + i - 0.1 for i in range(periods)],
            "Close": [base + i + 0.1 for i in range(periods)],
            "Volume": [10 + i for i in range(periods)],
        },
        index=index,
    )


def _usable_health(_symbol, timeframe):
    return {
        "usable": True,
        "last_candle_age_seconds": 30.0,
        "max_recovery_age_seconds": {"5m": 15 * 60, "15m": 45 * 60, "1h": 150 * 60}[timeframe],
        "recovery_mode": False,
    }


def test_returns_all_six_active_account_scoped_closed_streams():
    Session, _ = _sessions()
    _seed_all_streams(Session)

    result = reader.load_durable_indicator_candles(
        SYMBOLS,
        TIMEFRAMES,
        stream_scope=ACTIVE_SCOPE,
        session_factory=Session,
        now=datetime(2026, 9, 2, tzinfo=timezone.utc),
    )

    assert {
        (symbol, timeframe)
        for symbol, frames in result.items()
        for timeframe in frames
    } == {(symbol, timeframe) for symbol in SYMBOLS for timeframe in TIMEFRAMES}
    assert all(
        len(result[symbol][timeframe]) == 3
        for symbol in SYMBOLS
        for timeframe in TIMEFRAMES
    )


def test_resolves_active_scope_through_existing_account_scope_logic(monkeypatch):
    Session, _ = _sessions()
    _seed_all_streams(Session)
    monkeypatch.setattr(reader, "active_ctrader_stream_scope", lambda: ACTIVE_SCOPE)

    result = reader.load_durable_indicator_candles(
        SYMBOLS,
        TIMEFRAMES,
        session_factory=Session,
        now=datetime(2026, 9, 2, tzinfo=timezone.utc),
    )

    assert set(result) == set(SYMBOLS)


def test_another_account_scope_cannot_leak_into_active_display():
    Session, _ = _sessions()
    _seed_all_streams(Session, "CTRADER:DEMO:99999999")

    result = reader.load_durable_indicator_candles(
        SYMBOLS,
        TIMEFRAMES,
        stream_scope=ACTIVE_SCOPE,
        session_factory=Session,
        now=datetime(2026, 9, 2, tzinfo=timezone.utc),
    )

    assert result == {}


def test_missing_or_invalid_scope_never_uses_legacy_unscoped_rows(monkeypatch):
    Session, _ = _sessions()
    with Session.begin() as session:
        _seed(session, "EURUSD", "5m")

    monkeypatch.setattr(reader, "active_ctrader_stream_scope", lambda: None)
    for scope in (None, "CTRADER:INVALID:47810571", "CTRADER:DEMO:"):
        assert reader.load_durable_indicator_candles(
            SYMBOLS,
            TIMEFRAMES,
            stream_scope=scope,
            session_factory=Session,
            now=datetime(2026, 9, 2, tzinfo=timezone.utc),
        ) == {}


def test_durable_reader_limits_to_latest_500_and_returns_ascending():
    Session, _ = _sessions()
    storage_symbol = storage_symbol_for_scope("EURUSD", ACTIVE_SCOPE)
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    with Session.begin() as session:
        _seed(session, storage_symbol, "5m", count=502, start=start)

    frame = reader.load_durable_indicator_candles(
        ("EURUSD",),
        ("5m",),
        stream_scope=ACTIVE_SCOPE,
        session_factory=Session,
        now=datetime(2026, 9, 4, tzinfo=timezone.utc),
    )["EURUSD"]["5m"]

    assert len(frame) == 500
    assert frame.index.is_monotonic_increasing
    assert frame.index[0] == start + timedelta(minutes=10)
    assert frame.index[-1] == start + timedelta(minutes=5 * 501)


def test_durable_reader_issues_selects_only_and_never_writes():
    Session, engine = _sessions()
    _seed_all_streams(Session)
    statements = []
    event.listen(
        engine,
        "before_cursor_execute",
        lambda _conn, _cursor, statement, *_args: statements.append(statement.strip().upper()),
    )

    reader.load_durable_indicator_candles(
        SYMBOLS,
        TIMEFRAMES,
        stream_scope=ACTIVE_SCOPE,
        session_factory=Session,
        now=datetime(2026, 9, 2, tzinfo=timezone.utc),
    )

    assert statements
    assert all(statement.startswith("SELECT") for statement in statements)


def test_display_snapshot_prefers_usable_memory_removes_forming_and_preserves_cache():
    Session, _ = _sessions()
    original = _memory_frame("2026-09-15T12:00:00Z", periods=4, freq="5min")
    before = original.copy(deep=True)

    result = reader.load_dashboard_display_candles(
        ("EURUSD",),
        ("5m",),
        stream_scope=ACTIVE_SCOPE,
        session_factory=Session,
        candle_cache={f"{ACTIVE_SCOPE}:EURUSD:5m": {"data": original}},
        cache_health_reader=_usable_health,
        now=datetime(2026, 9, 15, 12, 17, tzinfo=timezone.utc),
    )

    frame = result["frames"]["EURUSD"]["5m"]
    assert list(frame.index) == list(pd.to_datetime([
        "2026-09-15T12:00:00Z",
        "2026-09-15T12:05:00Z",
        "2026-09-15T12:10:00Z",
    ]))
    meta = result["streams"]["EURUSD"]["5m"]
    assert meta["source"] == "in_memory_ctrader_closed_candles"
    assert meta["latest_candle_time"] == "2026-09-15T12:10:00+00:00"
    pd.testing.assert_frame_equal(original, before)


def test_display_snapshot_uses_memory_for_all_six_when_closed_frames_are_fresh():
    Session, _ = _sessions()
    now = datetime(2026, 9, 15, 16, 30, tzinfo=timezone.utc)
    starts = {
        "5m": "2026-09-15T16:10:00Z",
        "15m": "2026-09-15T15:45:00Z",
        "1h": "2026-09-15T13:00:00Z",
    }
    cache = {}
    for symbol in SYMBOLS:
        for timeframe in TIMEFRAMES:
            minutes = {"5m": 5, "15m": 15, "1h": 60}[timeframe]
            cache[f"{ACTIVE_SCOPE}:{symbol}:{timeframe}"] = {
                "data": _memory_frame(
                    starts[timeframe],
                    periods=3,
                    freq=f"{minutes}min",
                    base=1.0 if symbol == "EURUSD" else 3600.0,
                )
            }

    result = reader.load_dashboard_display_candles(
        SYMBOLS,
        TIMEFRAMES,
        stream_scope=ACTIVE_SCOPE,
        session_factory=Session,
        candle_cache=cache,
        cache_health_reader=_usable_health,
        now=now,
    )

    assert {
        (symbol, timeframe)
        for symbol, frames in result["frames"].items()
        for timeframe in frames
    } == {(symbol, timeframe) for symbol in SYMBOLS for timeframe in TIMEFRAMES}
    assert all(
        result["streams"][symbol][timeframe]["source"] == "in_memory_ctrader_closed_candles"
        for symbol in SYMBOLS
        for timeframe in TIMEFRAMES
    )


def test_display_snapshot_falls_back_individually_to_durable_streams():
    Session, _ = _sessions()
    _seed_all_streams(Session)
    now = datetime(2026, 9, 15, 16, 30, tzinfo=timezone.utc)
    cache = {
        f"{ACTIVE_SCOPE}:EURUSD:5m": {"data": _memory_frame("2026-09-15T16:10:00Z", freq="5min")},
        f"{ACTIVE_SCOPE}:EURUSD:15m": {"data": _memory_frame("2026-09-15T15:45:00Z", freq="15min")},
    }

    def health(symbol, timeframe):
        if f"{ACTIVE_SCOPE}:{symbol}:{timeframe}" not in cache:
            return {"usable": False}
        return _usable_health(symbol, timeframe)

    result = reader.load_dashboard_display_candles(
        SYMBOLS,
        TIMEFRAMES,
        stream_scope=ACTIVE_SCOPE,
        session_factory=Session,
        candle_cache=cache,
        cache_health_reader=health,
        now=now,
    )

    assert result["streams"]["EURUSD"]["5m"]["source"] == "in_memory_ctrader_closed_candles"
    assert result["streams"]["EURUSD"]["15m"]["source"] == "in_memory_ctrader_closed_candles"
    expected_durable = {
        ("EURUSD", "1h"),
        ("XAUUSD", "5m"),
        ("XAUUSD", "15m"),
        ("XAUUSD", "1h"),
    }
    assert {
        (symbol, timeframe)
        for symbol, frames in result["streams"].items()
        for timeframe, meta in frames.items()
        if meta["source"] == "persisted_ctrader_closed_candles"
    } == expected_durable


def test_display_snapshot_rejects_unusable_memory_and_uses_durable():
    Session, _ = _sessions()
    _seed_all_streams(Session)
    cache = {
        f"{ACTIVE_SCOPE}:XAUUSD:5m": {
            "data": _memory_frame("2026-09-15T16:10:00Z", base=3600.0),
        }
    }

    result = reader.load_dashboard_display_candles(
        ("XAUUSD",),
        ("5m",),
        stream_scope=ACTIVE_SCOPE,
        session_factory=Session,
        candle_cache=cache,
        cache_health_reader=lambda *_args: {"usable": False},
        now=datetime(2026, 9, 15, 16, 30, tzinfo=timezone.utc),
    )

    assert result["streams"]["XAUUSD"]["5m"]["source"] == "persisted_ctrader_closed_candles"


def test_display_snapshot_rechecks_freshness_after_forming_candle_is_removed(monkeypatch):
    Session, _ = _sessions()
    cached = pd.DataFrame(
        {
            "Open": [1.10, 1.11],
            "High": [1.11, 1.12],
            "Low": [1.09, 1.10],
            "Close": [1.105, 1.115],
        },
        index=pd.to_datetime(["2026-09-15T10:00:00Z", "2026-09-15T12:15:00Z"]),
    )
    monkeypatch.setattr(reader, "load_durable_indicator_candles", lambda *_args, **_kwargs: {})

    result = reader.load_dashboard_display_candles(
        ("EURUSD",),
        ("5m",),
        stream_scope=ACTIVE_SCOPE,
        session_factory=Session,
        candle_cache={f"{ACTIVE_SCOPE}:EURUSD:5m": {"data": cached}},
        cache_health_reader=lambda *_args: {
            "usable": True,
            "last_candle_age_seconds": 120.0,
            "max_recovery_age_seconds": 15 * 60,
        },
        now=datetime(2026, 9, 15, 12, 17, tzinfo=timezone.utc),
    )

    assert result == {"frames": {}, "streams": {}}


def test_display_snapshot_caps_memory_to_latest_500_closed_rows():
    Session, _ = _sessions()
    start = pd.Timestamp("2026-09-10T00:00:00Z")
    frame = _memory_frame(start.isoformat(), periods=502, freq="5min")
    now = (start + pd.Timedelta(minutes=5 * 503)).to_pydatetime()

    result = reader.load_dashboard_display_candles(
        ("EURUSD",),
        ("5m",),
        stream_scope=ACTIVE_SCOPE,
        session_factory=Session,
        candle_cache={f"{ACTIVE_SCOPE}:EURUSD:5m": {"data": frame}},
        cache_health_reader=_usable_health,
        now=now,
    )

    closed = result["frames"]["EURUSD"]["5m"]
    assert len(closed) == 500
    assert closed.index.is_monotonic_increasing
    assert closed.index[0] == start + pd.Timedelta(minutes=10)
    assert closed.index[-1] == start + pd.Timedelta(minutes=5 * 501)
