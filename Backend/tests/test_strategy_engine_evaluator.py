import pandas as pd
import pytest

from services.strategy_engine.evaluator import evaluate_strategy
from services.strategy_engine.types import (
    CandleFacts,
    EvaluationState,
    StructureEventFacts,
    TrendFacts,
)


T0 = pd.Timestamp("2026-09-17T10:00:00Z")
T1 = pd.Timestamp("2026-09-17T10:05:00Z")


class FakeTimeline:
    def __init__(self, *, candles=None, events=None, trends=None, opposite=None):
        self.candles = candles or {}
        self.events = events or {}
        self.trends = trends or {}
        self.opposite = opposite
        self._times = sorted(set(self.candles) | set(self.events) | set(self.trends))

    def candle(self, timestamp):
        return self.candles.get(pd.Timestamp(timestamp))

    def structure_event(self, timestamp):
        return self.events.get(pd.Timestamp(timestamp))

    def trend(self, timestamp):
        return self.trends.get(pd.Timestamp(timestamp), TrendFacts(None, None, None, None))

    def previous_timestamp(self, timestamp):
        stamp = pd.Timestamp(timestamp)
        earlier = [value for value in self._times if value < stamp]
        return earlier[-1] if earlier else None

    def next_timestamp(self, timestamp):
        stamp = pd.Timestamp(timestamp)
        later = [value for value in self._times if value > stamp]
        return later[0] if later else None

    def opposite_swing(self, timestamp, direction, entry):
        return self.opposite


def candle(timestamp, open_, high, low, close):
    span = max(high - low, 1e-12)
    return CandleFacts(timestamp, open_, high, low, close, abs(close - open_) / span * 100.0)


def event(timestamp=T0, direction="BUY", broken=1.1000, invalidation=1.0950, trigger_close=1.1010):
    return StructureEventFacts(timestamp, direction, "BOS", broken, invalidation, trigger_close)


def definition():
    return {
        "schema_version": 1,
        "symbols": ["EURUSD"],
        "trading_timeframe": "5m",
        "trend": {"timeframe": None, "methods": []},
        "structure": {
            "trigger": "BOS_CHOCH",
            "break_validation": [],
            "minimum_body_percent": None,
            "minimum_distance_pips": None,
        },
        "confirmation": {"rules": [], "minimum_body_percent": None},
        "entry": {"method": "BOS_CHOCH_CLOSE"},
        "stop_loss": {"method": "LAST_SWING", "buffer_pips": 0, "fixed_distance": None},
        "tp1": {"enabled": False, "target_r": None, "close_percent": None, "protection_r": None},
        "tp2": {"method": "FIXED_R", "value": 2},
        "risk": {"method": "PERCENT_BALANCE", "value": 1},
    }


def test_no_structure_event_waits_at_structure_step():
    timeline = FakeTimeline(candles={T0: candle(T0, 1.099, 1.101, 1.098, 1.100)})
    result = evaluate_strategy(definition(), timeline, T0, EvaluationState(), symbol="EURUSD", account_balance=10000)
    assert result.signal == "WAIT"
    assert result.steps["structure"]["state"] == "WAITING"


def test_all_selected_trend_filters_must_agree():
    value = definition()
    value["trend"] = {"timeframe": "15m", "methods": ["BOS_CHOCH", "EMA_50"]}
    timeline = FakeTimeline(
        candles={T0: candle(T0, 1.099, 1.102, 1.098, 1.101)},
        events={T0: event()},
        trends={T0: TrendFacts("BUY", "SELL", "BUY", "BUY")},
    )
    result = evaluate_strategy(value, timeline, T0, EvaluationState(), symbol="EURUSD", account_balance=10000)
    assert result.signal == "WAIT"
    assert result.steps["trend"]["state"] == "BLOCKED"


def test_missing_selected_trend_fact_is_reported_unavailable():
    value = definition()
    value["trend"] = {"timeframe": "15m", "methods": ["SWING_STRUCTURE"]}
    timeline = FakeTimeline(
        candles={T0: candle(T0, 1.099, 1.102, 1.098, 1.101)},
        events={T0: event()},
        trends={T0: TrendFacts("BUY", "BUY", "BUY", None)},
    )
    result = evaluate_strategy(value, timeline, T0, EvaluationState(), symbol="EURUSD", account_balance=10000)
    assert result.signal == "WAIT"
    assert result.steps["trend"]["state"] == "BLOCKED"
    assert result.steps["trend"]["reason"] == "TREND_SWING_STRUCTURE_UNAVAILABLE"


def test_bos_close_entry_builds_sl_tp_and_risk_budget():
    timeline = FakeTimeline(
        candles={T0: candle(T0, 1.099, 1.102, 1.098, 1.101)},
        events={T0: event()},
    )
    result = evaluate_strategy(definition(), timeline, T0, EvaluationState(), symbol="EURUSD", account_balance=10000)
    assert result.signal == "BUY"
    assert result.entry == pytest.approx(1.1010)
    assert result.sl == pytest.approx(1.0950)
    assert result.tp2 == pytest.approx(1.1130)
    assert result.tp1 is None
    assert result.risk_budget["dollars"] == pytest.approx(100.0)
    assert list(result.steps) == [
        "trend", "structure", "break_validation", "confirmation", "entry",
        "stop_loss", "tp1", "tp2", "risk",
    ]


def test_break_validation_uses_and_logic():
    value = definition()
    value["structure"]["break_validation"] = ["CLOSE_BEYOND", "MIN_BODY_PERCENT", "MIN_DISTANCE"]
    value["structure"]["minimum_body_percent"] = 60
    value["structure"]["minimum_distance_pips"] = 5
    # Close is beyond the level and >5 pips away, but this candle has a small body.
    timeline = FakeTimeline(
        candles={T0: candle(T0, 1.1008, 1.1025, 1.0990, 1.1010)},
        events={T0: event()},
    )
    result = evaluate_strategy(value, timeline, T0, EvaluationState(), symbol="EURUSD", account_balance=10000)
    assert result.signal == "WAIT"
    assert result.steps["break_validation"]["state"] == "BLOCKED"


def test_next_same_direction_uses_immediate_next_candle_and_enters_on_its_close():
    value = definition()
    value["confirmation"]["rules"] = ["NEXT_SAME_DIRECTION", "SECOND_CLOSE_BEYOND"]
    value["entry"]["method"] = "CONFIRMATION_CLOSE"
    timeline = FakeTimeline(
        candles={
            T0: candle(T0, 1.099, 1.102, 1.098, 1.101),
            T1: candle(T1, 1.101, 1.105, 1.1005, 1.104),
        },
        events={T0: event()},
    )
    first = evaluate_strategy(value, timeline, T0, EvaluationState(), symbol="EURUSD", account_balance=10000)
    assert first.signal == "WAIT"
    assert first.steps["confirmation"]["state"] == "WAITING"
    second = evaluate_strategy(value, timeline, T1, first.next_state, symbol="EURUSD", account_balance=10000)
    assert second.signal == "BUY"
    assert second.entry == pytest.approx(1.104)
    assert second.steps["confirmation"]["state"] == "PASSED"


def test_failed_immediate_confirmation_blocks_that_setup():
    value = definition()
    value["confirmation"]["rules"] = ["NEXT_SAME_DIRECTION"]
    value["entry"]["method"] = "CONFIRMATION_CLOSE"
    timeline = FakeTimeline(
        candles={
            T0: candle(T0, 1.099, 1.102, 1.098, 1.101),
            T1: candle(T1, 1.101, 1.102, 1.097, 1.098),
        },
        events={T0: event()},
    )
    first = evaluate_strategy(value, timeline, T0, EvaluationState(), symbol="EURUSD", account_balance=10000)
    second = evaluate_strategy(value, timeline, T1, first.next_state, symbol="EURUSD", account_balance=10000)
    assert second.signal == "WAIT"
    assert second.steps["confirmation"]["state"] == "BLOCKED"
    assert second.next_state.pending_setup is None
