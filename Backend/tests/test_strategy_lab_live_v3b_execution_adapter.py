import ast
from pathlib import Path

import pytest

from services.live_v3b_execution_adapter import (
    V3B_BROKER_HANDOFF_ENV,
    V3B_EXECUTION_PROFILE,
    build_v3b_broker_core_payload,
    dispatch_v3b_to_live_core,
    v3b_broker_handoff_enabled,
)
from services.live_v3b_service import LIVE_V3B_MODEL


def _candidate(symbol="EURUSD", side="BUY"):
    if symbol == "XAUUSD":
        entry = 4395.50
        sl = 4400.50
        tp2 = 4386.00
        trigger = 4388.85
        protected = 4389.80
    else:
        entry = 1.10250
        sl = 1.09950
        tp2 = 1.10820
        trigger = 1.10649
        protected = 1.10592

    return {
        "symbol": symbol,
        "signal": side,
        "side": side,
        "action": side,
        "entry": entry,
        "entry_price": entry,
        "sl": sl,
        "stop_loss": sl,
        "tp1": trigger,
        "tp2": tp2,
        "protected_sl_price": protected,
        "risk_reward": 1.90,
        "risk_reward_ratio": 1.90,
        "signal_setup_id": f"setup-{symbol}-{side}",
        "setup_identity": {
            "symbol": symbol,
            "direction": side,
            "swing_type": "LOW" if side == "BUY" else "HIGH",
            "swing_timestamp": "2026-09-10T09:50:00+00:00",
            "swing_price": sl,
            "bos_candle_timestamp": "2026-09-10T10:00:00+00:00",
            "bos_level": entry,
            "confirmation_timestamp": "2026-09-10T10:10:00+00:00",
            "indicator_event_id": f"event-{symbol}",
            "m5_confirmation_id": f"confirm-{symbol}",
            "setup_timeframe": "5m",
        },
        "source_indicator_event_id": f"event-{symbol}",
        "indicator_event_identity": {"stable": symbol},
        "m5_confirmation_id": f"confirm-{symbol}",
        "m5_confirmation_identity": {"stable": f"confirm-{symbol}"},
        "confirmation_5m": {"confirmation_id": f"confirm-{symbol}"},
        "five_m_break_time": "2026-09-10T10:00:00+00:00",
        "five_m_break_close_time": "2026-09-10T10:05:00+00:00",
        "five_m_closed_candle_time": "2026-09-10T10:10:00+00:00",
        "setup_candle_time": "2026-09-10T10:10:00+00:00",
        "strategy_setup_type": f"V3B_{side}_5M_BOS_TWO_CLOSE",
        "strategy_setup_complete": True,
        "live_v3b_ready": True,
        "live_strategy_model": LIVE_V3B_MODEL,
        "protection_trigger_tp2_fraction": 0.70,
        "protected_stop_tp2_fraction": 0.60,
        "no_partial_close_at_protection_trigger": True,
    }


def test_broker_handoff_kill_switch_defaults_off():
    assert v3b_broker_handoff_enabled({}) is False
    assert v3b_broker_handoff_enabled({V3B_BROKER_HANDOFF_ENV: "false"}) is False
    assert v3b_broker_handoff_enabled({V3B_BROKER_HANDOFF_ENV: "true"}) is True


@pytest.mark.parametrize(
    "strategy_enabled,broker_handoff_enabled,live_auto_enabled,profile_supported,reason",
    [
        (False, True, True, True, "WAIT_V3B_LIVE_DISABLED"),
        (True, False, True, True, "WAIT_V3B_BROKER_HANDOFF_DISABLED"),
        (True, True, False, True, "LIVE_AUTO_OFF"),
        (True, True, True, False, "WAIT_V3B_EXECUTION_PROFILE_UNSUPPORTED"),
    ],
)
def test_executor_is_unreachable_until_all_four_gates_are_true(
    strategy_enabled,
    broker_handoff_enabled,
    live_auto_enabled,
    profile_supported,
    reason,
):
    calls = []

    def executor(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("executor must remain unreachable")

    result = dispatch_v3b_to_live_core(
        _candidate(),
        executor=executor,
        strategy_enabled=strategy_enabled,
        broker_handoff_enabled=broker_handoff_enabled,
        live_auto_enabled=live_auto_enabled,
        execution_profile_supported=profile_supported,
    )

    assert result["ok"] is False
    assert result["submitted"] is False
    assert result["reason"] == reason
    assert calls == []


def test_frozen_payload_carries_exact_v3b_management_contract():
    prepared = build_v3b_broker_core_payload(_candidate("XAUUSD", "SELL"))

    assert prepared["ok"] is True
    payload = prepared["payload"]
    assert payload["live_strategy_model"] == LIVE_V3B_MODEL
    assert payload["strategy_execution_profile"] == V3B_EXECUTION_PROFILE
    assert payload["setup_identity"]["strategy_execution_profile"] == V3B_EXECUTION_PROFILE
    assert payload["risk_reward_ratio"] == pytest.approx(1.90)
    assert payload["protection_trigger_price"] == pytest.approx(4388.85)
    assert payload["protected_sl_price"] == pytest.approx(4389.80)
    assert payload["protection_trigger_tp2_fraction"] == pytest.approx(0.70)
    assert payload["protected_stop_tp2_fraction"] == pytest.approx(0.60)
    assert payload["no_partial_close_at_protection_trigger"] is True


def test_frozen_contract_mismatch_fails_closed_before_executor():
    candidate = _candidate()
    candidate["protected_stop_tp2_fraction"] = 0.50
    called = False

    def executor(*args, **kwargs):
        nonlocal called
        called = True
        return {"ok": True}

    result = dispatch_v3b_to_live_core(
        candidate,
        executor=executor,
        strategy_enabled=True,
        broker_handoff_enabled=True,
        live_auto_enabled=True,
        execution_profile_supported=True,
    )

    assert result["ok"] is False
    assert result["reason"] == "WAIT_V3B_FROZEN_MANAGEMENT_CONTRACT"
    assert called is False


def test_all_gates_true_hands_exact_payload_to_injected_live_core_once():
    candidate = _candidate()
    calls = []

    def executor(payload, source=None):
        calls.append((payload, source))
        return {"ok": True, "position_id": "test-only"}

    result = dispatch_v3b_to_live_core(
        candidate,
        executor=executor,
        strategy_enabled=True,
        broker_handoff_enabled=True,
        live_auto_enabled=True,
        execution_profile_supported=True,
    )

    assert result["ok"] is True
    assert result["submitted"] is True
    assert len(calls) == 1
    payload, source = calls[0]
    assert source == "auto"
    assert payload["strategy_execution_profile"] == V3B_EXECUTION_PROFILE
    assert payload["source_indicator_event_id"] == "event-EURUSD"
    assert payload["signal_setup_id"] == "setup-EURUSD-BUY"
    assert payload["protected_sl_price"] == pytest.approx(candidate["protected_sl_price"])


def test_adapter_has_no_direct_broker_imports_or_calls():
    path = Path(__file__).parents[1] / "services" / "live_v3b_execution_adapter.py"
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
    called_names = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    assert not any("ctrader_connector" in name for name in imports)
    assert "place_market_order" not in called_names
    assert "modify_position_sltp" not in called_names
    assert "modify_position_stop_loss" not in called_names
    assert "close_position" not in called_names
