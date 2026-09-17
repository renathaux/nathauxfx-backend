"""Deterministic broker-free evaluator for saved Strategy Studio definitions."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Any

import pandas as pd


PIP_SIZE = {"EURUSD": 0.0001, "XAUUSD": 0.01}
STEP_ORDER = [
    "trend",
    "structure",
    "break_validation",
    "confirmation",
    "entry",
    "stop_loss",
    "tp1",
    "tp2",
    "risk",
]
STEP_STATES = {"PASSED", "WAITING", "BLOCKED", "NOT_APPLICABLE"}


@dataclass(frozen=True)
class EvaluationState:
    status: str
    pending_setup: dict | None


@dataclass(frozen=True)
class EvaluationResult:
    signal: str
    steps: dict[str, dict]
    setup_id: str | None
    entry: float | None
    sl: float | None
    tp1: float | None
    tp2: float | None
    risk_budget: dict | None
    next_state: EvaluationState


def empty_state() -> EvaluationState:
    return EvaluationState(status="READY", pending_setup=None)


def _step(state: str, reason: str | None = None, **facts) -> dict:
    if state not in STEP_STATES:
        raise ValueError(f"invalid evaluator step state: {state}")
    value = {"state": state}
    if reason:
        value["reason"] = reason
    if facts:
        value.update(facts)
    return value


def _steps() -> dict[str, dict]:
    return {name: _step("NOT_APPLICABLE") for name in STEP_ORDER}


def _timestamp(value) -> pd.Timestamp:
    result = pd.Timestamp(value)
    if result.tzinfo is None:
        return result.tz_localize("UTC")
    return result.tz_convert("UTC")


def _wait(steps, *, setup_id=None, pending=None, status="READY") -> EvaluationResult:
    return EvaluationResult(
        signal="WAIT",
        steps=steps,
        setup_id=setup_id,
        entry=None,
        sl=None,
        tp1=None,
        tp2=None,
        risk_budget=None,
        next_state=EvaluationState(status=status, pending_setup=pending),
    )


def _setup_id(symbol: str, event) -> str:
    raw = f"{str(symbol).upper()}|{event.timestamp.isoformat()}|{event.direction}|{event.event_type}|{event.broken_level:.12g}"
    return "setup_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def _new_pending(symbol: str, event, trigger_candle) -> dict:
    return {
        "setup_id": _setup_id(symbol, event),
        "direction": event.direction,
        "event_type": event.event_type,
        "event_timestamp": event.timestamp.isoformat(),
        "broken_level": float(event.broken_level),
        "invalidation_price": (
            float(event.invalidation_price) if event.invalidation_price is not None else None
        ),
        "trigger_close": float(trigger_candle.close),
    }


def _trend_direction(trend, method: str) -> str | None:
    return {
        "BOS_CHOCH": trend.bos_choch_direction,
        "EMA_50": trend.ema50_direction,
        "EMA_200": trend.ema200_direction,
        "SWING_STRUCTURE": trend.swing_structure_direction,
    }[method]


def _trend_passes(definition: dict, timeline, timestamp, direction: str) -> tuple[bool, dict]:
    methods = list((definition.get("trend") or {}).get("methods") or [])
    if not methods:
        return True, _step("NOT_APPLICABLE")
    trend = timeline.trend(timestamp)
    actual = {method: _trend_direction(trend, method) for method in methods}
    ok = all(actual.get(method) == direction for method in methods)
    if ok:
        return True, _step("PASSED", methods=methods, direction=direction, actual=actual)
    return False, _step("BLOCKED", "TREND_FILTER_DISAGREEMENT", methods=methods, direction=direction, actual=actual)


def _break_passes(definition: dict, event, candle, symbol: str) -> tuple[bool, dict]:
    structure = definition.get("structure") or {}
    rules = list(structure.get("break_validation") or [])
    if not rules:
        return True, _step("NOT_APPLICABLE")

    pip = PIP_SIZE[str(symbol).upper()]
    failures: list[str] = []
    if "CLOSE_BEYOND" in rules:
        beyond = candle.close > event.broken_level if event.direction == "BUY" else candle.close < event.broken_level
        if not beyond:
            failures.append("CLOSE_BEYOND")
    if "MIN_BODY_PERCENT" in rules:
        minimum = float(structure.get("minimum_body_percent") or 0.0)
        if float(candle.body_percent) < minimum:
            failures.append("MIN_BODY_PERCENT")
    if "MIN_DISTANCE" in rules:
        distance = (
            (candle.close - event.broken_level) / pip
            if event.direction == "BUY"
            else (event.broken_level - candle.close) / pip
        )
        if distance < float(structure.get("minimum_distance_pips") or 0.0):
            failures.append("MIN_DISTANCE")
    if failures:
        return False, _step("BLOCKED", "BREAK_VALIDATION_FAILED", failed_rules=failures)
    return True, _step("PASSED", rules=rules)


def _same_direction(candle, direction: str) -> bool:
    return candle.close > candle.open if direction == "BUY" else candle.close < candle.open


def _close_beyond(candle, direction: str, level: float) -> bool:
    return candle.close > level if direction == "BUY" else candle.close < level


def _retest_qualifies(candle, direction: str, level: float) -> bool:
    if direction == "BUY":
        return candle.low <= level and candle.close > level
    return candle.high >= level and candle.close < level


def _confirmation(definition: dict, timeline, timestamp, pending: dict) -> tuple[str, dict]:
    rules = list((definition.get("confirmation") or {}).get("rules") or [])
    if not rules:
        return "PASSED", _step("NOT_APPLICABLE")

    event_time = _timestamp(pending["event_timestamp"])
    current_time = _timestamp(timestamp)
    if current_time <= event_time:
        return "WAITING", _step("WAITING", "CONFIRMATION_NOT_YET_AVAILABLE")

    immediate_rules = {"NEXT_SAME_DIRECTION", "SECOND_CLOSE_BEYOND"}
    retest_required = "RETEST_LEVEL" in rules
    requires_immediate = bool(immediate_rules.intersection(rules)) or not retest_required
    next_time = timeline.next_trading_timestamp(event_time)
    if requires_immediate:
        if next_time is None or current_time < next_time:
            return "WAITING", _step("WAITING", "NEXT_TRADING_CANDLE_PENDING")
        if current_time > next_time:
            return "BLOCKED", _step("BLOCKED", "IMMEDIATE_CONFIRMATION_MISSED")

    candle = timeline.candle(current_time)
    if candle is None:
        return "WAITING", _step("WAITING", "CONFIRMATION_CANDLE_UNAVAILABLE")

    direction = pending["direction"]
    level = float(pending["broken_level"])
    failures: list[str] = []

    if "NEXT_SAME_DIRECTION" in rules and not _same_direction(candle, direction):
        failures.append("NEXT_SAME_DIRECTION")
    if "SECOND_CLOSE_BEYOND" in rules and not _close_beyond(candle, direction, level):
        failures.append("SECOND_CLOSE_BEYOND")

    if retest_required:
        if not _retest_qualifies(candle, direction, level):
            if requires_immediate:
                failures.append("RETEST_LEVEL")
            else:
                return "WAITING", _step("WAITING", "RETEST_LEVEL_PENDING")

    if "MIN_BODY_PERCENT" in rules:
        minimum = float((definition.get("confirmation") or {}).get("minimum_body_percent") or 0.0)
        if float(candle.body_percent) < minimum:
            failures.append("MIN_BODY_PERCENT")

    if failures:
        return "BLOCKED", _step("BLOCKED", "CONFIRMATION_FAILED", failed_rules=failures)
    return "PASSED", _step("PASSED", rules=rules, timestamp=current_time.isoformat())


def _plan_levels(definition: dict, timeline, timestamp, pending: dict, entry: float, symbol: str, account_balance: float, risk_override: dict | None, steps: dict):
    direction = pending["direction"]
    pip = PIP_SIZE[str(symbol).upper()]
    stop = definition.get("stop_loss") or {}
    if stop.get("method") == "LAST_SWING":
        reference = pending.get("invalidation_price")
        if reference is None:
            steps["stop_loss"] = _step("WAITING", "LAST_SWING_UNAVAILABLE")
            return None
        buffer_pips = float(stop.get("buffer_pips") or 0.0)
        sl = float(reference) - buffer_pips * pip if direction == "BUY" else float(reference) + buffer_pips * pip
    else:
        distance = float(stop.get("fixed_distance") or 0.0) * pip
        sl = entry - distance if direction == "BUY" else entry + distance

    valid_sl = sl < entry if direction == "BUY" else sl > entry
    if not valid_sl:
        steps["stop_loss"] = _step("BLOCKED", "STOP_LOSS_NOT_BEYOND_ENTRY")
        return None
    steps["stop_loss"] = _step("PASSED", value=sl)
    risk_distance = abs(entry - sl)

    tp1_def = definition.get("tp1") or {}
    tp1 = None
    if tp1_def.get("enabled"):
        target_r = float(tp1_def.get("target_r") or 0.0)
        tp1 = entry + risk_distance * target_r if direction == "BUY" else entry - risk_distance * target_r
        steps["tp1"] = _step("PASSED", value=tp1)
    else:
        steps["tp1"] = _step("NOT_APPLICABLE")

    tp2_def = definition.get("tp2") or {}
    method = tp2_def.get("method")
    if method == "FIXED_R":
        value = float(tp2_def.get("value") or 0.0)
        tp2 = entry + risk_distance * value if direction == "BUY" else entry - risk_distance * value
    elif method == "FIXED_DISTANCE":
        distance = float(tp2_def.get("value") or 0.0) * pip
        tp2 = entry + distance if direction == "BUY" else entry - distance
    elif method == "OPPOSITE_SWING":
        tp2 = timeline.nearest_opposite_swing(timestamp, direction, entry)
        if tp2 is None:
            steps["tp2"] = _step("WAITING", "TP2_OPPOSITE_SWING_UNAVAILABLE")
            return None
    else:
        steps["tp2"] = _step("BLOCKED", "TP2_METHOD_UNSUPPORTED")
        return None
    valid_tp2 = tp2 > entry if direction == "BUY" else tp2 < entry
    if not valid_tp2:
        steps["tp2"] = _step("BLOCKED", "TP2_NOT_PROFITABLE")
        return None
    steps["tp2"] = _step("PASSED", value=tp2)

    risk = dict(risk_override or definition.get("risk") or {})
    method = risk.get("method")
    value = float(risk.get("value") or 0.0)
    if value <= 0 or method not in {"PERCENT_BALANCE", "FIXED_DOLLARS"}:
        steps["risk"] = _step("BLOCKED", "RISK_BUDGET_INVALID")
        return None
    dollars = float(account_balance) * value / 100.0 if method == "PERCENT_BALANCE" else value
    if dollars <= 0:
        steps["risk"] = _step("BLOCKED", "RISK_BUDGET_INVALID")
        return None
    budget = {"method": method, "value": value, "dollars": dollars}
    steps["risk"] = _step("PASSED", **budget)
    return sl, tp1, tp2, budget


def evaluate_strategy(
    definition: dict,
    timeline,
    timestamp,
    prior_state: EvaluationState,
    *,
    symbol: str,
    account_balance: float,
    risk_override: dict | None = None,
) -> EvaluationResult:
    """Evaluate one closed trading candle without any broker-side side effect."""
    current_time = _timestamp(timestamp)
    steps = _steps()
    pending = dict(prior_state.pending_setup) if prior_state and prior_state.pending_setup else None
    event = timeline.structure_event(current_time)

    if pending and event is not None and event.direction != pending.get("direction"):
        pending = None

    if pending is None:
        if event is None:
            methods = list((definition.get("trend") or {}).get("methods") or [])
            steps["trend"] = _step("WAITING", "STRUCTURE_DIRECTION_PENDING") if methods else _step("NOT_APPLICABLE")
            steps["structure"] = _step("WAITING", "BOS_CHOCH_PENDING")
            return _wait(steps)

        trend_ok, steps["trend"] = _trend_passes(definition, timeline, current_time, event.direction)
        if not trend_ok:
            steps["structure"] = _step("PASSED", event_type=event.event_type, direction=event.direction)
            return _wait(steps, setup_id=_setup_id(symbol, event), status="BLOCKED")

        steps["structure"] = _step("PASSED", event_type=event.event_type, direction=event.direction, broken_level=event.broken_level)
        trigger_candle = timeline.candle(current_time)
        if trigger_candle is None:
            steps["break_validation"] = _step("WAITING", "TRIGGER_CANDLE_UNAVAILABLE")
            return _wait(steps, setup_id=_setup_id(symbol, event))
        break_ok, steps["break_validation"] = _break_passes(definition, event, trigger_candle, symbol)
        if not break_ok:
            return _wait(steps, setup_id=_setup_id(symbol, event), status="BLOCKED")
        pending = _new_pending(symbol, event, trigger_candle)
    else:
        steps["trend"] = _step("PASSED", reason="PENDING_SETUP_ALREADY_QUALIFIED")
        steps["structure"] = _step("PASSED", direction=pending["direction"], event_type=pending["event_type"])
        steps["break_validation"] = _step("PASSED", reason="PENDING_SETUP_ALREADY_QUALIFIED")

    setup_id = pending["setup_id"]
    confirmation_state, confirmation_step = _confirmation(definition, timeline, current_time, pending)
    steps["confirmation"] = confirmation_step
    if confirmation_state == "WAITING":
        return _wait(steps, setup_id=setup_id, pending=pending, status="WAITING")
    if confirmation_state == "BLOCKED":
        return _wait(steps, setup_id=setup_id, pending=None, status="BLOCKED")

    entry_method = (definition.get("entry") or {}).get("method")
    if entry_method == "BOS_CHOCH_CLOSE":
        entry = float(pending["trigger_close"])
    elif entry_method in {"CONFIRMATION_CLOSE", "RETEST"}:
        candle = timeline.candle(current_time)
        if candle is None:
            steps["entry"] = _step("WAITING", "ENTRY_CANDLE_UNAVAILABLE")
            return _wait(steps, setup_id=setup_id, pending=pending, status="WAITING")
        entry = float(candle.close)
    else:
        steps["entry"] = _step("BLOCKED", "ENTRY_METHOD_UNSUPPORTED")
        return _wait(steps, setup_id=setup_id, status="BLOCKED")
    steps["entry"] = _step("PASSED", method=entry_method, value=entry)

    planned = _plan_levels(
        definition,
        timeline,
        current_time,
        pending,
        entry,
        str(symbol).upper(),
        float(account_balance),
        risk_override,
        steps,
    )
    if planned is None:
        state = "WAITING" if any(item.get("state") == "WAITING" for item in steps.values()) else "BLOCKED"
        return _wait(steps, setup_id=setup_id, pending=(pending if state == "WAITING" else None), status=state)

    sl, tp1, tp2, budget = planned
    return EvaluationResult(
        signal=pending["direction"],
        steps=steps,
        setup_id=setup_id,
        entry=entry,
        sl=sl,
        tp1=tp1,
        tp2=tp2,
        risk_budget=budget,
        next_state=EvaluationState(status="READY", pending_setup=None),
    )
