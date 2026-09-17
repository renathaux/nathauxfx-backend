"""Deterministic, broker-free evaluator for saved Strategy Studio definitions."""
from __future__ import annotations

import copy
import hashlib

import pandas as pd

from services.strategy_engine.types import EvaluationResult, EvaluationState
from services.strategy_studio_schema import normalize_definition


PIP_SIZE = {"EURUSD": 0.0001, "XAUUSD": 0.01}
STEP_ORDER = [
    "trend", "structure", "break_validation", "confirmation", "entry",
    "stop_loss", "tp1", "tp2", "risk",
]


def _step(state: str, reason: str | None = None, **detail) -> dict:
    payload = {"state": state}
    if reason:
        payload["reason"] = reason
    payload.update(detail)
    return payload


def _steps() -> dict[str, dict]:
    return {key: _step("NOT_APPLICABLE") for key in STEP_ORDER}


def _result(steps, *, signal="WAIT", setup_id=None, entry=None, sl=None, tp1=None,
            tp2=None, risk_budget=None, state=None):
    return EvaluationResult(
        signal=signal,
        steps=steps,
        setup_id=setup_id,
        entry=entry,
        sl=sl,
        tp1=tp1,
        tp2=tp2,
        risk_budget=risk_budget,
        next_state=state or EvaluationState(),
    )


def _setup_id(symbol: str, setup: dict) -> str:
    raw = f"{symbol}|{setup['direction']}|{setup['event_timestamp']}|{setup['broken_level']:.12g}"
    return "setup_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _new_setup(event) -> dict:
    return {
        "direction": event.direction,
        "event_timestamp": pd.Timestamp(event.timestamp).isoformat(),
        "broken_level": float(event.broken_level),
        "invalidation_price": (
            float(event.invalidation_price) if event.invalidation_price is not None else None
        ),
        "trigger_close": float(event.trigger_close) if event.trigger_close is not None else None,
    }


def _trend_passes(definition: dict, trend, direction: str) -> tuple[bool, str | None]:
    methods = definition["trend"]["methods"]
    if not methods:
        return True, None
    values = {
        "BOS_CHOCH": trend.bos_choch_direction,
        "EMA_50": trend.ema50_direction,
        "EMA_200": trend.ema200_direction,
        "SWING_STRUCTURE": trend.swing_structure_direction,
    }
    for method in methods:
        if values.get(method) != direction:
            return False, f"TREND_{method}_DISAGREES"
    return True, None


def _beyond(close: float, level: float, direction: str) -> bool:
    return close > level if direction == "BUY" else close < level


def _same_direction(candle, direction: str) -> bool:
    return candle.close > candle.open if direction == "BUY" else candle.close < candle.open


def _break_validation(definition: dict, candle, setup: dict, pip_size: float) -> tuple[bool, str | None]:
    structure = definition["structure"]
    rules = structure["break_validation"]
    for rule in rules:
        if rule == "CLOSE_BEYOND" and not _beyond(candle.close, setup["broken_level"], setup["direction"]):
            return False, "BREAK_CLOSE_NOT_BEYOND"
        if rule == "MIN_BODY_PERCENT" and candle.body_percent < float(structure["minimum_body_percent"]):
            return False, "BREAK_BODY_TOO_SMALL"
        if rule == "MIN_DISTANCE":
            distance = (
                candle.close - setup["broken_level"]
                if setup["direction"] == "BUY"
                else setup["broken_level"] - candle.close
            ) / pip_size
            if distance < float(structure["minimum_distance_pips"]):
                return False, "BREAK_DISTANCE_TOO_SMALL"
    return True, None


def _confirmation(definition: dict, timeline, timestamp, candle, setup: dict) -> tuple[str, str | None]:
    rules = definition["confirmation"]["rules"]
    if not rules:
        return "PASSED", None

    trigger_time = pd.Timestamp(setup["event_timestamp"])
    stamp = pd.Timestamp(timestamp)
    if stamp == trigger_time:
        return "WAITING", "CONFIRMATION_PENDING"

    immediate_rules = {"NEXT_SAME_DIRECTION", "SECOND_CLOSE_BEYOND"}
    if immediate_rules.intersection(rules):
        next_time = timeline.next_timestamp(trigger_time)
        if next_time is None or stamp < pd.Timestamp(next_time):
            return "WAITING", "CONFIRMATION_PENDING"
        if stamp != pd.Timestamp(next_time):
            return "BLOCKED", "IMMEDIATE_CONFIRMATION_MISSED"

    for rule in rules:
        if rule == "NEXT_SAME_DIRECTION" and not _same_direction(candle, setup["direction"]):
            return "BLOCKED", "NEXT_CANDLE_WRONG_DIRECTION"
        if rule == "SECOND_CLOSE_BEYOND" and not _beyond(candle.close, setup["broken_level"], setup["direction"]):
            return "BLOCKED", "SECOND_CLOSE_NOT_BEYOND"
        if rule == "RETEST_LEVEL":
            touched = candle.low <= setup["broken_level"] <= candle.high
            closed_setup_side = _beyond(candle.close, setup["broken_level"], setup["direction"])
            if not (touched and closed_setup_side):
                return "WAITING", "RETEST_PENDING"
        if rule == "MIN_BODY_PERCENT" and candle.body_percent < float(definition["confirmation"]["minimum_body_percent"]):
            return "BLOCKED", "CONFIRMATION_BODY_TOO_SMALL"
    return "PASSED", None


def _entry_price(definition: dict, candle, setup: dict) -> float | None:
    method = definition["entry"]["method"]
    if method == "BOS_CHOCH_CLOSE":
        return setup.get("trigger_close") if setup.get("trigger_close") is not None else candle.close
    if method in {"CONFIRMATION_CLOSE", "RETEST"}:
        return float(candle.close)
    return None


def _stop_price(definition: dict, setup: dict, entry: float, direction: str, pip_size: float) -> float | None:
    stop = definition["stop_loss"]
    if stop["method"] == "LAST_SWING":
        base = setup.get("invalidation_price")
        if base is None:
            return None
        buffer = float(stop.get("buffer_pips") or 0.0) * pip_size
        value = float(base) - buffer if direction == "BUY" else float(base) + buffer
    else:
        distance = float(stop["fixed_distance"]) * pip_size
        value = entry - distance if direction == "BUY" else entry + distance
    if direction == "BUY" and value >= entry:
        return None
    if direction == "SELL" and value <= entry:
        return None
    return value


def _target_prices(definition: dict, timeline, timestamp, entry: float, sl: float, direction: str, pip_size: float):
    distance = abs(entry - sl)
    sign = 1.0 if direction == "BUY" else -1.0
    tp1_def = definition["tp1"]
    tp1 = entry + sign * distance * float(tp1_def["target_r"]) if tp1_def["enabled"] else None

    tp2_def = definition["tp2"]
    if tp2_def["method"] == "FIXED_R":
        tp2 = entry + sign * distance * float(tp2_def["value"])
    elif tp2_def["method"] == "FIXED_DISTANCE":
        tp2 = entry + sign * float(tp2_def["value"]) * pip_size
    else:
        tp2 = timeline.opposite_swing(timestamp, direction, entry)
    return tp1, tp2


def _risk_budget(definition: dict, account_balance: float, risk_override: dict | None) -> dict:
    risk = copy.deepcopy(risk_override if risk_override is not None else definition["risk"])
    method = risk["method"]
    value = float(risk["value"])
    dollars = float(account_balance) * value / 100.0 if method == "PERCENT_BALANCE" else value
    return {"method": method, "value": value, "dollars": dollars}


def evaluate_strategy(definition: dict, timeline, timestamp, prior_state: EvaluationState,
                      *, symbol: str, account_balance: float, risk_override: dict | None = None) -> EvaluationResult:
    value = normalize_definition(copy.deepcopy(definition))
    public_symbol = str(symbol or "").upper().replace("/", "")
    if public_symbol not in value["symbols"]:
        raise ValueError("SIMULATION_SYMBOL_NOT_ALLOWED")
    if public_symbol not in PIP_SIZE:
        raise ValueError("SIMULATION_SYMBOL_UNSUPPORTED")
    if not isinstance(account_balance, (int, float)) or float(account_balance) <= 0:
        raise ValueError("SIMULATION_BALANCE_INVALID")

    stamp = pd.Timestamp(timestamp)
    candle = timeline.candle(stamp)
    if candle is None:
        raise ValueError("SIMULATION_CANDLE_UNAVAILABLE")

    steps = _steps()
    pending = copy.deepcopy(prior_state.pending_setup) if prior_state and prior_state.pending_setup else None
    event = timeline.structure_event(stamp)

    if pending and event is not None and event.direction != pending.get("direction"):
        pending = None

    new_event = event is not None
    if new_event:
        pending = _new_setup(event)

    if pending is None:
        steps["trend"] = _step("NOT_APPLICABLE")
        steps["structure"] = _step("WAITING", "BOS_CHOCH_REQUIRED")
        return _result(steps, state=EvaluationState("WAITING", None))

    direction = pending["direction"]
    setup_id = _setup_id(public_symbol, pending)

    if new_event:
        trend_ok, trend_reason = _trend_passes(value, timeline.trend(stamp), direction)
        steps["trend"] = _step("PASSED") if trend_ok else _step("BLOCKED", trend_reason)
        steps["structure"] = _step("PASSED", event_type=event.event_type, direction=direction)
        if not trend_ok:
            return _result(steps, setup_id=setup_id, state=EvaluationState("BLOCKED", None))

        break_ok, break_reason = _break_validation(value, candle, pending, PIP_SIZE[public_symbol])
        steps["break_validation"] = _step("PASSED") if break_ok else _step("BLOCKED", break_reason)
        if not break_ok:
            return _result(steps, setup_id=setup_id, state=EvaluationState("BLOCKED", None))
    else:
        steps["trend"] = _step("PASSED") if value["trend"]["methods"] else _step("NOT_APPLICABLE")
        steps["structure"] = _step("PASSED", direction=direction)
        steps["break_validation"] = _step("PASSED") if value["structure"]["break_validation"] else _step("NOT_APPLICABLE")

    confirmation_state, confirmation_reason = _confirmation(value, timeline, stamp, candle, pending)
    if not value["confirmation"]["rules"]:
        steps["confirmation"] = _step("NOT_APPLICABLE")
    else:
        steps["confirmation"] = _step(confirmation_state, confirmation_reason)
    if confirmation_state == "WAITING":
        return _result(steps, setup_id=setup_id, state=EvaluationState("WAITING", pending))
    if confirmation_state == "BLOCKED":
        return _result(steps, setup_id=setup_id, state=EvaluationState("BLOCKED", None))

    entry = _entry_price(value, candle, pending)
    if entry is None:
        steps["entry"] = _step("BLOCKED", "ENTRY_UNAVAILABLE")
        return _result(steps, setup_id=setup_id, state=EvaluationState("BLOCKED", None))
    steps["entry"] = _step("PASSED", price=float(entry))

    sl = _stop_price(value, pending, float(entry), direction, PIP_SIZE[public_symbol])
    if sl is None:
        steps["stop_loss"] = _step("WAITING", "STOP_LOSS_UNAVAILABLE")
        return _result(steps, setup_id=setup_id, entry=float(entry), state=EvaluationState("WAITING", pending))
    steps["stop_loss"] = _step("PASSED", price=float(sl))

    tp1, tp2 = _target_prices(value, timeline, stamp, float(entry), float(sl), direction, PIP_SIZE[public_symbol])
    if value["tp1"]["enabled"]:
        steps["tp1"] = _step("PASSED", price=float(tp1))
    else:
        steps["tp1"] = _step("NOT_APPLICABLE")
    if tp2 is None:
        steps["tp2"] = _step("WAITING", "TP2_OPPOSITE_SWING_UNAVAILABLE")
        return _result(
            steps, setup_id=setup_id, entry=float(entry), sl=float(sl), tp1=tp1,
            state=EvaluationState("WAITING", pending),
        )
    steps["tp2"] = _step("PASSED", price=float(tp2))

    budget = _risk_budget(value, float(account_balance), risk_override)
    if budget["dollars"] <= 0:
        steps["risk"] = _step("BLOCKED", "RISK_BUDGET_INVALID")
        return _result(steps, setup_id=setup_id, entry=float(entry), sl=float(sl), tp1=tp1, tp2=float(tp2), state=EvaluationState("BLOCKED", None))
    steps["risk"] = _step("PASSED", **budget)

    return _result(
        steps,
        signal=direction,
        setup_id=setup_id,
        entry=float(entry),
        sl=float(sl),
        tp1=float(tp1) if tp1 is not None else None,
        tp2=float(tp2),
        risk_budget=budget,
        state=EvaluationState("READY", None),
    )
