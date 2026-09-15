from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db import Base
from models import IndicatorCandle
from services import indicator_candle_display_reader as reader
from services.indicator_stream_account_scope import storage_symbol_for_scope


ACTIVE_SCOPE = "CTRADER:LIVE:47810571"
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


def test_returns_all_six_active_account_scoped_closed_streams():
    Session, _ = _sessions()
    _seed_all_streams(Session)

    result = reader.load_closed_indicator_candles(
        SYMBOLS, TIMEFRAMES, stream_scope=ACTIVE_SCOPE,
        session_factory=Session, now=datetime(2026, 9, 2, tzinfo=timezone.utc),
    )

    assert {
        (symbol, timeframe)
        for symbol, frames in result.items()
        for timeframe in frames
    } == {(symbol, timeframe) for symbol in SYMBOLS for timeframe in TIMEFRAMES}
    assert all(len(result[symbol][timeframe]) == 3 for symbol in SYMBOLS for timeframe in TIMEFRAMES)


def test_resolves_the_active_scope_through_existing_account_scope_logic(monkeypatch):
    Session, _ = _sessions()
    _seed_all_streams(Session)
    monkeypatch.setattr(reader, "active_ctrader_stream_scope", lambda: ACTIVE_SCOPE)

    result = reader.load_closed_indicator_candles(
        SYMBOLS, TIMEFRAMES, session_factory=Session,
        now=datetime(2026, 9, 2, tzinfo=timezone.utc),
    )

    assert set(result) == set(SYMBOLS)


def test_another_account_scope_cannot_leak_into_active_display():
    Session, _ = _sessions()
    other_scope = "CTRADER:LIVE:99999999"
    _seed_all_streams(Session, other_scope)

    result = reader.load_closed_indicator_candles(
        SYMBOLS, TIMEFRAMES, stream_scope=ACTIVE_SCOPE,
        session_factory=Session, now=datetime(2026, 9, 2, tzinfo=timezone.utc),
    )

    assert result == {}


def test_missing_or_invalid_scope_never_uses_legacy_unscoped_rows(monkeypatch):
    Session, _ = _sessions()
    with Session.begin() as session:
        _seed(session, "EURUSD", "5m")

    monkeypatch.setattr(reader, "active_ctrader_stream_scope", lambda: None)
    for scope in (None, "CTRADER:INVALID:47810571", "CTRADER:LIVE:"):
        assert reader.load_closed_indicator_candles(
            SYMBOLS, TIMEFRAMES, stream_scope=scope,
            session_factory=Session, now=datetime(2026, 9, 2, tzinfo=timezone.utc),
        ) == {}


def test_limits_to_latest_500_rows_and_returns_ascending_closed_candles():
    Session, _ = _sessions()
    storage_symbol = storage_symbol_for_scope("EURUSD", ACTIVE_SCOPE)
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    with Session.begin() as session:
        _seed(session, storage_symbol, "5m", count=502, start=start)

    frame = reader.load_closed_indicator_candles(
        ("EURUSD",), ("5m",), stream_scope=ACTIVE_SCOPE,
        session_factory=Session, now=datetime(2026, 9, 4, tzinfo=timezone.utc),
    )["EURUSD"]["5m"]

    assert len(frame) == 500
    assert frame.index.is_monotonic_increasing
    assert frame.index[0] == start + timedelta(minutes=10)
    assert frame.index[-1] == start + timedelta(minutes=5 * 501)


def test_reader_issues_selects_only_and_never_writes():
    Session, engine = _sessions()
    _seed_all_streams(Session)
    statements = []
    event.listen(
        engine,
        "before_cursor_execute",
        lambda _conn, _cursor, statement, *_args: statements.append(statement.strip().upper()),
    )

    reader.load_closed_indicator_candles(
        SYMBOLS, TIMEFRAMES, stream_scope=ACTIVE_SCOPE,
        session_factory=Session, now=datetime(2026, 9, 2, tzinfo=timezone.utc),
    )

    assert statements
    assert all(statement.startswith("SELECT") for statement in statements)
