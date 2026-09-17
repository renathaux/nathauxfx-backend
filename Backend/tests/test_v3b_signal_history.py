from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from models import Base
from services.v3b_signal_history import list_v3b_transitions, record_v3b_transition


SCOPE = "CTRADER:DEMO:47810571"
OTHER = "CTRADER:DEMO:47784297"
START = datetime(2026, 9, 17, 4, 30, tzinfo=timezone.utc)


def _sessions():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)


def test_buy_survives_blocked_execution_and_repeated_polling():
    engine, sessions = _sessions()
    try:
        record_v3b_transition(SCOPE, "EURUSD", "WAIT", START, session_factory=sessions)
        buy = record_v3b_transition(
            SCOPE, "EURUSD", "BUY", START + timedelta(minutes=5),
            event_id="bos-1", confirmation_id="confirm-1", setup_id="setup-1",
            entry=1.14644, session_factory=sessions,
        )
        record_v3b_transition(
            SCOPE, "EURUSD", "BUY", START + timedelta(minutes=5, seconds=30),
            event_id="bos-1", confirmation_id="confirm-1", setup_id="setup-1",
            execution_status="BLOCKED", reason="WAIT_V3B_FROZEN_MANAGEMENT_CONTRACT",
            session_factory=sessions,
        )
        record_v3b_transition(
            SCOPE, "EURUSD", "BUY", START + timedelta(minutes=6),
            event_id="bos-1", confirmation_id="confirm-1", setup_id="setup-1",
            execution_status="BLOCKED", reason="WAIT_V3B_FROZEN_MANAGEMENT_CONTRACT",
            session_factory=sessions,
        )
        rows = list_v3b_transitions(SCOPE, session_factory=sessions)
        assert [row["signal"] for row in rows] == ["BUY", "WAIT"]
        assert rows[0]["id"] == buy["id"]
        assert rows[0]["execution_status"] == "BLOCKED"
        assert rows[0]["reason"] == "WAIT_V3B_FROZEN_MANAGEMENT_CONTRACT"
        assert rows[0]["event_id"] == "bos-1"
        assert rows[0]["timestamp"].endswith("Z")
    finally:
        engine.dispose()


def test_wait_after_buy_is_new_transition_and_history_is_scoped_and_limited():
    engine, sessions = _sessions()
    try:
        for index in range(13):
            record_v3b_transition(
                SCOPE, "EURUSD", "BUY" if index % 2 else "WAIT",
                START + timedelta(minutes=index * 5),
                setup_id=f"setup-{index}" if index % 2 else None,
                session_factory=sessions,
            )
        record_v3b_transition(OTHER, "EURUSD", "SELL", START, session_factory=sessions)
        rows = list_v3b_transitions(SCOPE, limit=10, session_factory=sessions)
        assert len(rows) == 10
        assert [row["signal"] for row in rows[:2]] == ["WAIT", "BUY"]
        assert all(row["account_scope"] == SCOPE for row in rows)
        assert rows[0]["timestamp"] > rows[-1]["timestamp"]
        assert len(list_v3b_transitions(OTHER, session_factory=sessions)) == 1
        # A new service call/session after the original writes still reads rows.
        assert len(list_v3b_transitions(SCOPE, limit=10, session_factory=sessions)) == 10
    finally:
        engine.dispose()


def test_invalid_scope_fails_closed_without_legacy_or_unscoped_rows():
    engine, sessions = _sessions()
    try:
        assert record_v3b_transition(None, "EURUSD", "BUY", START, session_factory=sessions) is None
        assert list_v3b_transitions(None, session_factory=sessions) == []
        assert list_v3b_transitions("EURUSD", session_factory=sessions) == []
    finally:
        engine.dispose()
