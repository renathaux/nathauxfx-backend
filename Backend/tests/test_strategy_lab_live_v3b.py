import ast
from pathlib import Path

import pandas as pd
import pytest

from services.live_v3b_service import (
    LIVE_V3B_ENV,
    LIVE_V3B_MODEL,
    build_live_v3b_candidate,
    build_live_v3b_execution_payload,
    live_v3b_enabled,
)
from services.paper_v3b_bridge import build_paper_v3b_candidate
from services.v3b_strategy_settings_sync import install_v3b_strategy_settings_sync


class _Strict:
    @staticmethod
    def closed_frame(frame, minutes):
        assert minutes == 5
        return frame.copy()

    @staticmethod
    def point_size(symbol):
        return 0.01 if symbol == "XAUUSD" else 0.00001


def _eur_frame():
    index = pd.to_datetime([
        "2026-09-10T10:00:00Z",
        "2026-09-10T10:05:00Z",
    ])
    return pd.DataFrame(
        {
            "Open": [1.1000, 1.1017],
            "High": [1.1020, 1.1028],
            "Low": [1.0995, 1.1015],
            "Close": [1.1018, 1.1025],
        },
        index=index,
    )


def _gold_frame():
    index = pd.to_datetime([
        "2026-09-10T11:00:00Z",
        "2026-09-10T11:05:00Z",
    ])
    return pd.DataFrame(
        {
            "Open": [4400.0, 4397.5],
            "High": [4402.0, 4398.0],
            "Low": [4396.0, 4395.0],
            "Close": [4397.0, 4395.5],
        },
        index=index,
    )


def _event(symbol):
    if symbol == "XAUUSD":
        return {
            "event_id": "smc1_gold_live_v3b",
            "symbol": "XAUUSD",
            "timeframe": "5m",
            "timestamp": "2026-09-10T11:00:00+00:00",
            "event_type": "BOS",
            "direction": "BEARISH",
            "broken_level": 4396.0,
            "broken_swing_timestamp": "2026-09-10T10:50:00Z",
            "event_invalidation_swing": {"type": "HIGH", "price": 4400.0},
            "event_identity": {"stable": "gold-live"},
            "tradable": True,
        }
    return {
        "event_id": "smc1_eur_live_v3b",
        "symbol": "EURUSD",
        "timeframe": "5m",
        "timestamp": "2026-09-10T10:00:00+00:00",
        "event_type": "BOS",
        "direction": "BULLISH",
        "broken_level": 1.1015,
        "broken_swing_timestamp": "2026-09-10T09:50:00Z",
        "event_invalidation_swing": {"type": "LOW", "price": 1.1000},
        "event_identity": {"stable": "eur-live"},
        "tradable": True,
    }


def _authority(event):
    def reader(frame, symbol, timeframe, point_size):
        assert symbol == event["symbol"]
        assert timeframe == "5m"
        return {"events": [event], "source": "test-authority"}

    return reader


def _setup_id(candidate, side):
    assert candidate["setup_identity"]["setup_timeframe"] == "5m"
    return f"setup-{candidate['symbol']}-{side}"


def test_live_v3b_feature_gate_defaults_off():
    assert live_v3b_enabled({}) is False
    assert live_v3b_enabled({LIVE_V3B_ENV: "false"}) is False
    assert live_v3b_enabled({LIVE_V3B_ENV: "true"}) is True


def test_disabled_live_v3b_does_not_read_authority():
    called = {"authority": False}

    def reader(*args, **kwargs):
        called["authority"] = True
        raise AssertionError("disabled V3B must not evaluate market authority")

    result = build_live_v3b_candidate(
        "EURUSD",
        _eur_frame(),
        strict_trader_module=_Strict,
        setup_id_builder=_setup_id,
        authoritative_reader=reader,
        enabled=False,
    )

    assert result["live_v3b_ready"] is False
    assert result["live_v3b_reason"] == "WAIT_V3B_LIVE_DISABLED"
    assert called["authority"] is False


def test_enabled_eurusd_candidate_is_live_shaped_but_not_submitted():
    result = build_live_v3b_candidate(
        "EURUSD",
        _eur_frame(),
        strict_trader_module=_Strict,
        setup_id_builder=_setup_id,
        authoritative_reader=_authority(_event("EURUSD")),
        enabled=True,
    )

    assert result["live_v3b_ready"] is True
    assert result["live_strategy_model"] == LIVE_V3B_MODEL
    assert result["mode"] == "LIVE"
    assert result["side"] == "BUY"
    assert result["entry"] == pytest.approx(result["entry_price"])
    assert result["sl"] == pytest.approx(result["stop_loss"])
    assert result["risk_reward_ratio"] == pytest.approx(1.90)
    assert result["setup_identity"]["setup_timeframe"] == "5m"
    assert result["source_indicator_event_id"] == "smc1_eur_live_v3b"
    assert result["m5_confirmation_id"].startswith("m5v3b_")
    assert result["signal_setup_id"] == "setup-EURUSD-BUY"

    handoff = build_live_v3b_execution_payload(result)
    assert handoff["ok"] is True
    payload = handoff["payload"]
    assert payload["symbol"] == "EURUSD"
    assert payload["side"] == "BUY"
    assert payload["source_indicator_event_id"] == "smc1_eur_live_v3b"
    assert payload["setup_identity"]["setup_timeframe"] == "5m"


def test_active_management_settings_reach_candidate_even_if_live_service_was_imported_early(monkeypatch):
    install_v3b_strategy_settings_sync()
    monkeypatch.setattr(
        "services.active_strategy_config_service.get_active_values",
        lambda **_kwargs: {
            "target_rr": 1.90,
            "protection_trigger_percent": 70.0,
            "protected_stop_percent": 55.0,
        },
    )
    result = build_live_v3b_candidate(
        "EURUSD", _eur_frame(), strict_trader_module=_Strict,
        setup_id_builder=_setup_id,
        authoritative_reader=_authority(_event("EURUSD")), enabled=True,
    )
    assert result["live_v3b_ready"] is True
    assert result["strategy_config"]["protected_stop_percent"] == 55.0
    assert result["protected_stop_tp2_fraction"] == pytest.approx(0.55)


def test_closed_5m_candidate_history_contract_and_durable_claim_allow_one_mock_broker_send(monkeypatch):
    from datetime import datetime, timezone

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from models import Base, ExecutionProtocolState, IndicatorEvent, IndicatorEventLifecycle
    from services import live_v3b_execution_adapter as adapter
    from services import trade_submission_service as submissions
    from services.v3b_signal_history import list_v3b_transitions, record_v3b_transition

    values = {"target_rr": 1.90, "protection_trigger_percent": 70.0, "protected_stop_percent": 55.0}
    install_v3b_strategy_settings_sync()
    monkeypatch.setattr(
        "services.active_strategy_config_service.get_active_values",
        lambda **_kwargs: dict(values),
    )
    candidate = build_live_v3b_candidate(
        "EURUSD", _eur_frame(), strict_trader_module=_Strict,
        setup_id_builder=_setup_id,
        authoritative_reader=_authority(_event("EURUSD")), enabled=True,
    )
    assert candidate["live_v3b_ready"] is True
    assert candidate["paper_entry_details"]["bos_body_ratio"] >= 0.50
    assert candidate["paper_entry_details"]["second_5m_same_direction"] is True
    assert candidate["paper_entry_details"]["second_5m_stays_beyond_bos_level"] is True
    assert candidate["setup_identity"]["setup_timeframe"] == "5m"
    assert candidate["v3b_setup_state"]["indicator_event_id"] == candidate["source_indicator_event_id"]
    assert candidate["v3b_setup_state"]["m5_confirmation_id"] == candidate["m5_confirmation_id"]
    assert candidate["v3b_setup_state"]["signal_setup_id"] == candidate["signal_setup_id"]

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    now = datetime.now(timezone.utc)
    with sessions() as session:
        session.add(IndicatorEvent(
            event_id=candidate["source_indicator_event_id"], symbol="EURUSD", timeframe="5m",
            candle_timestamp=now, classification="BOS", direction="BUY", broken_level=1.1015,
            opposite_swing=None, identity={}, payload={}, configuration_version="test",
            is_historical=False, created_at=now,
        ))
        session.add(IndicatorEventLifecycle(
            event_id=candidate["source_indicator_event_id"], mode="LIVE", owner_id="OWNER",
            account_id="47810571", status="ELIGIBLE", updated_at=now,
        ))
        session.add(ExecutionProtocolState(
            singleton_id=1, protocol_version=submissions.EXECUTION_PROTOCOL_VERSION,
            updated_at=now,
        ))
        session.commit()

    scope = "CTRADER:DEMO:47810571"
    record_v3b_transition(scope, "EURUSD", "WAIT", "2026-09-10T09:55:00Z", session_factory=sessions)
    record_v3b_transition(
        scope, "EURUSD", "BUY", candidate["five_m_closed_candle_time"],
        event_id=candidate["source_indicator_event_id"],
        confirmation_id=candidate["m5_confirmation_id"],
        setup_id=candidate["signal_setup_id"], session_factory=sessions,
    )
    broker_sends = []

    def guarded_core(payload, source):
        assert source == "auto"
        assert payload["signal_setup_id"] == candidate["signal_setup_id"]
        claim = submissions.claim_submission(
            payload["source_indicator_event_id"], "LIVE", "47810571", "EURUSD",
            payload["signal_setup_id"], payload, session_factory=sessions,
        )
        if not claim["ok"]:
            return {"ok": False, "reason": claim["reason"]}
        assert submissions.mark_request_started(claim["idempotency_key"], session_factory=sessions)
        broker_sends.append(payload)
        assert submissions.complete_submission(
            claim["idempotency_key"],
            {"ok": True, "broker_result": "ACCEPTED", "order_id": "mock-order"},
            session_factory=sessions,
        )
        return {"ok": True, "broker_result": "ACCEPTED"}

    try:
        for _ in range(2):
            result = adapter.dispatch_v3b_to_live_core(
                candidate, executor=guarded_core, live_auto_enabled=True,
                strategy_enabled=True, broker_handoff_enabled=True,
                execution_profile_supported=True,
                authoritative_live_state_loader=lambda **_kwargs: {"live_enabled": True},
            )
            record_v3b_transition(
                scope, "EURUSD", "BUY", candidate["five_m_closed_candle_time"],
                setup_id=candidate["signal_setup_id"],
                execution_status="EXECUTED" if result["ok"] else "BLOCKED",
                reason=result["reason"], session_factory=sessions,
            )
        rows = list_v3b_transitions(scope, session_factory=sessions)
        assert len(broker_sends) == 1
        assert broker_sends[0]["source_indicator_event_id"] == candidate["v3b_setup_state"]["indicator_event_id"]
        assert broker_sends[0]["m5_confirmation_id"] == candidate["v3b_setup_state"]["m5_confirmation_id"]
        assert broker_sends[0]["entry"] == candidate["v3b_setup_state"]["entry"]
        assert broker_sends[0]["sl"] == candidate["v3b_setup_state"]["sl"]
        assert broker_sends[0]["tp1"] == candidate["v3b_setup_state"]["tp1"]
        assert broker_sends[0]["tp2"] == candidate["v3b_setup_state"]["tp2"]
        assert len(rows) == 2
        assert rows[0]["signal"] == "BUY"
        assert rows[0]["execution_status"] == "EXECUTED"
        assert rows[0]["event_id"] == candidate["source_indicator_event_id"]
        assert rows[0]["m5_confirmation_id"] == candidate["m5_confirmation_id"]
    finally:
        engine.dispose()


def test_enabled_gold_candidate_preserves_frozen_gold_geometry():
    result = build_live_v3b_candidate(
        "XAUUSD",
        _gold_frame(),
        strict_trader_module=_Strict,
        setup_id_builder=_setup_id,
        authoritative_reader=_authority(_event("XAUUSD")),
        enabled=True,
    )

    assert result["live_v3b_ready"] is True
    assert result["side"] == "SELL"
    assert result["stop_loss"] == pytest.approx(4400.50)
    assert result["tp2"] == pytest.approx(4386.00)
    assert result["tp1"] == pytest.approx(4388.85)
    assert result["protected_sl_price"] == pytest.approx(4389.80)
    assert result["risk_reward_ratio"] == pytest.approx(1.90)


def test_live_candidate_fails_closed_without_setup_fingerprint():
    result = build_live_v3b_candidate(
        "EURUSD",
        _eur_frame(),
        strict_trader_module=_Strict,
        setup_id_builder=lambda candidate, side: None,
        authoritative_reader=_authority(_event("EURUSD")),
        enabled=True,
    )

    assert result["live_v3b_ready"] is False
    assert result["live_v3b_reason"] == "WAIT_V3B_LIVE_SETUP_ID"
    assert result["live_v3b_details"]["source_candidate"]["v3b_setup_state"]["signal"] == "BUY"
    assert result["live_v3b_details"]["source_candidate"]["v3b_setup_state"]["execution_status"] == "BLOCKED"


def test_live_final_gate_can_block_without_broker_handoff():
    result = build_live_v3b_candidate(
        "EURUSD",
        _eur_frame(),
        strict_trader_module=_Strict,
        setup_id_builder=_setup_id,
        authoritative_reader=_authority(_event("EURUSD")),
        final_gate=lambda candidate, side: {
            "ok": False,
            "reason": "WAIT_TEST_EXECUTION_SAFETY",
        },
        enabled=True,
    )

    assert result["live_v3b_ready"] is False
    assert result["signal"] == "WAIT"
    assert result["live_v3b_reason"] == "WAIT_TEST_EXECUTION_SAFETY"
    assert result["v3b_setup_state"]["signal"] == "BUY"
    assert result["v3b_setup_state"]["lifecycle_state"] == "BLOCKED"
    assert result["v3b_setup_state"]["execution_block_reason"] == "WAIT_TEST_EXECUTION_SAFETY"
    handoff = build_live_v3b_execution_payload(result)
    assert handoff["ok"] is False
    assert handoff["payload"] is None


def test_live_v3b_service_has_no_broker_execution_imports_or_calls():
    path = Path(__file__).parents[1] / "services" / "live_v3b_service.py"
    source = path.read_text()
    tree = ast.parse(source)
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    imports |= {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    called = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            called.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            called.add(node.func.attr)

    assert not any("ctrader_connector" in name for name in imports)
    assert not {
        "place_market_order",
        "execute_live_order_core",
        "modify_position",
        "modify_position_sltp",
        "modify_position_stop_loss",
        "close_position",
    } & called


def test_paper_and_live_share_the_same_refreshed_authoritative_event_source():
    frame = _eur_frame()
    event = _event("EURUSD")
    calls = []

    def updater(source, symbol, timeframe, point_size, **kwargs):
        calls.append((symbol, timeframe, source.index[-1], kwargs.get("analyzer")))
        return {
            "events": [event],
            "source": "authoritative_indicator_event_stream",
            "stream_status": "READY",
            "stream_last_candle": source.index[-1].isoformat(),
        }

    paper = build_paper_v3b_candidate(
        "EURUSD", frame, strict_trader_module=_Strict,
        authoritative_updater=updater,
    )
    live = build_live_v3b_candidate(
        "EURUSD", frame, strict_trader_module=_Strict,
        setup_id_builder=_setup_id,
        authoritative_updater=updater,
        enabled=True,
    )

    assert paper["paper_entry_ready"] is True
    assert live["live_v3b_ready"] is True
    assert paper["source_indicator_event_id"] == live["source_indicator_event_id"]
    assert paper["m5_confirmation_id"] == live["m5_confirmation_id"]
    assert [call[:2] for call in calls] == [("EURUSD", "5m"), ("EURUSD", "5m")]
    assert all(call[3] is not None for call in calls)
