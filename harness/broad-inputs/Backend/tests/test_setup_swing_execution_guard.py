import unittest
from unittest.mock import patch

import pandas as pd

from services.setup_swing_execution_guard import validate_fresh_setup_swing_identity
from strategies import strict_trader


class StableSetupSwingExecutionGuardTests(unittest.TestCase):
    def _full_history(self):
        index = pd.date_range(
            "2026-09-02T05:00:00Z",
            periods=10,
            freq="15min",
        )
        rows = [
            (1.1002, 1.1005, 1.1000, 1.1003),
            (1.1001, 1.1004, 1.0998, 1.1000),
            (1.0998, 1.1002, 1.0988, 1.0995),
            (1.0996, 1.1006, 1.0995, 1.1002),
            (1.1001, 1.1008, 1.0999, 1.1005),
            (1.1017, 1.1020, 1.1016, 1.1018),
            (1.1007, 1.1009, 1.1002, 1.1005),
            (1.1004, 1.1007, 1.1000, 1.1002),
            (1.1003, 1.1008, 1.1001, 1.1004),
            (1.1002, 1.1006, 1.1000, 1.1003),
        ]
        return pd.DataFrame(
            rows,
            columns=["Open", "High", "Low", "Close"],
            index=index,
        )

    def test_short_window_does_not_reject_already_qualified_exact_pivot(self):
        full = self._full_history()
        target_time = full.index[5].isoformat()
        full_valid = strict_trader.detect_valid_swings(full, "EURUSD")
        self.assertTrue(
            any(
                swing.get("type") == "HIGH"
                and swing.get("time") == target_time
                and abs(float(swing.get("price")) - 1.1020) < 1e-12
                for swing in full_valid
            )
        )

        # Simulate the final execution gate's shorter fresh window. The prior
        # LOW that qualified the HIGH has fallen outside the window, so legacy
        # detect_valid_swings re-qualification loses the already-valid pivot.
        truncated = full.iloc[3:].copy()
        truncated_valid = strict_trader.detect_valid_swings(
            truncated,
            "EURUSD",
        )
        self.assertFalse(
            any(
                swing.get("type") == "HIGH"
                and swing.get("time") == target_time
                for swing in truncated_valid
            )
        )

        result = validate_fresh_setup_swing_identity(
            truncated,
            "EURUSD",
            {
                "swing_type": "HIGH",
                "swing_timestamp": target_time,
                "swing_price": 1.1020,
            },
            strict_trader,
        )

        self.assertTrue(result["ok"])
        self.assertIsNone(result["reason"])
        self.assertTrue(result["details"]["fresh_setup_swing_matched"])
        self.assertEqual(
            result["details"]["fresh_setup_swing_match_method"],
            "smc_indicator_confirmed_pivot_identity",
        )

    def test_changed_pivot_price_still_blocks_execution(self):
        truncated = self._full_history().iloc[3:].copy()
        target_time = truncated.index[2].isoformat()
        result = validate_fresh_setup_swing_identity(
            truncated,
            "EURUSD",
            {
                "swing_type": "HIGH",
                "swing_timestamp": target_time,
                "swing_price": 1.1015,
            },
            strict_trader,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(
            result["reason"],
            "WAIT_SETUP_SWING_CHANGED_BEFORE_EXECUTION",
        )
        self.assertFalse(result["details"]["fresh_setup_swing_matched"])

    def test_durable_event_allows_delayed_execution_after_pivot_leaves_frame(self):
        truncated = self._full_history().iloc[-5:].copy()
        setup = {
            "indicator_event_id": "smc1-delayed-eurusd",
            "swing_type": "LOW",
            "swing_timestamp": "2026-09-02T11:45:00+00:00",
            "swing_price": 1.16218,
        }
        durable_event = {
            "event_id": "smc1-delayed-eurusd",
            "symbol": "EURUSD",
            "timeframe": "15m",
            "tradable": True,
            "direction": "BEARISH",
            "broken_swing_timestamp": "2026-09-02T11:45:00+00:00",
            "broken_level": 1.16218,
        }
        with patch(
            "services.setup_swing_execution_guard.read_authoritative_event",
            return_value=durable_event,
        ):
            result = validate_fresh_setup_swing_identity(
                truncated, "EURUSD", setup, strict_trader
            )

        self.assertTrue(result["ok"])
        self.assertEqual(
            result["details"]["fresh_setup_swing_match_method"],
            "durable_indicator_event_identity",
        )

    def test_v3b_durable_5m_event_is_allowed_only_when_identity_declares_5m(self):
        setup = {
            "indicator_event_id": "smc1-v3b-5m",
            "setup_timeframe": "5m",
            "swing_type": "HIGH",
            "swing_timestamp": "2026-09-10T09:50:00+00:00",
            "swing_price": 1.1015,
        }
        durable_event = {
            "event_id": "smc1-v3b-5m",
            "symbol": "EURUSD",
            "timeframe": "5m",
            "tradable": True,
            "direction": "BULLISH",
            "broken_swing_timestamp": setup["swing_timestamp"],
            "broken_level": 1.1015,
        }
        with patch(
            "services.setup_swing_execution_guard.read_authoritative_event",
            return_value=durable_event,
        ):
            result = validate_fresh_setup_swing_identity(
                None, "EURUSD", setup, strict_trader
            )

        self.assertTrue(result["ok"])
        self.assertEqual(
            result["details"]["fresh_setup_matched_swing"]["timeframe"],
            "5m",
        )

        legacy_identity = dict(setup)
        legacy_identity.pop("setup_timeframe")
        with patch(
            "services.setup_swing_execution_guard.read_authoritative_event",
            return_value=durable_event,
        ):
            blocked = validate_fresh_setup_swing_identity(
                None, "EURUSD", legacy_identity, strict_trader
            )
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["reason"], "WAIT_SETUP_SWING_CHANGED_BEFORE_EXECUTION")

    def test_durable_event_mismatch_fails_closed_without_raw_fallback(self):
        setup = {
            "indicator_event_id": "smc1-mismatch",
            "swing_type": "HIGH",
            "swing_timestamp": "2026-09-02T11:45:00+00:00",
            "swing_price": 1.16218,
        }
        with patch(
            "services.setup_swing_execution_guard.read_authoritative_event",
            return_value={"symbol": "EURUSD", "timeframe": "15m", "tradable": True,
                          "direction": "BULLISH", "broken_swing_timestamp": setup["swing_timestamp"],
                          "broken_level": 1.16221},
        ):
            result = validate_fresh_setup_swing_identity(
                self._full_history(), "EURUSD", setup, strict_trader
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "WAIT_SETUP_SWING_CHANGED_BEFORE_EXECUTION")


if __name__ == "__main__":
    unittest.main()
