from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db import Base
from models import (
    IndicatorCandle,
    IndicatorEvent,
    IndicatorEventLifecycle,
    IndicatorStreamState,
    TradeSubmissionAttempt,
)
from services import indicator_event_stream_service as stream
from services.indicator_stream_account_scope import (
    install_account_scoped_indicator_stream,
    storage_symbol_for_scope,
    uninstall_account_scoped_indicator_stream_for_tests,
)
from services.paper_v3b_bridge import build_paper_v3b_candidate


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


def _analysis_before_conflict(data, **_kwargs):
    result = _analysis(data)
    result["events"][0]["timestamp"] = data.index[1].isoformat()
    result["events"][0]["broken_swing_timestamp"] = data.index[0].isoformat()
    result["events"][0]["broken_level"] = float(data.iloc[0]["High"])
    result["events"][0]["break_index"] = 1
    result["events"][0]["structure_start_index"] = 0
    result["events"][0]["event_invalidation_swing"] = {
        "type": "LOW",
        "price": float(data.iloc[0]["Low"]),
        "swing_time": data.index[0].isoformat(),
        "source": "TEST",
    }
    return result


def _confirmation_at_conflict(data):
    return {
        "symbol": "EURUSD",
        "timeframe": "5m",
        "candle_open_time": data.index[2].isoformat(),
        "candle_close_time": (data.index[2] + pd.Timedelta(minutes=5)).isoformat(),
    }


def test_different_ctrader_accounts_get_isolated_streams_for_same_symbol():
    Session, engine = _session_factory()
    install_account_scoped_indicator_stream()
    try:
        scope_a = "CTRADER:DEMO:ACCOUNT-A"
        scope_b = "CTRADER:DEMO:ACCOUNT-B"
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


def _rows_for(db, model, symbol, timeframe):
    return db.query(model).filter_by(symbol=symbol, timeframe=timeframe).order_by(
        model.candle_timestamp, getattr(model, "event_id", model.candle_timestamp)
    ).all()


def test_account_scoped_v3b_5m_correction_rolls_back_and_replays(caplog):
    Session, engine = _session_factory()
    install_account_scoped_indicator_stream()
    try:
        scope = "CTRADER:DEMO:ACCOUNT-A"
        storage = storage_symbol_for_scope("XAUUSD", scope)
        empty_analysis = lambda _data, **_kwargs: {
            "bias": "NEUTRAL", "events": [], "current_structure": None,
            "swings": [], "fib_levels": [],
        }
        stream.get_authoritative_structure(
            _frame(),
            "XAUUSD",
            "5m",
            0.01,
            analyzer=empty_analysis,
            session_factory=Session,
            stream_scope=scope,
        )
        with Session() as db:
            state = db.query(IndicatorStreamState).one()
            origin_candle = state.origin_candle
            activation_watermark = state.activation_watermark
            configuration_version = state.configuration_version
            state.status = "RECONCILIATION_REQUIRED"
            state.reconciliation_reason = (
                "conflicting closed candle correction at "
                f"{_frame().index[2].isoformat()}"
            )
            db.commit()

        corrected = _frame(close_shift=0.25)
        forming_time = corrected.index[-1] + pd.Timedelta(minutes=5)
        corrected.loc[forming_time] = corrected.iloc[-1]

        class ClosedStrict:
            @staticmethod
            def closed_frame(data, minutes):
                assert minutes == 5
                return data.iloc[:-1]

            @staticmethod
            def point_size(symbol):
                assert symbol == "XAUUSD"
                return 0.01

        def updater(source, symbol, timeframe, point_size, **_kwargs):
            return stream.get_authoritative_structure(
                source, symbol, timeframe, point_size,
                analyzer=empty_analysis, session_factory=Session,
                stream_scope=scope,
            )

        repaired_candidate = build_paper_v3b_candidate(
            "XAUUSD", corrected,
            strict_trader_module=ClosedStrict,
            authoritative_updater=updater,
        )
        assert repaired_candidate["paper_entry_reason"] == "WAIT_V3B_PAPER_5M_BOS"
        assert repaired_candidate["paper_entry_reason"] != "WAIT_V3B_5M_AUTHORITY_STALE"

        db = Session()
        try:
            state = db.query(IndicatorStreamState).one()
            assert state.status == "READY"
            assert state.reconciliation_reason is None
            assert state.origin_candle == origin_candle
            assert state.activation_watermark == activation_watermark
            assert state.configuration_version == configuration_version
            assert pd.Timestamp(state.last_processed_candle, tz="UTC") == _frame().index[-1]
            assert db.query(IndicatorCandle).filter_by(
                symbol=storage, timeframe="5m"
            ).count() == len(_frame())
            corrected = db.query(IndicatorCandle).filter_by(
                symbol=storage,
                timeframe="5m",
                candle_timestamp=_frame().index[2].to_pydatetime().replace(tzinfo=None),
            ).one()
            assert corrected.close_price == pytest.approx(_frame(close_shift=0.25).iloc[2]["Close"])
        finally:
            db.close()
        assert "V3B_5M_RECONCILIATION_START" in caplog.text
        assert "V3B_5M_RECONCILIATION_ROLLBACK" in caplog.text
        assert "V3B_5M_RECONCILIATION_REPLAY_COMPLETE" in caplog.text
        assert "2026-09-14T12:05:00+00:00" in caplog.text
    finally:
        uninstall_account_scoped_indicator_stream_for_tests()
        engine.dispose()


def test_safe_lifecycle_is_removed_and_event_is_deterministically_regenerated():
    Session, engine = _session_factory()
    install_account_scoped_indicator_stream()
    try:
        scope = "CTRADER:LIVE:ACCOUNT-A"
        created = stream.get_authoritative_structure(
            _frame(), "EURUSD", "5m", 0.00001,
            analyzer=_analysis_before_conflict,
            session_factory=Session, stream_scope=scope,
        )
        event_id = created["events"][0]["event_id"]
        assert stream.update_event_lifecycle(
            event_id, "LIVE", "ELIGIBLE", owner_id="OWNER",
            account_id="ACCOUNT-A", m5_confirmation_id="confirmation-1",
            m5_confirmation_identity=_confirmation_at_conflict(_frame()),
            session_factory=Session,
        )

        repaired = stream.get_authoritative_structure(
            _frame(close_shift=0.00003), "EURUSD", "5m", 0.00001,
            analyzer=_analysis_before_conflict,
            session_factory=Session, stream_scope=scope,
        )
        assert [item["event_id"] for item in repaired["events"]] == [event_id]
        with Session() as db:
            assert db.query(IndicatorEvent).filter_by(event_id=event_id).count() == 1
            assert db.query(IndicatorEventLifecycle).filter_by(event_id=event_id).count() == 0
    finally:
        uninstall_account_scoped_indicator_stream_for_tests()
        engine.dispose()


@pytest.mark.parametrize("lifecycle_status", ["SUBMITTING", "CONSUMED"])
def test_irreversible_live_lifecycle_blocks_repair_without_history_mutation(
    lifecycle_status, caplog
):
    Session, engine = _session_factory()
    install_account_scoped_indicator_stream()
    try:
        scope = "CTRADER:LIVE:ACCOUNT-A"
        storage = storage_symbol_for_scope("EURUSD", scope)
        created = stream.get_authoritative_structure(
            _frame(), "EURUSD", "5m", 0.00001,
            analyzer=_analysis_before_conflict,
            session_factory=Session, stream_scope=scope,
        )
        event_id = created["events"][0]["event_id"]
        assert stream.update_event_lifecycle(
            event_id, "LIVE", lifecycle_status, owner_id="OWNER",
            account_id="ACCOUNT-A", m5_confirmation_id="confirmation-1",
            m5_confirmation_identity=_confirmation_at_conflict(_frame()),
            session_factory=Session,
        )
        with Session() as db:
            before = [
                (row.candle_timestamp, row.open_price, row.high_price, row.low_price, row.close_price)
                for row in _rows_for(db, IndicatorCandle, storage, "5m")
            ]

        for _attempt in range(2):
            with pytest.raises(stream.IndicatorStreamUnavailable, match="irreversible"):
                stream.get_authoritative_structure(
                    _frame(close_shift=0.00003), "EURUSD", "5m", 0.00001,
                    analyzer=_analysis_before_conflict,
                    session_factory=Session, stream_scope=scope,
                )

        with Session() as db:
            after = [
                (row.candle_timestamp, row.open_price, row.high_price, row.low_price, row.close_price)
                for row in _rows_for(db, IndicatorCandle, storage, "5m")
            ]
            state = db.query(IndicatorStreamState).filter_by(
                symbol=storage, timeframe="5m"
            ).one()
            assert before == after
            assert db.query(IndicatorEvent).filter_by(event_id=event_id).count() == 1
            assert db.query(IndicatorEventLifecycle).filter_by(event_id=event_id).count() == 1
            assert state.status == "RECONCILIATION_REQUIRED"
            assert "irreversible" in state.reconciliation_reason
        assert "V3B_5M_RECONCILIATION_BLOCKED_IRREVERSIBLE_EVENT" in caplog.text
    finally:
        uninstall_account_scoped_indicator_stream_for_tests()
        engine.dispose()


def test_claimed_broker_linked_live_event_blocks_repair():
    Session, engine = _session_factory()
    install_account_scoped_indicator_stream()
    try:
        scope = "CTRADER:LIVE:ACCOUNT-A"
        created = stream.get_authoritative_structure(
            _frame(), "EURUSD", "5m", 0.00001,
            analyzer=_analysis, session_factory=Session, stream_scope=scope,
        )
        event_id = created["events"][0]["event_id"]
        now = pd.Timestamp("2026-09-14T13:00:00Z").to_pydatetime()
        with Session() as db:
            db.add(TradeSubmissionAttempt(
                event_id=event_id, mode="LIVE", owner_id="OWNER",
                account_id="ACCOUNT-A", symbol="EURUSD", direction="BUY",
                signal_setup_id="setup-1", idempotency_key="claim-1",
                attempt_status="ACCEPTED", claimed_at=now,
                broker_request_id="request-1", broker_client_order_id="client-1",
                request_payload_fingerprint="fingerprint", broker_order_id="order-1",
                reconciliation_status="NOT_REQUIRED", updated_at=now,
            ))
            db.commit()

        with pytest.raises(stream.IndicatorStreamUnavailable, match="claimed LIVE submission"):
            stream.get_authoritative_structure(
                _frame(close_shift=0.00003), "EURUSD", "5m", 0.00001,
                analyzer=_analysis, session_factory=Session, stream_scope=scope,
            )
        with Session() as db:
            assert db.query(TradeSubmissionAttempt).filter_by(event_id=event_id).count() == 1
            assert db.query(IndicatorStreamState).one().status == "RECONCILIATION_REQUIRED"
    finally:
        uninstall_account_scoped_indicator_stream_for_tests()
        engine.dispose()


def test_repair_is_account_symbol_timeframe_and_legacy_isolated_and_idempotent():
    Session, engine = _session_factory()
    install_account_scoped_indicator_stream()
    try:
        scope_a = "CTRADER:DEMO:ACCOUNT-A"
        scope_b = "CTRADER:DEMO:ACCOUNT-B"
        key_a = storage_symbol_for_scope("EURUSD", scope_a)
        key_b = storage_symbol_for_scope("EURUSD", scope_b)
        gold_key = storage_symbol_for_scope("XAUUSD", scope_a)
        base = _frame()
        for symbol, timeframe, scope in (
            ("EURUSD", "5m", scope_a),
            ("EURUSD", "5m", scope_b),
            ("XAUUSD", "5m", scope_a),
            ("EURUSD", "15m", scope_a),
            ("EURUSD", "1h", scope_a),
        ):
            stream.get_authoritative_structure(
                base, symbol, timeframe, 0.01 if symbol == "XAUUSD" else 0.00001,
                analyzer=_analysis, session_factory=Session, stream_scope=scope,
            )
        stream.get_authoritative_structure(
            base, "EURUSD", "5m", 0.00001,
            analyzer=_analysis, session_factory=Session, initialize=True,
        )

        untouched_keys = [(key_b, "5m"), (gold_key, "5m"), (key_a, "15m"), (key_a, "1h"), ("EURUSD", "5m")]
        with Session() as db:
            untouched_before = {
                key: [(row.candle_timestamp, row.close_price) for row in _rows_for(db, IndicatorCandle, *key)]
                for key in untouched_keys
            }

        first = stream.get_authoritative_structure(
            _frame(close_shift=0.00003), "EURUSD", "5m", 0.00001,
            analyzer=_analysis, session_factory=Session, stream_scope=scope_a,
        )
        second = stream.get_authoritative_structure(
            _frame(close_shift=0.00003), "EURUSD", "5m", 0.00001,
            analyzer=_analysis, session_factory=Session, stream_scope=scope_a,
        )
        assert first["stream_status"] == second["stream_status"] == "READY"
        assert [event["event_id"] for event in first["events"]] == [
            event["event_id"] for event in second["events"]
        ]
        with Session() as db:
            assert {
                key: [(row.candle_timestamp, row.close_price) for row in _rows_for(db, IndicatorCandle, *key)]
                for key in untouched_keys
            } == untouched_before
            assert db.query(IndicatorCandle).filter_by(symbol=key_a, timeframe="5m").count() == len(base)
            assert db.query(IndicatorEvent).filter_by(symbol=key_a, timeframe="5m").count() == 1
    finally:
        uninstall_account_scoped_indicator_stream_for_tests()
        engine.dispose()


def test_identical_closed_history_is_noop_and_forming_candle_is_not_reconciled(caplog):
    Session, engine = _session_factory()
    install_account_scoped_indicator_stream()
    try:
        scope = "CTRADER:DEMO:ACCOUNT-A"
        closed = _frame()
        stream.get_authoritative_structure(
            closed, "EURUSD", "5m", 0.00001,
            analyzer=_analysis, session_factory=Session, stream_scope=scope,
        )
        identical = stream.get_authoritative_structure(
            closed.copy(), "EURUSD", "5m", 0.00001,
            analyzer=_analysis, session_factory=Session, stream_scope=scope,
        )
        assert identical["new_event_ids"] == []

        # The production caller removes this row with closed_frame. A changed
        # synthetic/current row therefore never reaches durable ingestion.
        forming = closed.copy()
        forming.loc[closed.index[-1] + pd.Timedelta(minutes=5)] = {
            "Open": 1.20, "High": 1.30, "Low": 1.10, "Close": 1.25,
        }
        provider_closed = forming.iloc[:-1]
        result = stream.get_authoritative_structure(
            provider_closed, "EURUSD", "5m", 0.00001,
            analyzer=_analysis, session_factory=Session, stream_scope=scope,
        )
        assert result["stream_status"] == "READY"
        with Session() as db:
            assert db.query(IndicatorCandle).count() == len(closed)
        assert "V3B_5M_RECONCILIATION_START" not in caplog.text
    finally:
        uninstall_account_scoped_indicator_stream_for_tests()
        engine.dispose()


@pytest.mark.parametrize(
    ("timeframe", "stream_scope"),
    [("5m", None), ("15m", "CTRADER:DEMO:ACCOUNT-A")],
)
def test_corrections_outside_account_scoped_v3b_5m_remain_fail_closed(
    timeframe, stream_scope
):
    Session, engine = _session_factory()
    install_account_scoped_indicator_stream()
    try:
        kwargs = {
            "analyzer": _analysis,
            "session_factory": Session,
            "stream_scope": stream_scope,
        }
        if stream_scope is None:
            kwargs["initialize"] = True
        stream.get_authoritative_structure(
            _frame(), "EURUSD", timeframe, 0.00001, **kwargs
        )
        with pytest.raises(
            stream.IndicatorStreamUnavailable,
            match="conflicting closed candle correction",
        ):
            stream.get_authoritative_structure(
                _frame(close_shift=0.00003),
                "EURUSD", timeframe, 0.00001,
                analyzer=_analysis, session_factory=Session,
                stream_scope=stream_scope,
            )
        with Session() as db:
            assert db.query(IndicatorStreamState).one().status == "RECONCILIATION_REQUIRED"
    finally:
        uninstall_account_scoped_indicator_stream_for_tests()
        engine.dispose()


def test_two_workers_reconciling_same_stream_are_serialized_and_idempotent(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'concurrent-repair.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    install_account_scoped_indicator_stream()
    try:
        scope = "CTRADER:LIVE:ACCOUNT-A"
        stream.get_authoritative_structure(
            _frame(), "EURUSD", "5m", 0.00001,
            analyzer=_analysis, session_factory=Session, stream_scope=scope,
        )

        def reconcile():
            return stream.get_authoritative_structure(
                _frame(close_shift=0.00003), "EURUSD", "5m", 0.00001,
                analyzer=_analysis, session_factory=Session, stream_scope=scope,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _index: reconcile(), range(2)))
        assert [result["stream_status"] for result in results] == ["READY", "READY"]
        assert results[0]["events"] == results[1]["events"]
        storage = storage_symbol_for_scope("EURUSD", scope)
        with Session() as db:
            assert db.query(IndicatorCandle).filter_by(
                symbol=storage, timeframe="5m"
            ).count() == len(_frame())
            assert db.query(IndicatorEvent).filter_by(
                symbol=storage, timeframe="5m"
            ).count() == 1
            assert db.query(IndicatorStreamState).filter_by(
                symbol=storage, timeframe="5m"
            ).one().status == "READY"
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
