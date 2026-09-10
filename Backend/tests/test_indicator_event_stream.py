from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
from unittest.mock import patch

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db import Base
from models import (
    ExecutionProtocolState,
    IndicatorCandle, IndicatorEvent, IndicatorEventLifecycle,
    IndicatorStreamState, TradeSubmissionAttempt,
)
from services import indicator_event_stream_service as stream
from services import paper_live_entry_service as paper
from services import smc_strategy_authority as authority
from services import trade_submission_service as submissions
from strategies import strict_trader
import api
import app_bootstrap


def frame(count=12, start="2026-09-01T00:00:00Z"):
    index = pd.date_range(start, periods=count, freq="15min")
    return pd.DataFrame({
        "Open": [1.1000] * count,
        "High": [1.1020] * count,
        "Low": [1.0980] * count,
        "Close": [1.1015] * count,
    }, index=index)


def event(data, index, *, classification="BOS", level=1.1000, swing=1.0980):
    return {
        "event_type": classification,
        "direction": "BULLISH",
        "timestamp": data.index[index].isoformat(),
        "close": 1.1015,
        "broken_swing_timestamp": data.index[max(0, index - 3)].isoformat(),
        "broken_level": level,
        "structure_start_index": max(0, index - 3),
        "break_index": index,
        "event_invalidation_swing": {
            "type": "LOW",
            "price": swing,
            "swing_time": data.index[max(0, index - 2)].isoformat(),
            "source": "LEGACY_CURRENT_STRUCTURE",
        },
    }


def analysis(events):
    return {
        "bias": "BULLISH",
        "events": events,
        "current_structure": {"bias": "BULLISH"},
        "swings": [],
        "fib_levels": [],
    }


class StubTrader:
    BOS_MIN_BUFFER_POINTS = 10

    class shared:
        FIFTEEN_M_SWING_WATCH = {}

        @staticmethod
        def normalize_symbol(value):
            return str(value).upper()

        @staticmethod
        def save_fifteen_m_swing_watch():
            return None

    @staticmethod
    def point_size(_symbol):
        return 0.00001

    @staticmethod
    def minimum_swing_size(_symbol):
        return 0.001

    @staticmethod
    def get_cached_execution_settings():
        return {"bos_buffer_points": 10}

    @staticmethod
    def bos_buffer(_data, _symbol, _configured):
        return 0.0001

    @staticmethod
    def utc_timestamp(value):
        timestamp = pd.Timestamp(value)
        return timestamp.tz_localize("UTC") if timestamp.tzinfo is None else timestamp.tz_convert("UTC")

    @staticmethod
    def candle_close_time(value, minutes):
        return (StubTrader.utc_timestamp(value) + pd.Timedelta(minutes=minutes)).isoformat()

    @staticmethod
    def clear_opposite_watch(*_args):
        return False

    @staticmethod
    def get_watch_key(symbol, side):
        return f"{symbol}:{side}"

    @staticmethod
    def remembered_breakout(*_args, **_kwargs):
        return None


def make_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine), engine


def test_event_identity_contains_only_stable_indicator_fields():
    data = frame()
    identity, event_id = stream.build_event_identity(
        event(data, 5), "EURUSD", "15m", 0.00001
    )
    assert identity == {
        "symbol": "EURUSD",
        "timeframe": "15m",
        "candle_timestamp": data.index[5].isoformat(),
        "classification": "BOS",
        "direction": "BULLISH",
        "broken_level": "1.10000",
        "opposite_swing": {
            "type": "LOW",
            "price": "1.09800",
            "swing_time": data.index[3].isoformat(),
        },
    }
    assert event_id.startswith("smc1_")
    assert "status" not in identity


def test_historical_event_is_immutable_across_append_older_load_and_restart():
    Session, engine = make_session()
    initial = frame(12)

    def first_analyzer(data, **_kwargs):
        return analysis([event(data, 5)])

    first = stream.initialize_indicator_stream(
        initial, "EURUSD", "15m", 0.00001,
        analyzer=first_analyzer, session_factory=Session,
    )
    original = first["events"][0]

    appended = frame(13)

    def later_analyzer(data, **_kwargs):
        return analysis([
            event(data, 5, classification="CHOCH", level=1.1006, swing=1.0990),
            event(data, 12, classification="CHOCH", level=1.1010, swing=1.0995),
        ])

    second = stream.get_authoritative_structure(
        appended, "EURUSD", "15m", 0.00001,
        analyzer=later_analyzer, session_factory=Session,
    )
    assert second["events"][0] == original
    assert len(second["events"]) == 2

    older_plus_same = appended.tail(8)
    restarted = stream.get_authoritative_structure(
        older_plus_same, "EURUSD", "15m", 0.00001,
        analyzer=later_analyzer, session_factory=Session,
    )
    assert restarted["events"][0] == original
    durable_after_restart = stream.read_authoritative_event(
        original["event_id"], session_factory=Session
    )
    assert durable_after_restart["event_id"] == original["event_id"]
    assert durable_after_restart["broken_swing_timestamp"] == original["broken_swing_timestamp"]
    assert durable_after_restart["tradable"] is False

    session = Session()
    try:
        assert session.query(IndicatorEvent).count() == 2
        assert session.query(IndicatorCandle).count() == 13
    finally:
        session.close()
        engine.dispose()


def test_event_lifecycle_is_separate_and_consumed_event_blocks_reprocessing():
    Session, engine = make_session()
    data = frame()
    created = stream.initialize_indicator_stream(
        data,
        "EURUSD",
        "15m",
        0.00001,
        analyzer=lambda source, **_kwargs: analysis([event(source, 5)]),
        session_factory=Session,
    )
    confirmed = created["events"][0]
    immutable_identity = confirmed["event_identity"].copy()
    assert stream.update_event_lifecycle(
        confirmed["event_id"],
        "LIVE",
        "CONSUMED",
        m5_confirmation_id="m5_confirmation",
        signal_setup_id="setup_id",
        session_factory=Session,
    )
    lifecycle = stream.get_event_lifecycles(
        [confirmed["event_id"]], session_factory=Session
    )[confirmed["event_id"]]["LIVE"]
    assert lifecycle["status"] == "CONSUMED"
    assert confirmed["event_identity"] == immutable_identity

    with patch.object(
        api,
        "get_event_lifecycles",
        return_value={confirmed["event_id"]: {"LIVE": lifecycle}},
    ), patch.dict(api.LIVE_ACTIVE_ORDERS, {"EURUSD": None}, clear=False):
        blocked = api.validate_auto_entry_state_locked(
            "EURUSD",
            "BUY",
            {"source_indicator_event_id": confirmed["event_id"]},
            broker_positions=[],
        )
    assert blocked["reason"] == "indicator event unavailable: CONSUMED"
    assert not stream.update_event_lifecycle(
        confirmed["event_id"], "LIVE", "ELIGIBLE", session_factory=Session
    )
    engine.dispose()


def test_chart_and_strategy_use_identical_persisted_event():
    data = frame(10)
    raw = event(data, 9, classification="CHOCH")
    identity, event_id = stream.build_event_identity(raw, "EURUSD", "15m", 0.00001)
    raw.update({"event_id": event_id, "event_identity": identity, "tradable": True})
    stable = analysis([raw])
    with patch.object(authority, "read_authoritative_structure", return_value=stable), patch.object(
        authority, "get_authoritative_structure", return_value=stable
    ):
        chart = authority.build_chart_structure(
            data, "EURUSD", "15m", strict_trader_module=StubTrader
        )
        strategy = authority.evaluate_indicator_breakout(
            data, "EURUSD", strict_trader_module=StubTrader
        )
    assert chart["events"][-1]["event_id"] == event_id
    assert strategy["indicator_event_id"] == event_id
    assert strategy["event_invalidation_swing"] == raw["event_invalidation_swing"]


def test_forming_candle_cannot_enter_authoritative_stream():
    now = pd.Timestamp(datetime.now(timezone.utc)).floor("15min")
    index = pd.date_range(end=now, periods=12, freq="15min")
    data = frame(12)
    data.index = index
    closed = strict_trader.closed_frame(data, 15)
    assert len(closed) == 11
    Session, engine = make_session()

    def analyzer(source, **_kwargs):
        return analysis([] if now not in source.index else [event(source, len(source) - 1)])

    result = stream.initialize_indicator_stream(
        closed, "EURUSD", "15m", 0.00001,
        analyzer=analyzer, session_factory=Session,
    )
    assert result["events"] == []
    engine.dispose()


def test_paper_uses_live_event_confirmation_and_levels_without_recalculation():
    live = {
        "symbol": "XAUUSD",
        "signal": "BUY",
        "final_signal": "BUY",
        "signal_after_filters": "BUY",
        "strategy_setup_complete": True,
        "source_indicator_event_id": "smc1_event",
        "indicator_event_identity": {"symbol": "XAUUSD"},
        "m5_confirmation_id": "m5_confirmation",
        "m5_confirmation_identity": {"direction": "BUY"},
        "setup_identity": {
            "indicator_event_id": "smc1_event",
            "m5_confirmation_id": "m5_confirmation",
        },
        "entry_price": 4649.0,
        "stop_loss": 4643.0,
        "tp1": 4658.6,
        "tp2": 4661.0,
        "risk_reward_ratio": 2.0,
    }
    final_gate = lambda *_args, **_kwargs: {"ok": True, "reason": None}
    result = paper.build_paper_entry_result(
        "XAUUSD", live, frame(), frame(),
        strict_trader_module=StubTrader,
        final_gate=final_gate,
    )
    assert result["signal"] == live["signal"]
    assert result["source_indicator_event_id"] == live["source_indicator_event_id"]
    assert result["m5_confirmation_id"] == live["m5_confirmation_id"]
    assert result["stop_loss"] == 4643.0
    assert result["paper_entry_model"] == "PAPER_LIVE_INDICATOR_EVENT_V1"


def test_paper_preserves_live_filter_block_reason_for_same_event():
    live = {
        "symbol": "EURUSD",
        "signal": "WAIT",
        "strategy_setup_complete": False,
        "blocked_reason": "WAIT_EMA_NOT_ALLOWED",
        "setup_status": "BLOCKED",
        "source_indicator_event_id": "smc1_event",
        "indicator_event_identity": {"symbol": "EURUSD"},
        "fifteen_m_swing_break": {"indicator_event_id": "smc1_event"},
    }
    with patch.object(paper, "update_event_lifecycle", return_value=True):
        result = paper.build_paper_entry_result(
            "EURUSD", live, frame(), frame(), strict_trader_module=StubTrader
        )
    assert result["signal"] == "WAIT"
    assert result["paper_entry_reason"] == "WAIT_EMA_NOT_ALLOWED"
    assert result["source_indicator_event_id"] == "smc1_event"


def test_paper_waits_when_live_has_no_authoritative_event():
    result = paper.build_paper_entry_result(
        "EURUSD",
        {"signal": "WAIT", "strategy_setup_complete": False},
        frame(),
        frame(),
        strict_trader_module=StubTrader,
    )
    assert result["signal"] == "WAIT"
    assert result["setup_status"] == "WAITING"
    assert result["paper_entry_reason"] == "WAIT_AUTHORITATIVE_INDICATOR_EVENT"


def _claim_fixture(account_id="account-1"):
    path = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    created = stream.initialize_indicator_stream(
        frame(), "EURUSD", "15m", 0.00001,
        analyzer=lambda source, **_kwargs: analysis([event(source, 5)]),
        session_factory=Session,
    )
    event_id = created["events"][0]["event_id"]
    session = Session()
    session.add(ExecutionProtocolState(
        singleton_id=1,
        protocol_version=submissions.EXECUTION_PROTOCOL_VERSION,
        updated_at=datetime.now(timezone.utc),
    ))
    session.commit()
    session.close()
    assert stream.update_event_lifecycle(
        event_id, "LIVE", "ELIGIBLE", owner_id="OWNER",
        account_id=account_id, session_factory=Session,
    )
    return engine, Session, event_id


def test_two_workers_get_exactly_one_durable_submission_claim():
    engine, Session, event_id = _claim_fixture()
    broker_requests = []
    def claim():
        result = submissions.claim_submission(
            event_id, "LIVE", "account-1", "EURUSD", "setup-1",
            {"entry": 1.1, "action": "BUY"}, session_factory=Session,
        )
        if result.get("ok"):
            broker_requests.append(result["idempotency_key"])
        return result
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _value: claim(), range(2)))
    assert sum(bool(result.get("ok")) for result in results) == 1
    assert len(broker_requests) == 1
    session = Session()
    assert session.query(TradeSubmissionAttempt).count() == 1
    assert session.query(IndicatorEventLifecycle).filter_by(event_id=event_id, mode="LIVE", owner_id="OWNER", account_id="account-1").one().status == "SUBMITTING"
    session.close(); engine.dispose()


def test_inflight_and_terminal_lifecycle_cannot_be_reopened():
    engine, Session, event_id = _claim_fixture()
    claim = submissions.claim_submission(
        event_id, "LIVE", "account-1", "EURUSD", "setup-1", {"action": "BUY"}, session_factory=Session
    )
    assert claim["ok"]
    assert not stream.update_event_lifecycle(event_id, "LIVE", "BLOCKED", owner_id="OWNER", account_id="account-1", session_factory=Session)
    assert submissions.complete_submission(claim["idempotency_key"], {"ok": True, "broker_result": "ACCEPTED", "order_id": "o1"}, session_factory=Session)
    assert not stream.update_event_lifecycle(event_id, "LIVE", "ELIGIBLE", owner_id="OWNER", account_id="account-1", session_factory=Session)
    engine.dispose()


@pytest.mark.parametrize("terminal", ["EXPIRED", "INVALIDATED"])
def test_expired_and_invalidated_events_cannot_be_claimed(terminal):
    engine, Session, event_id = _claim_fixture()
    assert stream.update_event_lifecycle(event_id, "LIVE", terminal, owner_id="OWNER", account_id="account-1", session_factory=Session)
    result = submissions.claim_submission(
        event_id, "LIVE", "account-1", "EURUSD", "setup-1", {"action": "BUY"}, session_factory=Session
    )
    assert not result["ok"]
    assert result["status"] == terminal
    engine.dispose()


def test_backfill_events_are_historical_and_not_strategy_candidates():
    Session, engine = make_session()
    data = frame(10)
    initialized = stream.initialize_indicator_stream(
        data, "EURUSD", "15m", 0.00001,
        analyzer=lambda source, **_kwargs: analysis([event(source, 9)]),
        session_factory=Session,
    )
    assert initialized["events"][-1]["tradable"] is False
    with patch.object(authority, "get_authoritative_structure", return_value=initialized):
        result = authority.evaluate_indicator_breakout(
            data, "EURUSD", strict_trader_module=StubTrader
        )
    assert result["side"] == "WAIT"
    engine.dispose()


def test_unsent_crash_recovers_but_ambiguous_send_never_retries():
    engine, Session, event_id = _claim_fixture("a")
    claim = submissions.claim_submission(event_id, "LIVE", "a", "EURUSD", "s", {"action": "BUY"}, session_factory=Session)
    assert submissions.recover_unsent_claim(claim["idempotency_key"], session_factory=Session)
    retry = submissions.claim_submission(event_id, "LIVE", "a", "EURUSD", "s", {"action": "BUY"}, session_factory=Session)
    assert retry["ok"]
    assert submissions.mark_request_started(retry["idempotency_key"], session_factory=Session)
    assert submissions.require_reconciliation(retry["idempotency_key"], "timeout", session_factory=Session)
    assert not submissions.claim_submission(event_id, "LIVE", "a", "EURUSD", "s", {"action": "BUY"}, session_factory=Session)["ok"]
    unresolved = submissions.reconcile_incomplete_submissions([], session_factory=Session)
    assert unresolved["ok"] is False
    assert unresolved["unresolved"] == [retry["idempotency_key"]]
    assert submissions.reconcile_known_broker_order(retry["idempotency_key"], {"ok": True, "order_id": "known"}, session_factory=Session)
    engine.dispose()


def test_startup_reconciliation_matches_stable_broker_reference_without_retry():
    engine, Session, event_id = _claim_fixture("a")
    claim = submissions.claim_submission(event_id, "LIVE", "a", "EURUSD", "s", {"action": "BUY"}, session_factory=Session)
    assert submissions.mark_request_started(claim["idempotency_key"], session_factory=Session)
    result = submissions.reconcile_incomplete_submissions(
        [{
            "position_id": "p1", "account_id": "a", "symbol": "EURUSD",
            "direction": "BUY",
            "raw": {"tradeData": {"label": claim["idempotency_key"]}},
        }],
        session_factory=Session,
    )
    assert result == {
        "ok": True,
        "recovered_unsent": [],
        "matched_broker_orders": [claim["idempotency_key"]],
        "unresolved": [],
    }
    assert not submissions.claim_submission(event_id, "LIVE", "a", "EURUSD", "s", {"action": "BUY"}, session_factory=Session)["ok"]
    session = Session()
    assert session.query(TradeSubmissionAttempt).one().attempt_status == "ACCEPTED"
    assert session.query(IndicatorEventLifecycle).one().status == "CONSUMED"
    session.close(); engine.dispose()


def test_unknown_long_in_session_gap_blocks_watermark_advancement():
    Session, engine = make_session()
    data = frame(10).drop(frame(10).index[2:8])
    with pytest.raises(stream.IndicatorStreamUnavailable, match="missing closed candles"):
        stream.initialize_indicator_stream(
            data, "EURUSD", "15m", 0.00001,
            analyzer=lambda _source, **_kwargs: analysis([]), session_factory=Session,
        )
    session = Session()
    state = session.query(IndicatorStreamState).one()
    assert state.status == "GAP_BLOCKED"
    assert state.last_processed_candle is None
    session.close(); engine.dispose()


def test_retention_policy_requires_replay_safe_checkpoint_before_deletion():
    policy = (Path(__file__).parents[1] / "INDICATOR_EVENT_STREAM.md").read_text()
    assert "Events referenced by lifecycle or submission records are never\ndeleted" in policy
    assert "checkpoint" in policy.lower()


def test_duplicate_late_and_conflicting_closed_candles_are_deterministic():
    Session, engine = make_session()
    base = frame(4)
    created = stream.initialize_indicator_stream(
        base.iloc[:2], "EURUSD", "15m", 0.00001,
        analyzer=lambda _source, **_kwargs: analysis([]), session_factory=Session,
    )
    duplicate = stream.get_authoritative_structure(
        base.iloc[:2], "EURUSD", "15m", 0.00001,
        analyzer=lambda _source, **_kwargs: analysis([]), session_factory=Session,
    )
    assert duplicate["canonical_candle_count"] == created["canonical_candle_count"]
    with pytest.raises(stream.IndicatorStreamUnavailable, match="missing closed candles"):
        stream.get_authoritative_structure(
            pd.concat([base.iloc[:2], base.iloc[[3]]]), "EURUSD", "15m", 0.00001,
            analyzer=lambda _source, **_kwargs: analysis([]), session_factory=Session,
        )
    repaired = stream.get_authoritative_structure(
        base, "EURUSD", "15m", 0.00001,
        analyzer=lambda _source, **_kwargs: analysis([]), session_factory=Session,
    )
    assert repaired["stream_status"] == "READY"
    corrected = base.copy(); corrected.iloc[1, corrected.columns.get_loc("Close")] += 0.0001
    with pytest.raises(stream.IndicatorStreamUnavailable, match="conflicting closed candle"):
        stream.get_authoritative_structure(
            corrected, "EURUSD", "15m", 0.00001,
            analyzer=lambda _source, **_kwargs: analysis([]), session_factory=Session,
        )
    engine.dispose()


def test_two_initializers_converge_on_one_ready_stream():
    path = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    analyze = lambda source, **_kwargs: analysis([event(source, 5)])
    def initialize():
        return stream.initialize_indicator_stream(
            frame(), "EURUSD", "15m", 0.00001,
            analyzer=analyze, session_factory=Session,
        )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _value: initialize(), range(2)))
    assert all(result["stream_status"] == "READY" for result in results)
    assert results[0]["events"] == results[1]["events"]
    session = Session()
    assert session.query(IndicatorEvent).count() == 1
    session.close(); engine.dispose()


def test_startup_does_not_start_trading_when_stream_initialization_fails():
    with patch.object(app_bootstrap, "_restore_ctrader_selection_before_market_data"), patch.object(
        app_bootstrap, "verify_execution_protocol", return_value=True
    ), patch.object(
        app_bootstrap, "reconcile_incomplete_submissions", return_value={"ok": True}
    ), patch.object(api, "get_open_positions", return_value=[]), patch.object(
        api, "get_ctrader_market_data", return_value=frame()
    ), patch.object(
        app_bootstrap, "initialize_indicator_stream", side_effect=stream.IndicatorStreamUnavailable("broken")
    ), patch.object(api, "start_ctrader_live_price_stream") as live_stream, patch.object(
        api, "background_fetch"
    ) as background:
        app_bootstrap._start_forex_background_task()
    assert api.ENGINE_RUNTIME_STATE["indicator_stream_startup"]["ready"] is False
    live_stream.assert_not_called()
    background.assert_not_called()


def test_continuous_and_restarted_processing_create_identical_future_events():
    data = frame(18)
    def analyze(source, **_kwargs):
        events = [event(source, index) for index in range(5, len(source), 4)]
        return analysis(events)
    def process(path, restart_at=None):
        engine = create_engine(f"sqlite:///{path}")
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        result = stream.initialize_indicator_stream(
            data.iloc[:8], "EURUSD", "15m", 0.00001,
            analyzer=analyze, session_factory=Session,
        )
        for end in range(8, len(data)):
            if restart_at == end:
                engine.dispose()
                engine = create_engine(f"sqlite:///{path}")
                Session = sessionmaker(bind=engine)
                result = stream.initialize_indicator_stream(
                    data.iloc[:end], "EURUSD", "15m", 0.00001,
                    analyzer=analyze, session_factory=Session,
                )
            result = stream.get_authoritative_structure(
                data.iloc[: end + 1], "EURUSD", "15m", 0.00001,
                analyzer=analyze, session_factory=Session,
            )
        engine.dispose()
        return [item["event_identity"] for item in result["events"] if item["tradable"]]
    continuous = process(tempfile.NamedTemporaryFile(suffix=".db", delete=False).name)
    restarted = process(tempfile.NamedTemporaryFile(suffix=".db", delete=False).name, restart_at=13)
    assert restarted == continuous
