import unittest
from unittest.mock import patch

import pandas as pd

from services import setup_swing_execution_guard as guard
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

    def _today_eurusd_event(self):
        return {
            "event_id": "smc1_today_eurusd_choch",
            "symbol": "EURUSD",
            "timeframe": "15m",
            "candle_timestamp": "2026-09-10T11:45:00+00:00",
            "classification": "CHOCH",
            "direction": "BEARISH",
            "broken_level": 1.16218,
            "is_historical": False,
            "payload": {
                "event_type": "CHOCH",
                "direction": "BEARISH",
                "timestamp": "2026-09-10T11:45:00+00:00",
                "broken_level": 1.16218,
                # The real source pivot can be much older than the short
                # execution-time candle window.
                "broken_swing_timestamp": "2026-09-09T10:00:00+00:00",
            },
        }

    def _today_setup_identity(self, **overrides):
        identity = {
            "symbol": "EURUSD",
            "direction": "SELL",
            "swing_type": "LOW",
            "swing_timestamp": "2026-09-09T10:00:00+00:00",
            "swing_price": 1.16218,
            "bos_candle_timestamp": "2026-09-10T11:45:00+00:00",
            "bos_level": 1.16218,
            "confirmation_timestamp": "2026-09-10T12:05:00+00:00",
            "indicator_event_id": "smc1_today_eurusd_choch",
            "m5_confirmation_id": "m5_today_confirm",
        }
        identity.update(overrides)
        return identity

    def test_authoritative_event_keeps_old_source_pivot_valid_outside_fresh_window(self):
        # This fresh execution window starts on Sep 10, so the Sep 9 source
        # pivot is intentionally absent. The immutable event remains authority.
        closed_15m = pd.DataFrame(
            {
                "Open": [1.1625] * 8,
                "High": [1.1630] * 8,
                "Low": [1.1590] * 8,
                "Close": [1.1600] * 8,
            },
            index=pd.date_range(
                "2026-09-10T10:30:00Z", periods=8, freq="15min"
            ),
        )
        with patch.object(
            guard,
            "get_indicator_event",
            return_value=self._today_eurusd_event(),
        ):
            result = guard.validate_fresh_setup_swing_identity(
                closed_15m,
                "EURUSD",
                self._today_setup_identity(),
                strict_trader,
            )

        self.assertTrue(result["ok"])
        self.assertIsNone(result["reason"])
        self.assertTrue(result["details"]["fresh_setup_swing_matched"])
        self.assertEqual(
            result["details"]["fresh_setup_swing_match_method"],
            "authoritative_indicator_event_identity",
        )

    def test_authoritative_event_identity_mismatch_still_blocks_execution(self):
        with patch.object(
            guard,
            "get_indicator_event",
            return_value=self._today_eurusd_event(),
        ):
            result = guard.validate_fresh_setup_swing_identity(
                self._full_history(),
                "EURUSD",
                self._today_setup_identity(swing_price=1.16350),
                strict_trader,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], guard.SWING_CHANGED_REASON)
        self.assertFalse(result["details"]["fresh_setup_swing_matched"])
        self.assertFalse(
            result["details"]["authoritative_indicator_event_checks"][
                "swing_price"
            ]
        )

    def test_missing_authoritative_source_event_fails_closed(self):
        with patch.object(guard, "get_indicator_event", return_value=None):
            result = guard.validate_fresh_setup_swing_identity(
                self._full_history(),
                "EURUSD",
                self._today_setup_identity(),
                strict_trader,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], guard.SWING_CHANGED_REASON)
        self.assertFalse(result["details"]["authoritative_indicator_event_found"])

    def test_legacy_setup_without_event_id_still_uses_current_pivot_fallback(self):
        full = self._full_history()
        target_time = full.index[5].isoformat()
        truncated = full.iloc[3:].copy()

        result = guard.validate_fresh_setup_swing_identity(
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
        self.assertIn(
            result["details"]["fresh_setup_swing_match_method"],
            {
                "smc_indicator_confirmed_pivot_identity",
                "legacy_raw_pivot_identity",
            },
        )


if __name__ == "__main__":
    unittest.main()
