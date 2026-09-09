from concurrent.futures import CancelledError
from datetime import datetime, timezone
import socket

import pandas as pd
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import api
import app_bootstrap
import ctrader_connector
from db import Base
from models import (
    ExecutionProtocolState,
    IndicatorCandle,
    IndicatorEvent,
    IndicatorEventLifecycle,
    IndicatorStreamState,
    TradeSubmissionAttempt,
)
from services import indicator_event_stream_service as stream
from services import smc_strategy_authority as authority
from services import trade_submission_service as submissions


def _frame(count=12):
    index = pd.date_range("2026-09-01T00:00:00Z", periods=count, freq="15min")
    return pd.DataFrame({
        "Open": [1.1000] * count,
        "High": [1.1020] * count,
        "Low": [1.0980] * count,
        "Close": [1.1015] * count,
    }, index=index)


def _event(data, index=5):
    return {
        "event_type": "CHOCH",
        "direction": "BEARISH",
        "timestamp": data.index[index].isoformat(),
        "close": 1.0990,
        "broken_swing_timestamp": data.index[index - 2].isoformat(),
        "broken_level": 1.1000,
        "structure_start_index": index - 2,
        "break_index": index,
        "event_invalidation_swing": {
            "type": "HIGH",
            "price": 1.1020,
            "swing_time": data.index[index - 1].isoformat(),
        },
    }


def _analysis(events):
    return {
        "bias": "BEARISH",
        "events": events,
        "current_structure": {"bias": "BEARISH"},
        "swings": [],
        "fib_levels": [],
    }


def _session_factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)


def _claim_fixture(account_id="account-1"):
    engine, Session = _session_factory()
    data = _frame()
    initialized = stream.initialize_indicator_stream(
        data,
        "EURUSD",
        "15m",
        0.00001,
        analyzer=lambda source, **_kwargs: _analysis([_event(source)]),
        session_factory=Session,
    )
    event_id = initialized["events"][0]["event_id"]
    session = Session()
    session.add(ExecutionProtocolState(
        singleton_id=1,
        protocol_version=submissions.EXECUTION_PROTOCOL_VERSION,
        updated_at=datetime.now(timezone.utc),
    ))
    session.commit()
    session.close()
    assert stream.update_event_lifecycle(
        event_id,
        "LIVE",
        "ELIGIBLE",
        owner_id="OWNER",
        account_id=account_id,
        session_factory=Session,
    )
    claim = submissions.claim_submission(
        event_id,
        "LIVE",
        account_id,
        "EURUSD",
        "setup-1",
        {"action": "SELL"},
        session_factory=Session,
    )
    assert claim["ok"]
    return engine, Session, event_id, claim


def test_actual_app_has_one_authoritative_public_chart_route(monkeypatch):
    matches = [
        route for route in api.app.routes
        if isinstance(route, APIRoute)
        and route.path == "/chart/smc-structure"
        and "GET" in route.methods
    ]
    assert len(matches) == 1
    assert matches[0].endpoint is app_bootstrap.chart_smc_structure
    assert matches[0].endpoint.__module__ == "app_bootstrap"
    monkeypatch.setattr(api, "get_ctrader_market_data", lambda *_args, **_kwargs: _frame())
    monkeypatch.setattr(
        app_bootstrap,
        "build_chart_structure",
        lambda *_args, **_kwargs: {
            "events": [{"event_id": "smc1_canonical"}],
            "source": "authoritative_indicator_event_stream",
        },
    )
    response = TestClient(api.app).get(
        "/chart/smc-structure?symbol=EURUSD&timeframe=15m&limit=250"
    )
    assert response.status_code == 200
    assert response.json()["events"][0]["event_id"] == "smc1_canonical"


def test_chart_read_does_not_change_stream_state(monkeypatch):
    engine, Session = _session_factory()
    data = _frame()
    initialized = stream.initialize_indicator_stream(
        data,
        "EURUSD",
        "15m",
        0.00001,
        analyzer=lambda source, **_kwargs: _analysis([_event(source)]),
        session_factory=Session,
    )
    canonical_id = initialized["events"][0]["event_id"]
    session = Session()
    before = {
        "candles": session.query(IndicatorCandle).count(),
        "events": session.query(IndicatorEvent).count(),
        "state": tuple(vars(session.query(IndicatorStreamState).one()).get(key) for key in (
            "origin_candle", "activation_watermark", "last_processed_candle", "status"
        )),
    }
    session.close()
    monkeypatch.setattr(stream, "SessionLocal", Session)
    result = authority.build_chart_structure(
        data.tail(10), "EURUSD", "15m", strict_trader_module=type("Trader", (), {
            "point_size": staticmethod(lambda _symbol: 0.00001),
            "shared": type("Shared", (), {"normalize_symbol": staticmethod(lambda value: value)}),
        })
    )
    assert [item["event_id"] for item in result["events"]] == [canonical_id]
    session = Session()
    after = {
        "candles": session.query(IndicatorCandle).count(),
        "events": session.query(IndicatorEvent).count(),
        "state": tuple(vars(session.query(IndicatorStreamState).one()).get(key) for key in (
            "origin_candle", "activation_watermark", "last_processed_candle", "status"
        )),
    }
    session.close()
    engine.dispose()
    assert after == before


def test_same_batch_identical_duplicates_are_idempotent():
    engine, Session = _session_factory()
    data = _frame(4)
    duplicate = pd.concat([data.iloc[:2], data.iloc[[1]], data.iloc[2:]])
    result = stream.initialize_indicator_stream(
        duplicate, "EURUSD", "15m", 0.00001,
        analyzer=lambda _source, **_kwargs: _analysis([]), session_factory=Session,
    )
    assert result["canonical_candle_count"] == 4
    engine.dispose()


def test_same_batch_conflicting_duplicates_halt_stream():
    engine, Session = _session_factory()
    data = _frame(4)
    conflict = data.iloc[[1]].copy()
    conflict.iloc[0, conflict.columns.get_loc("Close")] += 0.0001
    duplicate = pd.concat([data.iloc[:2], conflict, data.iloc[2:]])
    with pytest.raises(stream.IncomingCandleConflict):
        stream.initialize_indicator_stream(
            duplicate, "EURUSD", "15m", 0.00001,
            analyzer=lambda _source, **_kwargs: _analysis([]), session_factory=Session,
        )
    session = Session()
    assert session.query(IndicatorStreamState).one().status == "RECONCILIATION_REQUIRED"
    assert session.query(IndicatorCandle).count() == 0
    session.close()
    engine.dispose()


def test_restart_preserves_activation_watermark_and_post_activation_event():
    engine, Session = _session_factory()
    data = _frame(10)
    initial = stream.initialize_indicator_stream(
        data.iloc[:8], "EURUSD", "15m", 0.00001,
        analyzer=lambda source, **_kwargs: _analysis([_event(source, 5)]),
        session_factory=Session,
    )
    original_watermark = initial["activation_watermark"]
    restarted = stream.initialize_indicator_stream(
        data, "EURUSD", "15m", 0.00001,
        analyzer=lambda source, **_kwargs: _analysis([
            _event(source, 5), _event(source, 9)
        ]),
        session_factory=Session,
    )
    latest = next(item for item in restarted["events"] if item["timestamp"] == data.index[9].isoformat())
    assert restarted["activation_watermark"] == original_watermark
    assert latest["tradable"] is True
    engine.dispose()


@pytest.mark.parametrize(
    ("category", "expected_attempt", "expected_lifecycle"),
    [
        ("ACCEPTED", "ACCEPTED", "CONSUMED"),
        ("DEFINITELY_REJECTED", "DEFINITELY_REJECTED", "BLOCKED"),
        ("FAILED_BEFORE_SEND", "FAILED_BEFORE_SEND", "ELIGIBLE"),
        ("AMBIGUOUS", "RECONCILIATION_REQUIRED", "RECONCILIATION_REQUIRED"),
        ("ACCEPTED_PROTECTION_FAILED", "ACCEPTED_PROTECTION_FAILED", "CONSUMED"),
        ("UNKNOWN_VALUE", "RECONCILIATION_REQUIRED", "RECONCILIATION_REQUIRED"),
    ],
)
def test_broker_result_taxonomy(category, expected_attempt, expected_lifecycle):
    engine, Session, _event_id, claim = _claim_fixture()
    assert submissions.mark_request_started(claim["idempotency_key"], session_factory=Session)
    result = {
        "ok": category == "ACCEPTED",
        "broker_result": category,
        "order_id": "order-1" if category in {"ACCEPTED", "ACCEPTED_PROTECTION_FAILED"} else None,
        "position_id": "position-1" if category in {"ACCEPTED", "ACCEPTED_PROTECTION_FAILED"} else None,
    }
    assert submissions.complete_submission(
        claim["idempotency_key"], result, session_factory=Session
    )
    session = Session()
    assert session.query(TradeSubmissionAttempt).one().attempt_status == expected_attempt
    assert session.query(IndicatorEventLifecycle).one().status == expected_lifecycle
    session.close()
    engine.dispose()


@pytest.mark.parametrize(
    "error",
    [socket.timeout("timeout"), ConnectionError("disconnect"), CancelledError(),
     ValueError("malformed response"), RuntimeError("unknown")],
)
def test_every_exception_after_dispatch_is_ambiguous(error):
    assert error is not None
    assert ctrader_connector.classify_ctrader_failure(True) == "AMBIGUOUS"


def test_provable_pre_dispatch_failure_is_retryable_category_only():
    assert ctrader_connector.classify_ctrader_failure(False) == "FAILED_BEFORE_SEND"
    engine, Session, event_id, claim = _claim_fixture()
    assert submissions.mark_request_started(claim["idempotency_key"], session_factory=Session)
    assert submissions.complete_submission(
        claim["idempotency_key"],
        {"ok": False, "broker_result": "FAILED_BEFORE_SEND"},
        session_factory=Session,
    )
    retry = submissions.claim_submission(
        event_id, "LIVE", "account-1", "EURUSD", "setup-1",
        {"action": "SELL"}, session_factory=Session,
    )
    assert retry["ok"] is True
    engine.dispose()


def test_ctrader_references_are_stable_bounded_and_collision_resistant():
    observed = set()
    for account in ("a1", "a2"):
        for symbol in ("EURUSD", "XAUUSD"):
            for mode in ("PAPER", "LIVE"):
                for event_id in ("event-1", "event-2"):
                    for setup_id in ("m5-1", "m5-2"):
                        _identity, internal = submissions.submission_identity(
                            event_id, mode, "OWNER", account, symbol, setup_id
                        )
                        references = submissions.broker_order_references(internal)
                        assert references == submissions.broker_order_references(internal)
                        assert len(references["client_order_id"]) <= 50
                        assert len(references["label"]) <= 100
                        assert references["label"] == internal
                        assert references["client_order_id"] not in observed
                        observed.add(references["client_order_id"])
    assert len(observed) == 32


def test_ctrader_reference_lengths_are_rejected_before_any_network_setup(monkeypatch):
    monkeypatch.setattr(
        ctrader_connector,
        "get_ctrader_config",
        lambda: pytest.fail("configuration/network setup must not be reached"),
    )
    result = ctrader_connector.place_market_order(
        "EURUSD", action="BUY", client_order_id="x" * 51
    )
    assert result["broker_result"] == "FAILED_BEFORE_SEND"
    assert result["broker_order_sent"] is False


def test_reconciliation_match_never_crosses_account_symbol_direction_or_time():
    now = datetime.now(timezone.utc)
    attempt = {
        "key": "fs1_full",
        "client_order_id": "fsc1_short",
        "account_id": "a1",
        "symbol": "EURUSD",
        "direction": "SELL",
        "claimed_at": now,
    }
    base = {
        "account_id": "a1", "symbol": "EURUSD", "direction": "SELL",
        "position_id": "p1", "timestamp": int(now.timestamp() * 1000),
        "raw": {"tradeData": {"comment": "FS:fsc1_short"}},
    }
    assert submissions._record_matches_attempt(base, attempt)
    for field, value in (("account_id", "a2"), ("symbol", "XAUUSD"), ("direction", "BUY")):
        assert not submissions._record_matches_attempt({**base, field: value}, attempt)
    assert not submissions._record_matches_attempt({**base, "position_id": None}, attempt)
    assert not submissions._record_matches_attempt({
        **base, "timestamp": int((now.timestamp() - 60) * 1000)
    }, attempt)


def test_reconciliation_queries_each_incomplete_attempt_account_separately():
    engine, Session, event_id, first = _claim_fixture("a1")
    assert submissions.mark_request_started(first["idempotency_key"], session_factory=Session)
    assert stream.update_event_lifecycle(
        event_id, "LIVE", "ELIGIBLE", owner_id="OWNER", account_id="a2",
        session_factory=Session,
    )
    second = submissions.claim_submission(
        event_id, "LIVE", "a2", "EURUSD", "setup-1", {"action": "SELL"},
        session_factory=Session,
    )
    assert second["ok"]
    assert submissions.mark_request_started(second["idempotency_key"], session_factory=Session)
    calls = []

    def provider(account_id, _claimed_at):
        calls.append(account_id)
        claim = first if account_id == "a1" else second
        return {
            "ok": True,
            "complete": True,
            "records": [{
                "account_id": account_id,
                "symbol": "EURUSD",
                "direction": "SELL",
                "order_id": f"order-{account_id}",
                "raw": {"clientOrderId": claim["broker_client_order_id"]},
            }],
        }

    result = submissions.reconcile_incomplete_submissions(
        record_provider=provider, session_factory=Session
    )
    assert result["ok"] is True
    assert sorted(calls) == ["a1", "a2"]
    session = Session()
    assert {row.account_id: row.status for row in session.query(IndicatorEventLifecycle)} == {
        "a1": "CONSUMED", "a2": "CONSUMED"
    }
    session.close()
    engine.dispose()


def test_protocol_fence_absent_or_incompatible_fails_closed():
    engine, Session = _session_factory()
    assert submissions.verify_execution_protocol(session_factory=Session) is False
    session = Session()
    session.add(ExecutionProtocolState(
        singleton_id=1, protocol_version="old", updated_at=datetime.now(timezone.utc)
    ))
    session.commit()
    session.close()
    assert submissions.verify_execution_protocol(session_factory=Session) is False
    engine.dispose()


def test_schema_status_columns_accept_reconciliation_required():
    assert IndicatorEventLifecycle.status.type.length >= len("RECONCILIATION_REQUIRED")
    assert TradeSubmissionAttempt.attempt_status.type.length >= len("RECONCILIATION_REQUIRED")
    assert TradeSubmissionAttempt.reconciliation_status.type.length >= len("RECONCILIATION_REQUIRED")


def test_only_postgres_deadlock_and_serialization_errors_are_db_retryable():
    class Original:
        def __init__(self, code):
            self.pgcode = code

    deadlock = OperationalError("statement", {}, Original("40P01"))
    serialization = OperationalError("statement", {}, Original("40001"))
    ordinary = OperationalError("statement", {}, Original("08006"))
    assert submissions._retryable_postgres_transaction_error(deadlock)
    assert submissions._retryable_postgres_transaction_error(serialization)
    assert not submissions._retryable_postgres_transaction_error(ordinary)
