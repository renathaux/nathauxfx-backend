import unittest
from unittest.mock import patch

import pandas as pd

from services import smc_strategy_authority as authority
from services.setup_swing_execution_guard import validate_fresh_setup_swing_identity


class _Shared:
    FIFTEEN_M_SWING_WATCH = {}

    @staticmethod
    def normalize_symbol(symbol):
        return str(symbol or "").upper().replace("/", "")

    @staticmethod
    def save_fifteen_m_swing_watch():
        return None


class _StrictTraderStub:
    shared = _Shared()
    BOS_MIN_BUFFER_POINTS = 10
    REMEMBERED_BREAKOUT_MAX_15M_CANDLES = 4

    @staticmethod
    def get_cached_execution_settings():
        return {"bos_buffer_points": 10}

    @staticmethod
    def bos_buffer(data, symbol, configured_points):
        return 0.00010

    @staticmethod
    def point_size(symbol):
        return 0.00001

    @staticmethod
    def minimum_swing_size(symbol):
        return 0.00100

    @staticmethod
    def candle_close_time(value, minutes):
        timestamp = pd.Timestamp(value)
        if timestamp.tzinfo is None:
            timestamp = timestamp.tz_localize("UTC")
        else:
            timestamp = timestamp.tz_convert("UTC")
        return (timestamp + pd.Timedelta(minutes=minutes)).isoformat()

    @staticmethod
    def utc_timestamp(value):
        if value is None:
            return None
        timestamp = pd.Timestamp(value)
        if timestamp.tzinfo is None:
            timestamp = timestamp.tz_localize("UTC")
        else:
            timestamp = timestamp.tz_convert("UTC")
        return timestamp

    @staticmethod
    def clear_opposite_watch(symbol, side, reason):
        return False

    @staticmethod
    def get_watch_key(symbol, side):
        return f"{symbol}:{side}"

    @staticmethod
    def remembered_breakout(symbol, side, current_close_time=None, current_close=None):
        return None

    @staticmethod
    def detect_raw_swings(frame, symbol):
        return []


def _frame(periods=12):
    index = pd.date_range("2026-09-10T09:00:00Z", periods=periods, freq="15min")
    return pd.DataFrame(
        {
            "Open": [1.1640] * periods,
            "High": [1.1645] * periods,
            "Low": [1.1590] * periods,
            "Close": [1.1600] * periods,
        },
        index=index,
    )


def _bearish_choch(frame, event_index):
    return {
        "event_id": "evt-eurusd-choch",
        "event_identity": {"fixture": True},
        "event_status": "CONFIRMED",
        "tradable": True,
        "event_type": "CHOCH",
        "direction": "BEARISH",
        "timestamp": frame.index[event_index].isoformat(),
        "close": 1.16190,
        "broken_swing_timestamp": frame.index[event_index - 3].isoformat(),
        "broken_level": 1.16218,
        "structure_start_index": event_index - 3,
        "break_index": event_index,
        "event_invalidation_swing": {
            "type": "HIGH",
            "price": 1.16400,
            "swing_time": frame.index[event_index - 4].isoformat(),
        },
    }


class DurableSmcSetupRecoveryTests(unittest.TestCase):
    def setUp(self):
        _StrictTraderStub.shared.FIFTEEN_M_SWING_WATCH = {}

    def test_one_candle_old_confirmed_choch_remains_remembered(self):
        frame = _frame()
        event = _bearish_choch(frame, len(frame) - 2)
        analysis = {
            "bias": "BEARISH",
            "events": [event],
            "current_structure": {"bias": "BEARISH"},
            "swings": [],
        }
        with patch.object(authority, "get_authoritative_structure", return_value=analysis), patch.object(
            authority, "get_event_lifecycles", return_value={event["event_id"]: {}}
        ):
            result = authority.evaluate_indicator_breakout(
                frame,
                "EURUSD",
                strict_trader_module=_StrictTraderStub,
            )

        self.assertEqual(result["side"], "SELL")
        self.assertTrue(result["remembered"])
        self.assertTrue(result["durable_event_recovered"])
        self.assertEqual(result["reason"], "SMC_INDICATOR_REMEMBERED_CHOCH")
        self.assertEqual(result["indicator_event_id"], event["event_id"])

    def test_event_older_than_four_15m_candles_is_not_recovered(self):
        frame = _frame()
        event = _bearish_choch(frame, len(frame) - 7)
        analysis = {
            "bias": "BEARISH",
            "events": [event],
            "current_structure": {"bias": "BEARISH"},
            "swings": [],
        }
        with patch.object(authority, "get_authoritative_structure", return_value=analysis):
            result = authority.evaluate_indicator_breakout(
                frame,
                "EURUSD",
                strict_trader_module=_StrictTraderStub,
            )

        self.assertEqual(result["side"], "WAIT")
        self.assertEqual(result["reason"], "WAIT_NO_FRESH_15M_SMC_BREAK")

    def test_authoritative_event_revalidates_swing_missing_from_local_window(self):
        full = _frame(14)
        event = _bearish_choch(full, 7)
        local_window = full.iloc[9:].copy()
        setup_identity = {
            "symbol": "EURUSD",
            "direction": "SELL",
            "swing_type": "LOW",
            "swing_timestamp": event["broken_swing_timestamp"],
            "swing_price": event["broken_level"],
        }
        authority_result = {"events": [event]}

        with patch(
            "services.setup_swing_execution_guard.read_authoritative_structure",
            return_value=authority_result,
        ), patch(
            "services.setup_swing_execution_guard.detect_confirmed_swings",
            return_value=[],
        ):
            result = validate_fresh_setup_swing_identity(
                local_window,
                "EURUSD",
                setup_identity,
                _StrictTraderStub,
            )

        self.assertTrue(result["ok"])
        self.assertIsNone(result["reason"])
        self.assertEqual(
            result["details"]["fresh_setup_swing_match_method"],
            "authoritative_indicator_event_identity",
        )
        self.assertEqual(
            result["details"]["authoritative_setup_event_id"],
            event["event_id"],
        )

    def test_changed_setup_price_is_still_blocked(self):
        full = _frame(14)
        event = _bearish_choch(full, 7)
        local_window = full.iloc[9:].copy()
        setup_identity = {
            "symbol": "EURUSD",
            "direction": "SELL",
            "swing_type": "LOW",
            "swing_timestamp": event["broken_swing_timestamp"],
            "swing_price": event["broken_level"] + 0.00050,
        }

        with patch(
            "services.setup_swing_execution_guard.read_authoritative_structure",
            return_value={"events": [event]},
        ), patch(
            "services.setup_swing_execution_guard.detect_confirmed_swings",
            return_value=[],
        ):
            result = validate_fresh_setup_swing_identity(
                local_window,
                "EURUSD",
                setup_identity,
                _StrictTraderStub,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "WAIT_SETUP_SWING_CHANGED_BEFORE_EXECUTION")


if __name__ == "__main__":
    unittest.main()
