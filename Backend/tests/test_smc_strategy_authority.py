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
    REMEMBERED_BREAKOUT_MAX_15M_CANDLES = 4

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
        key = _StrictTraderStub.get_watch_key(symbol, side)
        watch = _StrictTraderStub.shared.FIFTEEN_M_SWING_WATCH.get(key)
        if not isinstance(watch, dict):
            return None
        expires_at = _StrictTraderStub.utc_timestamp(watch.get("expires_at"))
        current_time = _StrictTraderStub.utc_timestamp(current_close_time)
        invalidation_level = float(watch["invalidation_level"])
        invalidated = (
            side == "BUY" and float(current_close) <= invalidation_level
        ) or (
            side == "SELL" and float(current_close) >= invalidation_level
        )
        if (expires_at is not None and current_time > expires_at) or invalidated:
            _StrictTraderStub.shared.FIFTEEN_M_SWING_WATCH.pop(key, None)
            return None
        return {
            "side": side,
            "level": watch["swing_level"],
            "break_time": watch["break_candle_time"],
            "break_close_time": watch["break_close_time"],
            "break_close": watch["break_close"],
            "bos_buffer": watch["bos_buffer"],
            "swing": watch["swing"],
            "break_type": watch["break_type"],
            "invalidation_level": invalidation_level,
            "remembered": True,
            "watch_status": watch["status"],
            "watch": watch,
            "indicator_event_id": watch["indicator_event_id"],
            "indicator_event_identity": watch.get("indicator_event_identity"),
        }


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
                "event_id": "test-current-event",
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
        "new_event_ids": ["test-current-event"],
        "current_structure": {"bias": "BULLISH"},
        "swings": [],
        "fib_levels": [],
    }


def _remembered_watch(event, *, side="SELL", invalidation_level=1.16400):
    event_time = pd.Timestamp(event["timestamp"])
    break_close_time = event_time + pd.Timedelta(minutes=15)
    return {
        "source": authority.AUTHORITY_SOURCE,
        "side": side,
        "swing_level": float(event["broken_level"]),
        "break_candle_time": event["timestamp"],
        "break_close_time": break_close_time.isoformat(),
        "break_close": float(event["close"]),
        "bos_buffer": 0.00010,
        "swing": {
            "type": "LOW" if side == "SELL" else "HIGH",
            "time": event["broken_swing_timestamp"],
            "price": float(event["broken_level"]),
        },
        "break_type": event["event_type"],
        "invalidation_level": invalidation_level,
        "expires_at": (break_close_time + pd.Timedelta(minutes=60)).isoformat(),
        "status": "PENDING",
        "indicator_event_id": event.get("event_id"),
        "indicator_event_identity": event.get("event_identity"),
        "event_invalidation_swing": event.get("event_invalidation_swing"),
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
                "event_id": "test-previous-event",
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
                "event_id": "test-current-event",
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
        "new_event_ids": ["test-current-event"],
        "current_structure": {"bias": bias},
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

    def test_confirmed_event_remains_eligible_until_four_15m_candle_window_expires(self):
        frame = _frame(rows=12)
        analysis = _analysis(frame)
        # EURUSD BEARISH CHoCH: event candle 11:45 UTC, candle closes at
        # 12:00; it remains executable at 12:52 (last closed M15 12:45).
        event = analysis["events"][0]
        event.update({
            "event_id": "durable-eurusd-choch",
            "event_type": "CHOCH",
            "direction": "BEARISH",
            "timestamp": frame.index[7].isoformat(),
            "broken_swing_timestamp": frame.index[4].isoformat(),
            "broken_level": 1.16218,
            "close": 1.16200,
            "event_invalidation_swing": {
                "type": "HIGH", "price": 1.16400,
                "swing_time": frame.index[3].isoformat(),
            },
        })
        _StrictTraderStub.shared.FIFTEEN_M_SWING_WATCH["EURUSD:SELL"] = (
            _remembered_watch(event)
        )
        with patch.object(authority, "get_authoritative_structure", return_value=analysis):
            result = authority.evaluate_indicator_breakout(
                frame,
                "EURUSD",
                strict_trader_module=_StrictTraderStub,
            )

        self.assertEqual(result["side"], "SELL")
        self.assertEqual(result["reason"], "SMC_INDICATOR_REMEMBERED_CHOCH")
        self.assertTrue(result["remembered"])
        self.assertEqual(result["indicator_event_id"], "durable-eurusd-choch")

    def test_cleared_watch_cannot_be_recreated_from_aged_durable_event(self):
        frame = _frame(rows=11)
        analysis = _analysis(frame)
        event = analysis["events"][0]
        event.update({
            "event_id": "cleared-event",
            "timestamp": frame.index[7].isoformat(),
            "broken_swing_timestamp": frame.index[4].isoformat(),
        })
        with patch.object(authority, "get_authoritative_structure", return_value=analysis):
            result = authority.evaluate_indicator_breakout(
                frame, "EURUSD", strict_trader_module=_StrictTraderStub
            )

        self.assertEqual(result["side"], "WAIT")
        self.assertEqual(result["reason"], "WAIT_DURABLE_EVENT_WATCH_INACTIVE")
        self.assertEqual(result["indicator_event_truth"], "CONFIRMED")
        self.assertFalse(result["entry_lifecycle_eligible"])

    def test_structure_invalidation_clears_watch_without_resurrection(self):
        frame = _frame(rows=11)
        frame.iloc[-1, frame.columns.get_loc("Close")] = 1.16500
        analysis = _analysis(frame)
        event = analysis["events"][0]
        event.update({
            "event_id": "invalidated-event",
            "direction": "BEARISH",
            "timestamp": frame.index[7].isoformat(),
            "broken_swing_timestamp": frame.index[4].isoformat(),
            "broken_level": 1.16218,
            "close": 1.16200,
        })
        _StrictTraderStub.shared.FIFTEEN_M_SWING_WATCH["EURUSD:SELL"] = (
            _remembered_watch(event)
        )
        with patch.object(authority, "get_authoritative_structure", return_value=analysis):
            result = authority.evaluate_indicator_breakout(
                frame, "EURUSD", strict_trader_module=_StrictTraderStub
            )

        self.assertNotIn("EURUSD:SELL", _StrictTraderStub.shared.FIFTEEN_M_SWING_WATCH)
        self.assertEqual(result["side"], "WAIT")
        self.assertEqual(result["reason"], "WAIT_DURABLE_EVENT_WATCH_INACTIVE")
        self.assertEqual(result["indicator_event_truth"], "CONFIRMED")

    def test_cleared_or_consumed_current_event_cannot_restart_entry_lifecycle(self):
        frame = _frame()
        for status, blocking_reason in (
            ("BLOCKED", "EMA no longer permits remembered direction"),
            ("BLOCKED", "consolidation ended; fresh BOS required"),
            ("INVALIDATED", "remembered breakout structure invalidated"),
            ("CONSUMED", "trade submission accepted"),
        ):
            with self.subTest(status=status, reason=blocking_reason):
                analysis = _analysis(frame)
                analysis["events"][0]["event_id"] = "same-current-event"
                lifecycle = {
                    "same-current-event": {
                        "LIVE": {
                            "status": status,
                            "blocking_reason": blocking_reason,
                        }
                    }
                }
                with patch.object(
                    authority, "get_authoritative_structure", return_value=analysis
                ), patch.object(
                    authority, "get_event_lifecycles", return_value=lifecycle
                ):
                    result = authority.evaluate_indicator_breakout(
                        frame, "EURUSD", strict_trader_module=_StrictTraderStub
                    )

                self.assertEqual(result["side"], "WAIT")
                self.assertEqual(
                    result["reason"],
                    "WAIT_DURABLE_EVENT_LIFECYCLE_INELIGIBLE",
                )
                self.assertEqual(result["indicator_event_truth"], "CONFIRMED")
                self.assertFalse(result["entry_lifecycle_eligible"])
                self.assertEqual(result["entry_lifecycle_reason"], blocking_reason)

    def test_expired_event_remains_confirmed_truth_but_is_not_an_entry(self):
        frame = _frame(rows=15)
        analysis = _analysis(frame)
        analysis["events"][0].update({
            "event_id": "expired-event",
            "timestamp": frame.index[9].isoformat(),
        })
        with patch.object(authority, "get_authoritative_structure", return_value=analysis):
            result = authority.evaluate_indicator_breakout(
                frame,
                "EURUSD",
                strict_trader_module=_StrictTraderStub,
            )

        self.assertEqual(result["side"], "WAIT")
        self.assertEqual(result["reason"], "WAIT_INDICATOR_EVENT_EXPIRED")
        self.assertEqual(result["indicator_event_truth"], "CONFIRMED")
        self.assertTrue(result["indicator_event_expired"])


if __name__ == "__main__":
    unittest.main()
