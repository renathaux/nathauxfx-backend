from copy import deepcopy
from unittest.mock import patch

import pytest

from services import live_v3b_execution_profile as profile
from services.v3b_strategy_settings_sync import install_v3b_strategy_settings_sync


def _payload(symbol, side, entry, sl, values):
    sign = 1 if side == "BUY" else -1
    risk = abs(entry - sl)
    digits = 5 if symbol == "EURUSD" else 2
    raw_tp2 = entry + sign * values["target_rr"] * risk
    return {
        "symbol": symbol,
        "side": side,
        "entry": entry,
        "sl": sl,
        "tp2": round(raw_tp2, digits),
        "protection_trigger_price": round(
            entry + (raw_tp2 - entry) * values["protection_trigger_percent"] / 100, digits
        ),
        "protected_sl_price": round(
            entry + (raw_tp2 - entry) * values["protected_stop_percent"] / 100, digits
        ),
        "risk_reward_ratio": values["target_rr"],
        "protection_trigger_tp2_fraction": values["protection_trigger_percent"] / 100,
        "protected_stop_tp2_fraction": values["protected_stop_percent"] / 100,
        "no_partial_close_at_protection_trigger": True,
        "strategy_execution_profile": "V3B_M5_FROZEN",
        "strategy_config": dict(values),
        "strategy_config_profile": "V3B_M5_FROZEN",
        "signal_setup_id": "setup-1",
        "source_indicator_event_id": "event-1",
        "m5_confirmation_id": "confirmation-1",
    }


@pytest.mark.parametrize(
    "symbol,side,entry,sl",
    [("EURUSD", "BUY", 1.14644, 1.14533), ("XAUUSD", "SELL", 4316.54, 4328.77)],
)
def test_correct_rounded_geometry_uses_current_config_and_tick_precision(symbol, side, entry, sl):
    install_v3b_strategy_settings_sync()
    values = {"target_rr": 1.90, "protection_trigger_percent": 70.0, "protected_stop_percent": 55.0}
    payload = _payload(symbol, side, entry, sl, values)
    with patch("services.active_strategy_config_service.get_active_values", return_value=values):
        assert profile.validate_frozen_management_contract(payload)["ok"] is True
        quantum = 0.00001 if symbol == "EURUSD" else 0.01
        for field in ("tp2", "protection_trigger_price", "protected_sl_price"):
            drifted = deepcopy(payload)
            drifted[field] = round(drifted[field] + quantum, 5 if symbol == "EURUSD" else 2)
            assert profile.validate_frozen_management_contract(drifted)["ok"] is False
        stale = deepcopy(payload)
        stale["strategy_config"]["protected_stop_percent"] = 60.0
        assert profile.validate_frozen_management_contract(stale)["ok"] is False
        wrong_declaration = deepcopy(payload)
        wrong_declaration["risk_reward_ratio"] = 2.0
        assert profile.validate_frozen_management_contract(wrong_declaration)["ok"] is False


def test_eurusd_rounding_sensitive_example_accepts_1_14855():
    install_v3b_strategy_settings_sync()
    values = {"target_rr": 1.90, "protection_trigger_percent": 70.0, "protected_stop_percent": 55.0}
    payload = _payload("EURUSD", "BUY", 1.14644, 1.14533, values)
    assert payload["tp2"] == 1.14855
    with patch("services.active_strategy_config_service.get_active_values", return_value=values):
        assert profile.validate_frozen_management_contract(payload)["ok"] is True
