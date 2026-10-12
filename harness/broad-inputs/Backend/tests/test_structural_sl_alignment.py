import unittest

from strategies.strict_trader import select_structural_stop_loss


class StructuralStopAlignmentTests(unittest.TestCase):
    def swing(self, swing_type, price, *, swing_time="2026-08-24T10:00:00Z", confirmation_time="2026-08-24T10:30:00Z"):
        return {
            "type": swing_type,
            "price": price,
            "swing_time": swing_time,
            "swing_index": 10,
            "confirmation_time": confirmation_time,
            "confirmation_index": 12,
            "source": "CURRENT_LEG",
        }

    def test_eurusd_buy_uses_event_owned_low(self):
        result = select_structural_stop_loss(
            self.swing("LOW", 1.16400), "BUY", 1.16600, "EURUSD",
            setup_break_time="2026-08-24T11:00:00Z",
            minimum_sl_distance_points=100,
        )
        self.assertTrue(result["ok"])
        self.assertAlmostEqual(result["stop_loss"], 1.16350)
        self.assertEqual(result["sl_structure_source"], "event_owned_15m_smc_swing")

    def test_eurusd_sell_uses_event_owned_high(self):
        result = select_structural_stop_loss(
            self.swing("HIGH", 1.16747), "SELL", 1.16588, "EURUSD",
            setup_break_time="2026-08-24T11:00:00Z",
            minimum_sl_distance_points=100,
        )
        self.assertTrue(result["ok"])
        self.assertAlmostEqual(result["stop_loss"], 1.16797)

    def test_minimum_distance_blocks_without_search_or_manufacture(self):
        result = select_structural_stop_loss(
            self.swing("LOW", 1.16560), "BUY", 1.16600, "EURUSD",
            setup_break_time="2026-08-24T11:00:00Z",
            minimum_sl_distance_points=100,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "WAIT_SL_TOO_SMALL")
        self.assertNotIn("stop_loss", result)

    def test_missing_event_swing_does_not_fall_back(self):
        result = select_structural_stop_loss(
            None, "BUY", 1.16600, "EURUSD",
            setup_break_time="2026-08-24T11:00:00Z",
            minimum_sl_distance_points=100,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "WAIT_NO_STRUCTURAL_SL_SWING")

    def test_wrong_event_swing_type_is_rejected(self):
        result = select_structural_stop_loss(
            self.swing("HIGH", 1.16400), "BUY", 1.16600, "EURUSD",
            setup_break_time="2026-08-24T11:00:00Z",
            minimum_sl_distance_points=100,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "WAIT_STRUCTURAL_SL_SWING_TYPE_MISMATCH")

    def test_swing_after_setup_is_rejected(self):
        result = select_structural_stop_loss(
            self.swing("LOW", 1.16400, swing_time="2026-08-24T12:00:00Z"),
            "BUY", 1.16600, "EURUSD",
            setup_break_time="2026-08-24T11:00:00Z",
            minimum_sl_distance_points=100,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "WAIT_SL_SWING_AFTER_SETUP")

    def test_confirmation_after_setup_is_rejected(self):
        result = select_structural_stop_loss(
            self.swing("LOW", 1.16400, confirmation_time="2026-08-24T12:00:00Z"),
            "BUY", 1.16600, "EURUSD",
            setup_break_time="2026-08-24T11:00:00Z",
            minimum_sl_distance_points=100,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "WAIT_SL_SWING_CONFIRMED_AFTER_SETUP")

    def test_xau_event_low_uses_existing_five_pip_buffer(self):
        result = select_structural_stop_loss(
            self.swing("LOW", 4638.95), "BUY", 4659.50, "XAUUSD",
            setup_break_time="2026-08-24T11:30:00Z",
            minimum_sl_distance_points=100,
        )
        self.assertTrue(result["ok"])
        self.assertAlmostEqual(result["stop_loss"], 4638.45)


if __name__ == "__main__":
    unittest.main()
