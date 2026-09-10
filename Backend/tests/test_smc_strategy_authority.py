import unittest
from unittest.mock import patch

import pandas as pd

from services import smc_strategy_authority as authority


class _Shared:
    FIFTEEN_M_SWING_WATCH = {}

    @staticmethod
    def normalize_symbol(symbol):
        return str(symbol or "").upper()

    @staticmethod
    def save_fifteen_m_swing_watch():
        return None


class _StrictTraderStub:
    shared = _Shared()
    BOS_MIN_BUFFER_POINTS = 10

    @staticmethod
    def get_cached_execution_settings():
        return {"bos_buffer_points": 10}

    @staticmethod
    def bos_buffer(data, symbol, configured_points):
        return 0.00010 if symbol == "EURUSD" else 0.10

    @staticmethod
    def point_size(symbol):
        return 0.00001 if symbol == "EURUSD" else 0.01

    @staticmethod
    def minimum_swing_size(symbol):
        return 0.00100 if symbol == "EURUSD" else 1.00

    @staticmethod
    def candle_close_time(value, minutes):
        timestamp = pd.Timestamp(value)
        if timestamp.tzinfo is None:
            timestamp = timestamp.tz_localize("UTC")
        return (timestamp + pd.Timedelta(minutes=minutes)).isoformat()

    @staticmethod
    def utc_timestamp(value):
        if value is None:
            return None
        timestamp = pd.Timestamp(value)
        return timestamp.tz_localize("UTC") if timestamp.tzinfo is None else timestamp.tz_convert("UTC")

    @staticmethod
    def clear_opposite_watch(symbol, side, reason):
        return False

    @staticmethod
    def get_watch_key(symbol, side):
        return f"{symbol}:{side}"

    @staticmethod
    def remembered_breakout(symbol, side, current_close_time=None, current_close=None):
        return None


def _frame(rows=10):
    index = pd.date_range("2026-09-03T10:00:00Z", periods=rows, freq="15min")
    return pd.DataFrame(
        {
            "Open": [1.1000] * rows,
            "High": [1.1020] * rows,
            "Low": [1.0980] * rows,
            "Close": [1.1005] * rows,
        },
        index=index,
    )


def _analysis(frame, *, event_type="CHOCH", invalidation_price=1.0980, break_close=1.1015):
    last_index = len(frame) - 1
    event_time = frame.index[-1].isoformat()
    return {
        "bias": "BULLISH",
        "events": [
            {
                "event_type": event_type,
                "tradable": True,
                "direction": "BULLISH",
                "timestamp": event_time,
                "close": break_close,
                "broken_swing_timestamp": frame.index[-4].isoformat(),
                "broken_level": 1.1000,
                "structure_start_index": last_index - 3,
                "break_index": last_index,
                "event_invalidation_swing": {
                    "type": "LOW",
                    "price": invalidation_price,
                    "swing_time": frame.index[-5].isoformat(),
                },
            }
        ],
        "current_structure": {"bias": "BULLISH"},
        "swings": [],
        "fib_levels": [],
    }


def _two_small_bos_analysis(frame, direction="BULLISH", *, confirm_pattern=True):
    last_index = len(frame) - 1
    if direction == "BULLISH":
        previous_level = 1.1000
        previous_invalidation = 1.0998
        current_level = 1.1006
        current_invalidation = 1.0999 if confirm_pattern else 1.0997
        invalidation_type = "LOW"
        break_close = 1.1008
        bias = "BULLISH"
    else:
        previous_level = 1.1010
        previous_invalidation = 1.1012
        current_level = 1.1004
        current_invalidation = 1.1011 if confirm_pattern else 1.1013
        invalidation_type = "HIGH"
        break_close = 1.1002
        bias = "BEARISH"

    return {
        "bias": bias,
        "events": [
            {
                "event_type": "BOS",
                "tradable": True,
                "direction": direction,
                "timestamp": frame.index[-4].isoformat(),
                "close": previous_level,
                "broken_swing_timestamp": frame.index[-7].isoformat(),
                "broken_level": previous_level,
                "structure_start_index": last_index - 6,
                "break_index": last_index - 3,
                "event_invalidation_swing": {
                    "type": invalidation_type,
                    "price": previous_invalidation,
                    "swing_time": frame.index[-8].isoformat(),
                    "source": "CURRENT_LEG",
                },
            },
            {
                "event_type": "BOS",
                "tradable": True,
                "direction": direction,
                "timestamp": frame.index[-1].isoformat(),
                "close": break_close,
                "broken_swing_timestamp": frame.index[-3].isoformat(),
                "broken_level": current_level,
                "structure_start_index": last_index - 2,
                "break_index": last_index,
                "event_invalidation_swing": {
                    "type": invalidation_type,
                    "price": current_invalidation,
                    "swing_time": frame.index[-2].isoformat(),
                    "source": "CURRENT_LEG",
                },
            },
        ],
        "current_structure": {"bias": bias},
        "swings": [],
        "fib_levels": [],
    }


def _today_bearish_choch_analysis(frame, event_age=4):
    event_index = len(frame) - 1 - event_age
    event_time = frame.index[event_index]
    return {
        "bias": "BEARISH",
        "stream_last_candle": frame.index[-1].isoformat(),
        "events": [
            {
                "event_id": "smc1_today_eurusd_choch",
                "event_identity": {"symbol": "EURUSD", "timeframe": "15m"},
                "event_type": "CHOCH",
                "tradable": True,
                "direction": "BEARISH",
                "timestamp": event_time.isoformat(),
                "close": 1.16168,
                "broken_swing_timestamp": "2026-09-09T10:00:00+00:00",
                "broken_level": 1.16218,
                "structure_start_index": max(0, event_index - 5),
                "break_index": event_index,
                "event_invalidation_swing": {
                    "type": "HIGH",
                    "price": 1.16381,
                    "swing_time": "2026-09-10T10:00:00+00:00",
                },
            }
        ],
        "current_structure": {"bias": "BEARISH"},
        "swings": [],
        "fib_levels": [],
    }


class SmcStrategyAuthorityTests(unittest.TestCase):
    def setUp(self):
        _StrictTraderStub.shared.FIFTEEN_M_SWING_WATCH = {}

    def test_indicator_choch_owns_direction_and_classification(self):
        frame = _frame()
        with patch.object(authority, "get_authoritative_structure", return_value=_analysis(frame, event_type="CHOCH")):
            result = authority.evaluate_indicator_breakout(
                frame,
                "EURUSD",
                strict_trader_module=_StrictTraderStub,
            )

        self.assertEqual(result["side"], "BUY")
        self.assertEqual(result["break_type"], "CHOCH")
        self.assertTrue(result["indicator_authority"])
        self.assertEqual(result["indicator_source"], authority.AUTHORITY_SOURCE)
        self.assertEqual(result["swing"]["type"], "HIGH")

    def test_indicator_bos_classification_is_preserved(self):
        frame = _frame()
        with patch.object(authority, "get_authoritative_structure", return_value=_analysis(frame, event_type="BOS")):
            result = authority.evaluate_indicator_breakout(
                frame,
                "EURUSD",
                strict_trader_module=_StrictTraderStub,
            )

        self.assertEqual(result["side"], "BUY")
        self.assertEqual(result["break_type"], "BOS")

    def test_single_small_internal_bos_still_waits(self):
        frame = _frame()
        analysis = _analysis(frame, event_type="BOS", invalidation_price=1.0995)
        with patch.object(authority, "get_authoritative_structure", return_value=analysis):
            result = authority.evaluate_indicator_breakout(
                frame,
                "EURUSD",
                strict_trader_module=_StrictTraderStub,
            )

        self.assertEqual(result["side"], "WAIT")
        self.assertEqual(result["reason"], "WAIT_NO_VALID_100_POINT_SWING")

    def test_second_small_bullish_bos_with_hh_hl_can_enter_strategy(self):
        frame = _frame()
        analysis = _two_small_bos_analysis(frame, "BULLISH")
        with patch.object(authority, "get_authoritative_structure", return_value=analysis):
            result = authority.evaluate_indicator_breakout(
                frame,
                "EURUSD",
                strict_trader_module=_StrictTraderStub,
            )

        self.assertEqual(result["side"], "BUY")
        self.assertEqual(result["break_type"], "BOS")
        self.assertEqual(result["strategy_structure_qualification"], "INTERNAL_TWO_BOS_CONFIRMATION")
        self.assertEqual(result["internal_structure_confirmation"]["pattern"], "HH_HL")
        self.assertTrue(result["internal_structure_confirmation"]["qualified"])

    def test_second_small_bearish_bos_with_lh_ll_can_enter_strategy(self):
        frame = _frame()
        analysis = _two_small_bos_analysis(frame, "BEARISH")
        with patch.object(authority, "get_authoritative_structure", return_value=analysis):
            result = authority.evaluate_indicator_breakout(
                frame,
                "EURUSD",
                strict_trader_module=_StrictTraderStub,
            )

        self.assertEqual(result["side"], "SELL")
        self.assertEqual(result["strategy_structure_qualification"], "INTERNAL_TWO_BOS_CONFIRMATION")
        self.assertEqual(result["internal_structure_confirmation"]["pattern"], "LH_LL")
        self.assertTrue(result["internal_structure_confirmation"]["qualified"])

    def test_second_small_bos_without_hh_hl_still_waits(self):
        frame = _frame()
        analysis = _two_small_bos_analysis(frame, "BULLISH", confirm_pattern=False)
        with patch.object(authority, "get_authoritative_structure", return_value=analysis):
            result = authority.evaluate_indicator_breakout(
                frame,
                "EURUSD",
                strict_trader_module=_StrictTraderStub,
            )

        self.assertEqual(result["side"], "WAIT")
        self.assertEqual(result["reason"], "WAIT_NO_VALID_100_POINT_SWING")
        self.assertFalse(result["internal_structure_confirmation"]["qualified"])

    def test_small_choch_then_small_bos_does_not_use_internal_exception(self):
        frame = _frame()
        analysis = _two_small_bos_analysis(frame, "BULLISH")
        analysis["events"][0]["event_type"] = "CHOCH"
        with patch.object(authority, "get_authoritative_structure", return_value=analysis):
            result = authority.evaluate_indicator_breakout(
                frame,
                "EURUSD",
                strict_trader_module=_StrictTraderStub,
            )

        self.assertEqual(result["side"], "WAIT")
        self.assertEqual(result["reason"], "WAIT_NO_VALID_100_POINT_SWING")

    def test_existing_bos_buffer_still_blocks_weak_close(self):
        frame = _frame()
        analysis = _analysis(frame, break_close=1.10005)
        with patch.object(authority, "get_authoritative_structure", return_value=analysis):
            result = authority.evaluate_indicator_breakout(
                frame,
                "EURUSD",
                strict_trader_module=_StrictTraderStub,
            )

        self.assertEqual(result["side"], "WAIT")
        self.assertEqual(result["reason"], "WAIT_WEAK_15M_BOS")

    def test_today_bearish_choch_remains_authoritative_at_four_candles_old(self):
        frame = pd.DataFrame(
            {
                "Open": [1.1630] * 10,
                "High": [1.1640] * 10,
                "Low": [1.1590] * 10,
                "Close": [1.1605] * 10,
            },
            index=pd.date_range(
                "2026-09-10T10:30:00Z", periods=10, freq="15min"
            ),
        )
        analysis = _today_bearish_choch_analysis(frame, event_age=4)
        lifecycles = {
            "smc1_today_eurusd_choch": {
                "PAPER": {"status": "BLOCKED"},
                "LIVE": {"status": "ELIGIBLE"},
            }
        }
        with (
            patch.object(authority, "get_authoritative_structure", return_value=analysis),
            patch.object(authority, "get_event_lifecycles", return_value=lifecycles),
        ):
            result = authority.evaluate_indicator_breakout(
                frame,
                "EURUSD",
                strict_trader_module=_StrictTraderStub,
            )

        self.assertEqual(result["side"], "SELL")
        self.assertEqual(result["break_type"], "CHOCH")
        self.assertTrue(result["remembered"])
        self.assertEqual(result["smc_event_age_15m_candles"], 4)
        self.assertEqual(
            result["indicator_event_id"],
            "smc1_today_eurusd_choch",
        )
        self.assertEqual(result["reason"], "SMC_INDICATOR_REMEMBERED_CHOCH")

    def test_authoritative_event_expires_after_four_later_15m_candles(self):
        frame = _frame(rows=12)
        analysis = _analysis(frame)
        event = analysis["events"][0]
        event["event_id"] = "smc1_old_event"
        event["timestamp"] = frame.index[-6].isoformat()
        event["break_index"] = len(frame) - 6
        analysis["stream_last_candle"] = frame.index[-1].isoformat()
        with (
            patch.object(authority, "get_authoritative_structure", return_value=analysis),
            patch.object(
                authority,
                "get_event_lifecycles",
                return_value={"smc1_old_event": {"LIVE": {"status": "ELIGIBLE"}}},
            ),
        ):
            result = authority.evaluate_indicator_breakout(
                frame,
                "EURUSD",
                strict_trader_module=_StrictTraderStub,
            )

        self.assertEqual(result["side"], "WAIT")
        self.assertEqual(result["reason"], "WAIT_NO_FRESH_15M_SMC_BREAK")

    def test_terminal_event_is_not_replayed_inside_recent_window(self):
        frame = _frame(rows=12)
        analysis = _analysis(frame)
        event = analysis["events"][0]
        event["event_id"] = "smc1_consumed_event"
        event["timestamp"] = frame.index[-3].isoformat()
        event["break_index"] = len(frame) - 3
        analysis["stream_last_candle"] = frame.index[-1].isoformat()
        with (
            patch.object(authority, "get_authoritative_structure", return_value=analysis),
            patch.object(
                authority,
                "get_event_lifecycles",
                return_value={
                    "smc1_consumed_event": {
                        "LIVE": {"status": "CONSUMED"},
                        "PAPER": {"status": "EXPIRED"},
                    }
                },
            ),
        ):
            result = authority.evaluate_indicator_breakout(
                frame,
                "EURUSD",
                strict_trader_module=_StrictTraderStub,
            )

        self.assertEqual(result["side"], "WAIT")
        self.assertEqual(result["reason"], "WAIT_NO_FRESH_15M_SMC_BREAK")


if __name__ == "__main__":
    unittest.main()
