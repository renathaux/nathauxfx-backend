from __future__ import annotations

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db import Base
from models import IndicatorCandle, IndicatorEvent, IndicatorStreamState
from services import indicator_event_stream_service as stream
from services.indicator_stream_account_scope import (
    install_account_scoped_indicator_stream,
    storage_symbol_for_scope,
    uninstall_account_scoped_indicator_stream_for_tests,
)


def _session_factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine), engine


def _frame(close_shift=0.0):
    index = pd.date_range("2026-09-14T12:00:00Z", periods=8, freq="5min")
    rows = []
    for i in range(len(index)):
        base = 1.15000 + i * 0.00010
        rows.append({
            "Open": base,
            "High": base + 0.00020,
            "Low": base - 0.00010,
            "Close": base + 0.00012,
        })
    data = pd.DataFrame(rows, index=index)
    if close_shift:
        data.loc[index[2], "Close"] += close_shift
    return data


def _analysis(data, **_kwargs):
    return {
        "bias": "BULLISH",
        "events": [{
            "event_type": "CHOCH",
            "direction": "BULLISH",
            "timestamp": data.index[4].isoformat(),
            "close": float(data.iloc[4]["Close"]),
            "broken_swing_timestamp": data.index[1].isoformat(),
            "broken_level": float(data.iloc[3]["High"]),
            "structure_start_index": 1,
            "break_index": 4,
            "event_invalidation_swing": {
                "type": "LOW",
                "price": float(data.iloc[2]["Low"]),
                "swing_time": data.index[2].isoformat(),
                "source": "TEST",
            },
        }],
        "current_structure": {"bias": "BULLISH"},
        "swings": [],
        "fib_levels": [],
    }


def test_different_ctrader_accounts_get_isolated_streams_for_same_symbol():
    Session, engine = _session_factory()
    install_account_scoped_indicator_stream()
    try:
        scope_a = "CTRADER:DEMO:account-a"
        scope_b = "CTRADER:DEMO:account-b"
        base = _frame()

        first = stream.get_authoritative_structure(
            base,
            "EURUSD",
            "5m",
            0.00001,
            analyzer=_analysis,
            session_factory=Session,
            stream_scope=scope_a,
        )
        assert first["stream_status"] == "READY"
        assert first["stream_scope"] == scope_a
        assert first["events"][0]["symbol"] == "EURUSD"
        assert first["events"][0]["event_identity"]["symbol"] == "EURUSD"
        assert first["events"][0]["event_identity"]["stream_scope"] == scope_a
        assert first["events"][0]["tradable"] is False

        # A different broker account is allowed to have a different historical
        # candle for the same public symbol/time. It must create a separate
        # stream instead of poisoning account A with a reconciliation conflict.
        changed_history = _frame(close_shift=0.00003)
        second = stream.get_authoritative_structure(
            changed_history,
            "EURUSD",
            "5m",
            0.00001,
            analyzer=_analysis,
            session_factory=Session,
            stream_scope=scope_b,
        )
        assert second["stream_status"] == "READY"
        assert second["stream_scope"] == scope_b
        assert second["events"][0]["symbol"] == "EURUSD"
        assert second["events"][0]["event_identity"]["stream_scope"] == scope_b
        assert first["events"][0]["event_id"] != second["events"][0]["event_id"]

        db = Session()
        try:
            states = db.query(IndicatorStreamState).order_by(IndicatorStreamState.symbol).all()
            assert len(states) == 2
            assert states[0].symbol != states[1].symbol
            assert all("~" in row.symbol for row in states)
            assert db.query(IndicatorCandle).count() == 16
            assert db.query(IndicatorEvent).count() == 2
        finally:
            db.close()

        # Switching back to A reuses its existing stream and remains healthy.
        again = stream.get_authoritative_structure(
            base,
            "EURUSD",
            "5m",
            0.00001,
            analyzer=_analysis,
            session_factory=Session,
            stream_scope=scope_a,
        )
        assert again["stream_status"] == "READY"
        assert again["storage_symbol"] == storage_symbol_for_scope("EURUSD", scope_a)
        assert again["event_count"] == 1
    finally:
        uninstall_account_scoped_indicator_stream_for_tests()
        engine.dispose()


def test_same_account_still_fails_closed_on_a_historical_candle_correction():
    Session, engine = _session_factory()
    install_account_scoped_indicator_stream()
    try:
        scope = "CTRADER:DEMO:account-a"
        stream.get_authoritative_structure(
            _frame(),
            "XAUUSD",
            "5m",
            0.01,
            analyzer=_analysis,
            session_factory=Session,
            stream_scope=scope,
        )

        with pytest.raises(stream.IndicatorStreamUnavailable, match="conflicting closed candle correction"):
            stream.get_authoritative_structure(
                _frame(close_shift=0.25),
                "XAUUSD",
                "5m",
                0.01,
                analyzer=_analysis,
                session_factory=Session,
                stream_scope=scope,
            )

        db = Session()
        try:
            state = db.query(IndicatorStreamState).one()
            assert state.status == "RECONCILIATION_REQUIRED"
        finally:
            db.close()
    finally:
        uninstall_account_scoped_indicator_stream_for_tests()
        engine.dispose()


def test_missing_durable_timestamp_no_longer_turns_into_int_nan_error():
    install_account_scoped_indicator_stream()
    try:
        from services import paper_v3b_bridge as bridge

        details = bridge._freshness_details(
            "EURUSD",
            pd.Timestamp("2026-09-14T18:20:00Z"),
            {},
        )
        assert details["latest_durable_candle"] is None
        assert details["lag_minutes"] is None
        assert details["lag_candles"] is None
    finally:
        uninstall_account_scoped_indicator_stream_for_tests()
