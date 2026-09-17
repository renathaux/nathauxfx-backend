from __future__ import annotations

import pandas as pd
import pytest

from services.strategy_engine.evaluator import (
    EvaluationState,
    empty_state,
    evaluate_strategy,
)
from services.strategy_engine.types import CandleFacts, StructureEventFacts, TrendFacts


T0 = pd.Timestamp("2026-09-17T10:00:00Z")
T1 = pd.Timestamp("2026-09-17T10:05:00Z")
T2 = pd.Timestamp("2026-09-17T10:10:00Z")


class FakeTimeline:
    def __init__(self, *, event=True, trend=None, candles=None, opposite_swing=1.1100):
        self._event = event
        self._trend = trend or TrendFacts("BUY", "BUY", "BUY", "BUY")
        self._candles = candles or {
            T0: CandleFacts(T0, 1.0995, 1.1012, 1.0994, 1.1010, 88.8888889),
            T1: CandleFacts(T1, 1.1010, 1.1022, 1.1008, 1.1020, 71.4285714),
            T2: CandleFacts(T2, 1.1010, 1.1015, 1.0998, 1.1005, 29.4117647),
        }
        self._opposite_swing = opposite_swing
        self.trading_timestamps = tuple(self._candles)

    def candle(self, timestamp):
        return self._candles.get(pd.Timestamp(timestamp))

    def structure_event(self, timestamp):
        timestamp = pd.Timestamp(timestamp)
        if self._event is True and timestamp == T0:
            return StructureEventFacts(T0, "BUY", "BOS", 1.1000, 1.0980)
        if isinstance(self._event, dict):
            return self._event.get(timestamp)
        return None

    def trend(self, timestamp):
        return self._trend

    def next_trading_timestamp(self, timestamp):
        timestamp = pd.Timestamp(timestamp)
        values = list(self.trading_timestamps)
        for candidate in values:
            if candidate > timestamp:
                return candidate
        return None

    def nearest_opposite_swing(self, timestamp, direction, entry):
        return self._opposite_swing


def definition(*, trend_methods=None, breaks=None, confirmations=None, entry="BOS_CHOCH_CLOSE",
               stop=None, tp1=None, tp2=None, risk=None):
    confirmations = list(confirmations or [])
    return {
        "schema_version": 1,
        "symbols": ["EURUSD"],
        "trading_timeframe": "5m",
        "trend": {
            "timeframe": "15m" if trend_methods else None,
            "methods": list(trend_methods or []),
        },
        "structure": {
            "trigger": "BOS_CHOCH",
            "break_validation": list(breaks or []),
            "minimum_body_percent": 80.0 if "MIN_BODY_PERCENT" in (breaks or []) else None,
            "minimum_distance_pips": 5.0 if "MIN_DISTANCE" in (breaks or []) else None,
        },
        "confirmation": {
            "rules": confirmations,
            "minimum_body_percent": 60.0 if "MIN_BODY_PERCENT" in confirmations else None,
        },
        "entry": {"method": entry},
        "stop_loss": stop or {"method": "LAST_SWING", "buffer_pips": 0.0, "fixed_distance": None},
        "tp1": tp1 or {"enabled": False, "target_r": None, "close_percent": None, "protection_r": None},
        "tp2": tp2 or {"method": "FIXED_R", "value": 2.0},
        "risk": risk or {"method": "PERCENT_BALANCE", "value": 1.0},
    }


def test_no_bos_or_choch_waits_at_structure_step():
    result = evaluate_strategy(definition(), FakeTimeline(event=False), T0, empty_state(), symbol="EURUSD", account_balance=10000)
    assert result.signal == "WAIT"
    assert result.steps["structure"]["state"] == "WAITING"
    assert list(result.steps) == [
        "trend", "structure", "break_validation", "confirmation", "entry",
        "stop_loss", "tp1", "tp2", "risk",
    ]


def test_all_selected_trend_filters_must_agree():
    timeline = FakeTimeline(trend=TrendFacts("BUY", "SELL", "BUY", "BUY"))
    result = evaluate_strategy(
        definition(trend_methods=["BOS_CHOCH", "EMA_50"]),
        timeline,
        T0,
        empty_state(),
        symbol="EURUSD",
        account_balance=10000,
    )
    assert result.signal == "WAIT"
    assert result.steps["trend"]["state"] == "BLOCKED"


def test_break_validations_are_and_rules():
    weak = CandleFacts(T0, 1.1009, 1.1012, 1.1000, 1.1010, 8.3333333)
    timeline = FakeTimeline(candles={T0: weak, T1: FakeTimeline()._candles[T1], T2: FakeTimeline()._candles[T2]})
    result = evaluate_strategy(
        definition(breaks=["CLOSE_BEYOND", "MIN_BODY_PERCENT", "MIN_DISTANCE"]),
        timeline,
        T0,
        empty_state(),
        symbol="EURUSD",
        account_balance=10000,
    )
    assert result.signal == "WAIT"
    assert result.steps["break_validation"]["state"] == "BLOCKED"


def test_bos_close_entry_builds_sl_tp_and_risk_budget():
    result = evaluate_strategy(definition(), FakeTimeline(), T0, empty_state(), symbol="EURUSD", account_balance=10000)
    assert result.signal == "BUY"
    assert result.entry == pytest.approx(1.1010)
    assert result.sl == pytest.approx(1.0980)
    assert result.tp1 is None
    assert result.tp2 == pytest.approx(1.1070)
    assert result.risk_budget == {"method": "PERCENT_BALANCE", "value": 1.0, "dollars": 100.0}
    assert result.steps["entry"]["state"] == "PASSED"
    assert result.next_state.pending_setup is None


def test_next_same_direction_uses_immediate_next_candle_only():
    strategy = definition(confirmations=["NEXT_SAME_DIRECTION"], entry="CONFIRMATION_CLOSE")
    first = evaluate_strategy(strategy, FakeTimeline(), T0, empty_state(), symbol="EURUSD", account_balance=10000)
    assert first.signal == "WAIT"
    assert first.next_state.pending_setup is not None

    second = evaluate_strategy(strategy, FakeTimeline(), T1, first.next_state, symbol="EURUSD", account_balance=10000)
    assert second.signal == "BUY"
    assert second.entry == pytest.approx(1.1020)
    assert second.steps["confirmation"]["state"] == "PASSED"


def test_failed_immediate_confirmation_does_not_skip_to_later_candle():
    candles = {
        T0: FakeTimeline()._candles[T0],
        T1: CandleFacts(T1, 1.1010, 1.1011, 1.1000, 1.1002, 66.6666667),
        T2: CandleFacts(T2, 1.1002, 1.1020, 1.1001, 1.1018, 84.2105263),
    }
    strategy = definition(confirmations=["NEXT_SAME_DIRECTION", "SECOND_CLOSE_BEYOND"], entry="CONFIRMATION_CLOSE")
    timeline = FakeTimeline(candles=candles)
    first = evaluate_strategy(strategy, timeline, T0, empty_state(), symbol="EURUSD", account_balance=10000)
    failed = evaluate_strategy(strategy, timeline, T1, first.next_state, symbol="EURUSD", account_balance=10000)
    assert failed.signal == "WAIT"
    assert failed.steps["confirmation"]["state"] == "BLOCKED"
    assert failed.next_state.pending_setup is None

    later = evaluate_strategy(strategy, timeline, T2, failed.next_state, symbol="EURUSD", account_balance=10000)
    assert later.signal == "WAIT"


def test_retest_only_setup_can_wait_for_later_touch_and_close_back_above_level():
    candles = {
        T0: FakeTimeline()._candles[T0],
        T1: CandleFacts(T1, 1.1010, 1.1020, 1.1004, 1.1015, 31.25),
        T2: CandleFacts(T2, 1.1008, 1.1013, 1.0997, 1.1009, 6.25),
    }
    strategy = definition(confirmations=["RETEST_LEVEL"], entry="RETEST")
    timeline = FakeTimeline(candles=candles)
    first = evaluate_strategy(strategy, timeline, T0, empty_state(), symbol="EURUSD", account_balance=10000)
    waiting = evaluate_strategy(strategy, timeline, T1, first.next_state, symbol="EURUSD", account_balance=10000)
    assert waiting.signal == "WAIT"
    assert waiting.steps["confirmation"]["state"] == "WAITING"
    assert waiting.next_state.pending_setup is not None

    retest = evaluate_strategy(strategy, timeline, T2, waiting.next_state, symbol="EURUSD", account_balance=10000)
    assert retest.signal == "BUY"
    assert retest.entry == pytest.approx(1.1009)


def test_risk_override_is_ephemeral_for_one_evaluation():
    result = evaluate_strategy(
        definition(),
        FakeTimeline(),
        T0,
        empty_state(),
        symbol="EURUSD",
        account_balance=10000,
        risk_override={"method": "FIXED_DOLLARS", "value": 75.0},
    )
    assert result.risk_budget == {"method": "FIXED_DOLLARS", "value": 75.0, "dollars": 75.0}
