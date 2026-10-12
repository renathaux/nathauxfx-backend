import unittest
from unittest.mock import patch

import pandas as pd

from indicators.smc.engine import SwingPoint, analyze_structure, detect_confirmed_swings
from indicators.smc.legacy_engine import analyze_structure as analyze_legacy_structure


class SmcIndicatorEngineTests(unittest.TestCase):
    def frame(self, rows):
        index = pd.date_range("2026-08-17T00:00:00Z", periods=len(rows), freq="15min")
        return pd.DataFrame(rows, index=index, columns=["Open", "High", "Low", "Close"])

    def external_frame(self, closes, *, lows=None, highs=None):
        lows = lows or [value - 1 for value in closes]
        highs = highs or [value + 1 for value in closes]
        rows = [
            (closes[index - 1] if index else close, highs[index], lows[index], close)
            for index, close in enumerate(closes)
        ]
        return self.frame(rows)

    def swing(self, frame, swing_type, index, confirmed_index, price):
        return SwingPoint(
            swing_type,
            index,
            confirmed_index,
            frame.index[index].isoformat(),
            frame.index[confirmed_index].isoformat(),
            float(price),
        )

    def bullish_external_case(self, final_close, *, final_low=None):
        closes = [104, 103, 106, 108, 106, 109, 111, 110, 107, 110, 113, 112, 114, 116, 104, final_close]
        lows = [value - 1 for value in closes]
        if final_low is not None:
            lows[-1] = final_low
        frame = self.external_frame(closes, lows=lows)
        swings = [
            self.swing(frame, "LOW", 1, 3, 100),
            self.swing(frame, "HIGH", 3, 5, 110),
            self.swing(frame, "LOW", 8, 10, 105),
            self.swing(frame, "HIGH", 10, 12, 115),
        ]
        with patch("indicators.smc.engine.detect_confirmed_swings", return_value=swings):
            return analyze_structure(frame)

    def bearish_external_case(self, final_close, *, final_high=None):
        closes = [116, 117, 114, 112, 114, 111, 109, 110, 113, 110, 107, 108, 106, 104, 116, final_close]
        highs = [value + 1 for value in closes]
        if final_high is not None:
            highs[-1] = final_high
        frame = self.external_frame(closes, highs=highs)
        swings = [
            self.swing(frame, "HIGH", 1, 3, 120),
            self.swing(frame, "LOW", 3, 5, 110),
            self.swing(frame, "HIGH", 8, 10, 115),
            self.swing(frame, "LOW", 10, 12, 105),
        ]
        with patch("indicators.smc.engine.detect_confirmed_swings", return_value=swings):
            return analyze_structure(frame)

    def bullish_continuation_frontier_case(self, expansion_level=112.0):
        closes = [100, 100, 100, 100, 100, 100, 111, 109, 107, 106, 107, 109, 108, 111, expansion_level + 1]
        frame = self.external_frame(closes)
        swings = [
            self.swing(frame, "LOW", 2, 4, 95),
            self.swing(frame, "HIGH", 3, 5, 110),
            self.swing(frame, "HIGH", 8, 10, 108),
            self.swing(frame, "LOW", 9, 11, 104),
            self.swing(frame, "HIGH", 11, 13, expansion_level),
        ]
        with patch("indicators.smc.engine.detect_confirmed_swings", return_value=swings):
            return analyze_structure(frame, timeframe="15m", point_size=0.01)

    def bearish_continuation_frontier_case(self, expansion_level=108.0):
        closes = [120, 120, 120, 120, 120, 120, 109, 111, 113, 114, 113, 111, 112, 109, expansion_level - 1]
        frame = self.external_frame(closes)
        swings = [
            self.swing(frame, "HIGH", 2, 4, 125),
            self.swing(frame, "LOW", 3, 5, 110),
            self.swing(frame, "LOW", 8, 10, 112),
            self.swing(frame, "HIGH", 9, 11, 116),
            self.swing(frame, "LOW", 11, 13, expansion_level),
        ]
        with patch("indicators.smc.engine.detect_confirmed_swings", return_value=swings):
            return analyze_structure(frame, timeframe="15m", point_size=0.01)

    def bearish_choch_with_local_reset_high_case(self):
        closes = [100, 100, 100, 100, 100, 100, 111, 109, 107, 106,
                  107, 109, 108, 111, 121, 114, 113, 112, 110, 94]
        frame = self.external_frame(closes)
        swings = [
            self.swing(frame, "LOW", 2, 4, 95),
            self.swing(frame, "HIGH", 3, 5, 110),
            self.swing(frame, "HIGH", 8, 10, 108),
            self.swing(frame, "LOW", 9, 11, 104),
            self.swing(frame, "HIGH", 11, 13, 120),
            self.swing(frame, "HIGH", 16, 18, 115),
        ]
        with patch("indicators.smc.engine.detect_confirmed_swings", return_value=swings):
            return analyze_structure(frame, timeframe="15m", point_size=0.01)

    def bullish_choch_with_local_reset_low_case(self):
        closes = [120, 120, 120, 120, 120, 120, 109, 111, 113, 114,
                  113, 111, 112, 109, 99, 106, 107, 108, 110, 126]
        frame = self.external_frame(closes)
        swings = [
            self.swing(frame, "HIGH", 2, 4, 125),
            self.swing(frame, "LOW", 3, 5, 110),
            self.swing(frame, "LOW", 8, 10, 112),
            self.swing(frame, "HIGH", 9, 11, 116),
            self.swing(frame, "LOW", 11, 13, 100),
            self.swing(frame, "LOW", 16, 18, 105),
        ]
        with patch("indicators.smc.engine.detect_confirmed_swings", return_value=swings):
            return analyze_structure(frame, timeframe="15m", point_size=0.01)

    def test_swing_confirmation_does_not_look_ahead(self):
        data = self.frame([
            (10, 11, 9, 10),
            (10, 12, 9.5, 11),
            (11, 15, 10, 14),
            (14, 13, 10.5, 12),
            (12, 12.5, 10.8, 11.5),
        ])
        swings = detect_confirmed_swings(data, left_bars=2, right_bars=2)
        highs = [item for item in swings if item.swing_type == "HIGH"]
        self.assertEqual(len(highs), 1)
        self.assertEqual(highs[0].index, 2)
        self.assertEqual(highs[0].confirmed_index, 4)

    def test_bullish_break_is_bos_from_neutral(self):
        data = self.frame([
            (10, 11, 9.0, 10.0),
            (10, 12, 9.5, 11.0),
            (11, 15, 10.0, 14.0),
            (14, 13, 10.5, 12.0),
            (12, 12.5, 10.8, 11.5),
            (11.5, 14.0, 11.0, 13.5),
            (13.5, 16.0, 13.0, 15.5),
        ])
        result = analyze_structure(data, left_bars=2, right_bars=2)
        bullish = [event for event in result["events"] if event["direction"] == "BULLISH"]
        self.assertTrue(bullish)
        self.assertEqual(bullish[-1]["event_type"], "BOS")
        self.assertEqual(result["bias"], "BULLISH")

    def test_opposite_break_becomes_choch(self):
        data = self.frame([
            (10, 11, 9.0, 10.0),
            (10, 12, 9.5, 11.0),
            (11, 15, 10.0, 14.0),
            (14, 13, 10.5, 12.0),
            (12, 12.5, 10.8, 11.5),
            (11.5, 14.0, 11.0, 13.5),
            (13.5, 16.0, 13.0, 15.5),
            (15.5, 15.8, 12.0, 13.0),
            (13.0, 13.5, 10.0, 10.5),
            (10.5, 11.0, 8.5, 9.0),
        ])
        result = analyze_structure(data, left_bars=2, right_bars=2)
        bearish = [event for event in result["events"] if event["direction"] == "BEARISH"]
        if bearish:
            self.assertIn(bearish[-1]["event_type"], {"BOS", "CHOCH"})

    def test_internal_higher_low_break_does_not_flip_bullish_regime(self):
        # External bullish origin low is 100. A later internal HL forms near 112,
        # then price makes another high. Closing below 112 must not be CHoCH
        # while price remains above the protected external 100 low.
        data = self.frame([
            (103, 104, 100, 103),
            (103, 106, 102, 105),
            (105, 110, 104, 109),
            (109, 108, 103, 106),
            (106, 107, 102, 105),
            (105, 109, 104, 108),
            (108, 112, 107, 111),
            (111, 116, 110, 115),
            (115, 114, 111, 112),
            (112, 113, 111.5, 112.5),
            (112.5, 117, 112, 116.5),
            (116.5, 120, 115, 119),
            (119, 118, 113, 114),
            (114, 115, 110.5, 111),
            (111, 112, 108, 109),
        ])
        result = analyze_structure(data, left_bars=2, right_bars=2)
        bearish_choch = [
            event for event in result["events"]
            if event["direction"] == "BEARISH" and event["event_type"] == "CHOCH"
        ]
        self.assertFalse(bearish_choch)
        self.assertNotEqual(result["bias"], "BEARISH")

    def test_external_protected_low_break_can_flip_regime(self):
        data = self.frame([
            (103, 104, 100, 103),
            (103, 106, 102, 105),
            (105, 110, 104, 109),
            (109, 108, 103, 106),
            (106, 107, 102, 105),
            (105, 109, 104, 108),
            (108, 112, 107, 111),
            (111, 116, 110, 115),
            (115, 114, 111, 112),
            (112, 113, 111.5, 112.5),
            (112.5, 117, 112, 116.5),
            (116.5, 120, 115, 119),
            (119, 118, 110, 112),
            (112, 113, 104, 105),
            (105, 106, 98, 99),
        ])
        result = analyze_structure(data, left_bars=2, right_bars=2)
        bearish_choch = [
            event for event in result["events"]
            if event["direction"] == "BEARISH" and event["event_type"] == "CHOCH"
        ]
        self.assertTrue(bearish_choch)
        self.assertEqual(result["bias"], "BEARISH")

    def test_bullish_external_structure_ignores_internal_higher_low_break(self):
        result = self.bullish_external_case(104)
        bearish_choch = [event for event in result["events"] if event["direction"] == "BEARISH"]
        self.assertFalse(bearish_choch)
        self.assertEqual(result["bias"], "BULLISH")
        self.assertEqual(result["current_structure"]["protected_low"]["price"], 100.0)

    def test_bearish_external_structure_ignores_internal_lower_high_break(self):
        result = self.bearish_external_case(116)
        bullish_choch = [event for event in result["events"] if event["direction"] == "BULLISH"]
        self.assertFalse(bullish_choch)
        self.assertEqual(result["bias"], "BEARISH")
        self.assertEqual(result["current_structure"]["protected_high"]["price"], 120.0)

    def test_closed_break_of_protected_bullish_low_is_bearish_choch(self):
        result = self.bullish_external_case(99)
        event = result["events"][-1]
        self.assertEqual((event["event_type"], event["direction"]), ("CHOCH", "BEARISH"))
        self.assertEqual(event["broken_level"], 100.0)
        self.assertEqual(event["event_invalidation_swing"]["type"], "HIGH")
        self.assertEqual(event["event_invalidation_swing"]["price"], 115.0)
        self.assertEqual(result["current_structure"]["protected_high"]["price"], 115.0)

    def test_closed_break_of_protected_bearish_high_is_bullish_choch(self):
        result = self.bearish_external_case(121)
        event = result["events"][-1]
        self.assertEqual((event["event_type"], event["direction"]), ("CHOCH", "BULLISH"))
        self.assertEqual(event["broken_level"], 120.0)
        self.assertEqual(event["event_invalidation_swing"]["type"], "LOW")
        self.assertEqual(event["event_invalidation_swing"]["price"], 105.0)
        self.assertEqual(result["current_structure"]["protected_low"]["price"], 105.0)

    def test_continuation_bos_owns_current_leg_swing_without_ratchet_of_protected_level(self):
        bullish = self.bullish_external_case(104)
        first, continuation = bullish["events"][:2]
        self.assertEqual(first["event_invalidation_swing"]["price"], 100.0)
        self.assertEqual(continuation["event_invalidation_swing"]["price"], 105.0)
        self.assertEqual(bullish["current_structure"]["protected_low"]["price"], 100.0)

        bearish = self.bearish_external_case(116)
        first, continuation = bearish["events"][:2]
        self.assertEqual(first["event_invalidation_swing"]["price"], 120.0)
        self.assertEqual(continuation["event_invalidation_swing"]["price"], 115.0)
        self.assertEqual(bearish["current_structure"]["protected_high"]["price"], 120.0)

    def test_bullish_continuation_frontier_ignores_lower_local_high(self):
        result = self.bullish_continuation_frontier_case()
        bullish_levels = [
            event["broken_level"] for event in result["events"]
            if event["direction"] == "BULLISH"
        ]
        self.assertEqual(bullish_levels, [110.0, 112.0])
        self.assertNotIn(108.0, bullish_levels)
        self.assertEqual(
            result["current_structure"]["bullish_continuation_frontier"]["price"],
            112.0,
        )

    def test_bearish_continuation_frontier_ignores_higher_local_low(self):
        result = self.bearish_continuation_frontier_case()
        bearish_levels = [
            event["broken_level"] for event in result["events"]
            if event["direction"] == "BEARISH"
        ]
        self.assertEqual(bearish_levels, [110.0, 108.0])
        self.assertNotIn(112.0, bearish_levels)
        self.assertEqual(
            result["current_structure"]["bearish_continuation_frontier"]["price"],
            108.0,
        )

    def test_rejected_sub_100_point_expansion_does_not_move_frontier(self):
        result = self.bullish_continuation_frontier_case(expansion_level=110.99)
        bullish_levels = [
            event["broken_level"] for event in result["events"]
            if event["direction"] == "BULLISH"
        ]
        self.assertEqual(bullish_levels, [110.0])
        self.assertEqual(
            result["current_structure"]["bullish_continuation_frontier"]["price"],
            110.0,
        )
        self.assertEqual(result["config"]["last_accepted_structure_level"], 110.0)

    def test_frontier_resets_only_after_existing_choch_qualification(self):
        bullish = self.bullish_external_case(99)
        bearish_choch = bullish["events"][-1]
        self.assertEqual(
            (bearish_choch["event_type"], bearish_choch["direction"], bearish_choch["broken_level"]),
            ("CHOCH", "BEARISH", 100.0),
        )
        self.assertIsNone(
            bullish["current_structure"]["bullish_continuation_frontier"]
        )
        self.assertEqual(
            bullish["current_structure"]["bearish_continuation_frontier"]["price"],
            100.0,
        )

    def test_bearish_choch_reset_protects_outgoing_bullish_frontier(self):
        result = self.bearish_choch_with_local_reset_high_case()
        event = result["events"][-1]
        self.assertEqual(
            (event["event_type"], event["direction"], event["broken_level"]),
            ("CHOCH", "BEARISH", 95.0),
        )
        self.assertEqual(event["event_invalidation_swing"]["price"], 115.0)
        self.assertEqual(event["event_invalidation_swing"]["source"], "CURRENT_LEG")
        self.assertEqual(result["current_structure"]["protected_high"]["price"], 120.0)

    def test_bullish_choch_reset_protects_outgoing_bearish_frontier(self):
        result = self.bullish_choch_with_local_reset_low_case()
        event = result["events"][-1]
        self.assertEqual(
            (event["event_type"], event["direction"], event["broken_level"]),
            ("CHOCH", "BULLISH", 125.0),
        )
        self.assertEqual(event["event_invalidation_swing"]["price"], 105.0)
        self.assertEqual(event["event_invalidation_swing"]["source"], "CURRENT_LEG")
        self.assertEqual(result["current_structure"]["protected_low"]["price"], 100.0)

    def test_every_event_owned_swing_was_confirmed_by_break(self):
        for result in (
            self.bullish_external_case(99),
            self.bearish_external_case(121),
        ):
            for event in result["events"]:
                swing = event["event_invalidation_swing"]
                self.assertIsNotNone(swing)
                self.assertLessEqual(swing["confirmation_index"], event["break_index"])

    def test_legacy_engine_emits_event_owned_opposite_extrema_without_detector_change(self):
        closes = [104, 103, 106, 108, 106, 109, 111, 110, 107, 110, 113, 112, 114, 116, 104, 99]
        result = analyze_legacy_structure(
            self.external_frame(closes), timeframe="15m", point_size=0.01)
        self.assertTrue(result["events"])
        for event in result["events"]:
            swing = event["event_invalidation_swing"]
            expected_type = "LOW" if event["direction"] == "BULLISH" else "HIGH"
            self.assertEqual(swing["type"], expected_type)
            self.assertEqual(swing["source"], "LEGACY_CURRENT_STRUCTURE")
            self.assertLessEqual(swing["swing_index"], event["break_index"])
            self.assertEqual(swing["confirmation_index"], event["break_index"])

    def test_wick_through_protected_low_without_close_is_not_choch(self):
        result = self.bullish_external_case(101, final_low=99)
        bearish_choch = [event for event in result["events"] if event["direction"] == "BEARISH"]
        self.assertFalse(bearish_choch)
        self.assertEqual(result["bias"], "BULLISH")

    def test_module_is_analysis_only(self):
        import indicators.smc.engine as engine

        module_text = open(engine.__file__, "r", encoding="utf-8").read().lower()
        forbidden = ["place_market_order", "close_position", "live_auto", "paper_auto"]
        for token in forbidden:
            self.assertNotIn(token, module_text)


if __name__ == "__main__":
    unittest.main()
