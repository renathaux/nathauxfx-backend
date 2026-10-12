import unittest
from unittest.mock import Mock, patch

import pandas as pd

from strategies.xauusd_risk_guard import _event_owned_15m_stop


class XauusdRiskGuardTests(unittest.TestCase):
    def frame(self, count=12):
        index = pd.date_range("2026-08-17T00:00:00Z", periods=count, freq="15min")
        rows = [(4650.0, 4660.0, 4640.0, 4655.0) for _ in range(count)]
        return pd.DataFrame(rows, index=index, columns=["Open", "High", "Low", "Close"])

    def swing(self, swing_type, price, *, swing_time="2026-08-17T01:30:00+00:00",
              confirmation_time="2026-08-17T01:45:00+00:00"):
        return {
            "type": swing_type, "price": price, "swing_time": swing_time,
            "swing_index": 6, "confirmation_time": confirmation_time,
            "confirmation_index": 7, "source": "CURRENT_LEG",
        }

    def test_buy_stop_is_500_quote_points_below_event_owned_15m_swing(self):
        result = _event_owned_15m_stop(
            self.frame(), "BUY", 4649.0, 100,
            event_invalidation_swing=self.swing("LOW", 4648.0),
            setup_break_time="2026-08-17T02:00:00+00:00")
        self.assertTrue(result["ok"])
        self.assertEqual(result["sl_structure_source"], "event_owned_15m_smc_swing")
        self.assertAlmostEqual(result["swing"]["price"], 4648.0)
        self.assertAlmostEqual(result["stop_loss"], 4643.0)
        self.assertAlmostEqual(result["buffer_pips"], 50.0)
        self.assertAlmostEqual(result["distance"], 6.0)
        self.assertAlmostEqual(result["distance_points"], 600.0)

    def test_sell_stop_is_500_quote_points_above_event_owned_15m_swing(self):
        result = _event_owned_15m_stop(
            self.frame(), "SELL", 4640.0, 100,
            event_invalidation_swing=self.swing("HIGH", 4650.0),
            setup_break_time="2026-08-17T02:00:00+00:00")
        self.assertTrue(result["ok"])
        self.assertAlmostEqual(result["swing"]["price"], 4650.0)
        self.assertAlmostEqual(result["stop_loss"], 4655.0)

    def test_minimum_distance_does_not_manufacture_a_stop(self):
        result = _event_owned_15m_stop(
            self.frame(), "BUY", 4658.0, 600,
            event_invalidation_swing=self.swing("LOW", 4657.8),
            setup_break_time="2026-08-17T02:00:00+00:00")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "WAIT_SL_TOO_SMALL")
        self.assertNotIn("stop_loss", result)

    def test_missing_event_swing_does_not_fall_back(self):
        result = _event_owned_15m_stop(
            self.frame(), "BUY", 4658.0, 100,
            event_invalidation_swing=None,
            setup_break_time="2026-08-17T02:00:00+00:00")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "WAIT_NO_STRUCTURAL_SL_SWING")

    def test_target_event_swing_is_consumed_without_reanalysis(self):
        result = _event_owned_15m_stop(
            self.frame(), "BUY", 4659.5, 100,
            event_invalidation_swing=self.swing("LOW", 4638.95),
            setup_break_time="2026-08-17T02:00:00+00:00")
        self.assertTrue(result["ok"])
        self.assertEqual(result["stop_loss"], 4633.95)

    def test_confirmation_after_break_is_rejected(self):
        result = _event_owned_15m_stop(
            self.frame(), "BUY", 4659.5, 100,
            event_invalidation_swing=self.swing(
                "LOW", 4638.95, confirmation_time="2026-08-17T02:15:00+00:00"),
            setup_break_time="2026-08-17T02:00:00+00:00")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "WAIT_SL_SWING_CONFIRMED_AFTER_SETUP")

    def test_eurusd_risk_builder_remains_the_original_generic_path(self):
        import brain
        wrapper = brain.build_risk_levels_with_xauusd_15m
        original = Mock(return_value={"ok": True, "source": "generic_eurusd"})
        with patch.dict(wrapper.__globals__, {"_original_build_risk_levels": original}):
            result = wrapper(
                self.frame(), "BUY", 1.1000, "EURUSD",
                setup_break_time="2026-08-17T01:00:00Z",
                execution_settings={"minimum_sl_distance_points": 100})
        self.assertEqual(result, {"ok": True, "source": "generic_eurusd"})
        original.assert_called_once()


if __name__ == "__main__":
    unittest.main()
