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

    assert not any("ctrader_connector" in name for name in imports)
    assert "place_market_order(" not in source
    assert "execute_live_order_core(" not in source
    assert "modify_position" not in source
    assert "close_position(" not in source
