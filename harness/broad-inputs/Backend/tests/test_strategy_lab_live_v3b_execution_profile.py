import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from services.live_v3b_execution_adapter import (
    build_v3b_broker_core_payload,
    dispatch_v3b_to_live_core,
)
from services.live_v3b_execution_profile import (
    V3B_EXECUTION_PROFILE,
    validate_frozen_management_contract,
    validate_v3b_locked_entry_state,
)


def candidate():
    return {
        "symbol": "EURUSD",
        "side": "BUY",
        "action": "BUY",
        "signal": "BUY",
        "entry": 1.1000,
        "entry_price": 1.1000,
        "sl": 1.0900,
        "stop_loss": 1.0900,
        "tp1": 1.1133,
        "tp2": 1.1190,
        "protected_sl_price": 1.1114,
        "risk_reward": "1:1.9",
        "risk_reward_ratio": 1.90,
        "signal_setup_id": "setup-v3b-1",
        "source_indicator_event_id": "event-v3b-1",
        "indicator_event_identity": {"event_id": "event-v3b-1"},
        "m5_confirmation_id": "m5v3b-confirmation-1",
        "m5_confirmation_identity": {"id": "m5v3b-confirmation-1"},
        "five_m_break_time": "2026-09-11T12:00:00+00:00",
        "five_m_break_close_time": "2026-09-11T12:05:00+00:00",
        "five_m_closed_candle_time": "2026-09-11T12:10:00+00:00",
        "setup_candle_time": "2026-09-11T12:10:00+00:00",
        "strategy_setup_type": "PAPER_BUY_V3B_M5",
        "strategy_setup_complete": True,
        "live_strategy_model": "LIVE_V3B_M5_FROZEN",
        "live_v3b_ready": True,
        "protection_trigger_tp2_fraction": 0.70,
        "protected_stop_tp2_fraction": 0.60,
        "no_partial_close_at_protection_trigger": True,
        "setup_identity": {
            "symbol": "EURUSD",
            "direction": "BUY",
            "swing_type": "HIGH",
            "swing_timestamp": "2026-09-11T11:40:00+00:00",
            "swing_price": 1.0990,
            "bos_candle_timestamp": "2026-09-11T12:00:00+00:00",
            "bos_level": 1.0990,
            "confirmation_timestamp": "2026-09-11T12:10:00+00:00",
            "indicator_event_id": "event-v3b-1",
            "m5_confirmation_id": "m5v3b-confirmation-1",
            "setup_timeframe": "5m",
        },
    }


def rounded_contract_payload(symbol, entry, sl, tp1, tp2, protected):
    side = "BUY" if tp2 > entry else "SELL"
    return {
        "symbol": symbol,
        "side": side,
        "action": side,
        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "protection_trigger_price": tp1,
        "tp2": tp2,
        "protected_sl_price": protected,
        "risk_reward": "1:1.9",
        "risk_reward_ratio": 1.90,
        "protection_trigger_tp2_fraction": 0.70,
        "protected_stop_tp2_fraction": 0.60,
        "no_partial_close_at_protection_trigger": True,
        "strategy_execution_profile": V3B_EXECUTION_PROFILE,
        "setup_identity": {
            "strategy_execution_profile": V3B_EXECUTION_PROFILE,
            "setup_timeframe": "5m",
        },
    }


class FrozenV3BProfileTests(unittest.TestCase):
    def test_broker_payload_is_explicitly_stamped_and_frozen(self):
        result = build_v3b_broker_core_payload(candidate())
        self.assertTrue(result["ok"])
        payload = result["payload"]
        self.assertEqual(payload["strategy_execution_profile"], V3B_EXECUTION_PROFILE)
        self.assertEqual(
            payload["setup_identity"]["strategy_execution_profile"],
            V3B_EXECUTION_PROFILE,
        )
        self.assertEqual(payload["protection_trigger_price"], 1.1133)
        self.assertEqual(payload["protected_sl_price"], 1.1114)
        self.assertTrue(payload["no_partial_close_at_protection_trigger"])
        self.assertTrue(validate_frozen_management_contract(payload)["ok"])

    def test_real_eurusd_rounded_levels_pass_frozen_contract(self):
        payload = rounded_contract_payload(
            "EURUSD",
            entry=1.15482,
            sl=1.15628,
            tp1=1.15288,
            tp2=1.15205,
            protected=1.15316,
        )
        result = validate_frozen_management_contract(payload)
        self.assertTrue(result["ok"], result)
        self.assertEqual(
            result["details"]["expected_rounded_levels"]["tp2"],
            1.15205,
        )

    def test_real_xauusd_rounded_levels_pass_frozen_contract(self):
        payload = rounded_contract_payload(
            "XAUUSD",
            entry=4305.99,
            sl=4318.57,
            tp1=4289.26,
            tp2=4282.09,
            protected=4291.65,
        )
        result = validate_frozen_management_contract(payload)
        self.assertTrue(result["ok"], result)
        self.assertEqual(
            result["details"]["expected_rounded_levels"]["tp2"],
            4282.09,
        )

    def test_rounded_contract_still_rejects_real_geometry_drift(self):
        payload = rounded_contract_payload(
            "EURUSD",
            entry=1.15482,
            sl=1.15628,
            tp1=1.15288,
            tp2=1.15205,
            protected=1.15316,
        )
        payload["tp2"] = 1.15180
        result = validate_frozen_management_contract(payload)
        self.assertFalse(result["ok"])
        self.assertFalse(result["details"]["checks"]["tp2_rounded_geometry"])

    def test_rounded_contract_rejects_declared_strategy_drift(self):
        payload = rounded_contract_payload(
            "XAUUSD",
            entry=4305.99,
            sl=4318.57,
            tp1=4289.26,
            tp2=4282.09,
            protected=4291.65,
        )
        payload["risk_reward_ratio"] = 2.0
        result = validate_frozen_management_contract(payload)
        self.assertFalse(result["ok"])
        self.assertFalse(result["details"]["checks"]["declared_target_rr"])

    def test_all_legacy_switches_still_cannot_reach_unaware_executor(self):
        executor = Mock(return_value={"ok": True})
        result = dispatch_v3b_to_live_core(
            candidate(),
            executor=executor,
            live_auto_enabled=True,
            strategy_enabled=True,
            broker_handoff_enabled=True,
            execution_profile_supported=False,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "WAIT_V3B_EXECUTION_PROFILE_UNSUPPORTED")
        executor.assert_not_called()

    def test_profile_aware_executor_can_only_be_reached_after_all_four_gates(self):
        executor = Mock(return_value={"ok": True, "position_id": "demo-only"})
        with patch(
            "services.live_v3b_execution_adapter.load_auto_trade_state",
            return_value={"paper_enabled": False, "live_enabled": True},
        ) as durable_state:
            result = dispatch_v3b_to_live_core(
                candidate(),
                executor=executor,
                live_auto_enabled=True,
                strategy_enabled=True,
                broker_handoff_enabled=True,
                execution_profile_supported=True,
            )
        self.assertTrue(result["ok"])
        self.assertTrue(result["submitted"])
        durable_state.assert_called_once_with(force_refresh=True)
        executor.assert_called_once()
        payload = executor.call_args.args[0]
        self.assertEqual(payload["strategy_execution_profile"], V3B_EXECUTION_PROFILE)
        self.assertEqual(executor.call_args.kwargs["source"], "auto")

    def test_locked_v3b_gate_accepts_5m_identity_without_15m_ema_fields(self):
        prepared = build_v3b_broker_core_payload(candidate())["payload"]
        result = validate_v3b_locked_entry_state(
            "EURUSD",
            "BUY",
            prepared,
            [],
            active_trade=None,
            now=datetime(2026, 9, 11, 12, 11, tzinfo=timezone.utc),
            last_closed_at=0,
            cooldown_seconds=900,
            setup_id_builder=lambda payload, side: "setup-v3b-1",
            lifecycle={"status": "ELIGIBLE"},
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["details"]["v1_15m_ema_bypassed"])
        self.assertTrue(result["details"]["v1_consolidation_bypassed"])

    def test_locked_v3b_gate_rejects_wrong_timeframe_and_active_position(self):
        prepared = build_v3b_broker_core_payload(candidate())["payload"]
        prepared["setup_identity"]["setup_timeframe"] = "15m"
        wrong_timeframe = validate_v3b_locked_entry_state(
            "EURUSD",
            "BUY",
            prepared,
            [],
            now=datetime(2026, 9, 11, 12, 11, tzinfo=timezone.utc),
        )
        self.assertFalse(wrong_timeframe["ok"])
        self.assertEqual(wrong_timeframe["reason"], "WAIT_V3B_5M_IDENTITY")

        prepared = build_v3b_broker_core_payload(candidate())["payload"]
        active = validate_v3b_locked_entry_state(
            "EURUSD",
            "BUY",
            prepared,
            [],
            active_trade={"symbol": "EURUSD", "status": "OPEN"},
            now=datetime(2026, 9, 11, 12, 11, tzinfo=timezone.utc),
        )
        self.assertFalse(active["ok"])
        self.assertEqual(active["reason"], "active position exists")

    def test_locked_v3b_gate_rejects_future_or_consumed_event(self):
        prepared = build_v3b_broker_core_payload(candidate())["payload"]
        future = validate_v3b_locked_entry_state(
            "EURUSD",
            "BUY",
            prepared,
            [],
            now=datetime(2026, 9, 11, 12, 9, tzinfo=timezone.utc),
        )
        self.assertFalse(future["ok"])
        self.assertEqual(future["reason"], "setup contains a future candle close")

        consumed = validate_v3b_locked_entry_state(
            "EURUSD",
            "BUY",
            prepared,
            [],
            now=datetime(2026, 9, 11, 12, 11, tzinfo=timezone.utc),
            lifecycle={"status": "CONSUMED"},
        )
        self.assertFalse(consumed["ok"])
        self.assertIn("CONSUMED", consumed["reason"])


if __name__ == "__main__":
    unittest.main()
