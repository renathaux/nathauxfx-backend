"""Experimental pure-5m Strategy Lab model.

Rule under test:
1. A CLOSED 5m candle confirms a BOS.
2. The immediately following CLOSED 5m candle must stay on the broken side of
   the BOS level and close in the same direction.
3. Enter at that second candle close.

No 15m structure, EMA, consolidation, or 15m confirmation is consulted.
Risk remains analysis-only and is built from the event-owned 5m invalidation
swing plus the existing replay RR/TP protection conventions.
"""
from __future__ import annotations

import hashlib
import json

import pandas as pd

from indicators.smc.legacy_engine import analyze_structure
from . import production_math as calc
from .baseline_v1 import resolve_trade

EVENT_CLOSE_MINUTES = 5


def _iso(value):
    stamp = pd.Timestamp(value)
    stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
    return stamp.isoformat()


def _identity(event):
    payload = {
        "symbol": "EURUSD",
        "timeframe": "5m",
        "timestamp": event["timestamp"],
        "event_type": event["event_type"],
        "direction": event["direction"],
        "broken_level": event["broken_level"],
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return "lab_5m_bos_" + digest


def _second_candle(event, frame5, end, previous_close=None):
    event_open = calc.utc(event["timestamp"])
    next_open = event_open + pd.Timedelta(minutes=5)
    entry_time = next_open + pd.Timedelta(minutes=5)
    if entry_time > end:
        return None
    if previous_close is not None and entry_time <= calc.utc(previous_close):
        return None
    try:
        candle = frame5.loc[next_open]
        if isinstance(candle, pd.DataFrame):
            candle = candle.iloc[-1]
    except KeyError:
        return None

    side = "BUY" if str(event.get("direction", "")).upper() == "BULLISH" else "SELL"
    open_price = float(candle.Open)
    close_price = float(candle.Close)
    level = float(event["broken_level"])
    same_direction = close_price > open_price if side == "BUY" else close_price < open_price
    stays_beyond = close_price > level if side == "BUY" else close_price < level
    return {
        "accepted": bool(same_direction and stays_beyond),
        "candle_time": next_open,
        "entry_time": entry_time,
        "entry": close_price,
        "same_direction": bool(same_direction),
        "stays_beyond_bos_level": bool(stays_beyond),
        "open": open_price,
        "close": close_price,
    }


def _build_5m_risk_levels(frame5, side, entry, setup_time, settings, invalidation):
    required = "LOW" if side == "BUY" else "HIGH"
    if (
        not isinstance(invalidation, dict)
        or invalidation.get("price") is None
        or str(invalidation.get("type", "")).upper() != required
    ):
        return {"ok": False, "reason": "WAIT_NO_5M_STRUCTURAL_SL_SWING"}

    setup = calc.utc(setup_time)
    for key in ("swing_time", "confirmation_time"):
        if invalidation.get(key) and calc.utc(invalidation[key]) > setup:
            return {"ok": False, "reason": "WAIT_5M_SL_SWING_AFTER_SETUP"}

    swing_price = float(invalidation["price"])
    stop = (
        swing_price - 50 * calc.POINT_SIZE
        if side == "BUY"
        else swing_price + 50 * calc.POINT_SIZE
    )
    distance = abs(float(entry) - stop)
    correct_side = stop < entry if side == "BUY" else stop > entry
    if not correct_side:
        return {"ok": False, "reason": "WAIT_5M_SWING_WRONG_SIDE"}
    minimum_sl = float(settings.get("minimum_sl_distance_points", 100)) * calc.POINT_SIZE
    if distance < minimum_sl:
        return {"ok": False, "reason": "WAIT_SL_TOO_SMALL"}

    source = frame5.loc[pd.DatetimeIndex(frame5.index) <= setup]
    swings = calc.detect_valid_swings(source)
    selected = calc.select_tp2(
        swings,
        side,
        float(entry),
        distance,
        float(settings["minimum_rr"]),
        float(settings["maximum_rr"]),
    )
    tp2 = float(selected["tp2"])
    rr = float(selected["rr"])
    swing = selected.get("swing")
    tp1_ratio = float(settings.get("tp1_percent_of_tp2", 80)) / 100.0
    tp1 = float(entry) + (tp2 - float(entry)) * tp1_ratio
    protected = float(entry) + (tp2 - float(entry)) * calc.PROTECTED_SL_TP2_FRACTION
    return {
        "ok": True,
        "entry": round(float(entry), 5),
        "stop_loss": round(float(stop), 5),
        "tp1": round(float(tp1), 5),
        "tp2": round(float(tp2), 5),
        "protected_sl_price": round(float(protected), 5),
        "risk_reward_ratio": round(rr, 4),
        "tp_structure_source": "inverse_5m_swing" if swing else selected["source"],
        "tp_structure_used": round(float(swing["price"]), 5) if swing else None,
        "sl_structure_source": "event_owned_5m_smc_swing",
    }


def candidates(frame15, frame5, start, end, settings):
    del frame15, settings
    analysis = analyze_structure(
        frame5.loc[frame5.index <= end],
        timeframe="5m",
        point_size=calc.POINT_SIZE,
    )
    for event in analysis.get("events", []):
        if str(event.get("event_type", "")).upper() != "BOS":
            continue
        timestamp = calc.utc(event["timestamp"])
        close_time = timestamp + pd.Timedelta(minutes=5)
        if close_time < start or close_time > end:
            continue
        prefix5 = frame5.loc[frame5.index <= timestamp]
        side = "BUY" if event["direction"] == "BULLISH" else "SELL"
        leg = calc.event_leg(event)
        meta = {
            "qualified": True,
            "reason": "five_minute_bos",
            "event_close_minutes": EVENT_CLOSE_MINUTES,
        }
        yield event, timestamp, prefix5, side, leg, True, meta


def evaluate_event(
    event,
    timestamp,
    prefix5,
    frame5,
    side,
    leg,
    settings,
    end,
    *,
    previous_close=None,
):
    trace = {
        "event_time": _iso(timestamp),
        "event_type": "BOS",
        "direction": event["direction"],
        "structural_leg_points": None if leg is None else leg / calc.POINT_SIZE,
        "structure_qualified": True,
        "structure_qualification": "five_minute_bos",
        "buffered_m15": None,
        "ema_allowed": None,
        "consolidation_allowed": None,
        "m5_confirmation_time": None,
        "second_5m_same_direction": None,
        "second_5m_stays_beyond_bos_level": None,
        "risk_result": None,
        "entry": None,
        "sl": None,
        "tp1": None,
        "tp2": None,
        "rr": None,
        "skipped_active_position": False,
        "skipped_previous_close_freshness": False,
        "final_action": None,
    }

    event_close = timestamp + pd.Timedelta(minutes=5)
    if previous_close is not None and event_close <= calc.utc(previous_close):
        trace.update(
            skipped_previous_close_freshness=True,
            final_action="SKIP_SETUP_BEFORE_PREVIOUS_CLOSE",
        )
        return None, "skipped_previous_position_close_freshness", trace

    confirmation = _second_candle(event, frame5, end, previous_close)
    if confirmation is None or not confirmation["accepted"]:
        if confirmation is not None:
            trace["m5_confirmation_time"] = _iso(confirmation["candle_time"])
            trace["second_5m_same_direction"] = confirmation["same_direction"]
            trace["second_5m_stays_beyond_bos_level"] = confirmation[
                "stays_beyond_bos_level"
            ]
        trace["final_action"] = "REJECT_SECOND_5M"
        return None, "rejected_by_second_5m", trace

    trace["m5_confirmation_time"] = _iso(confirmation["candle_time"])
    trace["second_5m_same_direction"] = confirmation["same_direction"]
    trace["second_5m_stays_beyond_bos_level"] = confirmation[
        "stays_beyond_bos_level"
    ]

    levels = _build_5m_risk_levels(
        prefix5,
        side,
        confirmation["entry"],
        timestamp,
        settings,
        event.get("event_invalidation_swing"),
    )
    trace["risk_result"] = (
        levels.get("reason") if not levels.get("ok") else levels["tp_structure_source"]
    )
    if not levels.get("ok"):
        trace["final_action"] = "REJECT_RISK_RR"
        return None, "rejected_by_risk_rr", trace

    trace.update(
        entry=levels["entry"],
        sl=levels["stop_loss"],
        tp1=levels["tp1"],
        tp2=levels["tp2"],
        rr=levels["risk_reward_ratio"],
        final_action="SIMULATED_TRADE",
    )
    trade = {
        "event_timestamp": _iso(timestamp),
        "event_type": "BOS",
        "side": side,
        "broken_level": float(event["broken_level"]),
        "event_structural_leg_size": leg,
        "m5_bos_close": float(event["close"]),
        "m5_confirmation_timestamp": _iso(confirmation["candle_time"]),
        "entry": levels["entry"],
        "sl": levels["stop_loss"],
        "original_sl": levels["stop_loss"],
        "tp1": levels["tp1"],
        "tp2": levels["tp2"],
        "protected_sl": levels["protected_sl_price"],
        "protected_sl_price": levels["protected_sl_price"],
        "rr": levels["risk_reward_ratio"],
        "tp_structure_source": levels["tp_structure_source"],
        "tp_structure_used": levels["tp_structure_used"],
        "entry_timestamp": _iso(confirmation["entry_time"]),
        "exit_timestamp": None,
        "exit_price": None,
        "exit_reason": None,
        "result": "UNRESOLVED_OPEN",
        "r_result": None,
        "exact_r_before_rounding": None,
        "tp1_reached": False,
        "source_event_identity": _identity(event),
        "filters_passed": [
            "5m_bos",
            "immediate_second_5m_same_direction",
            "second_5m_stays_beyond_bos_level",
            "5m_structural_risk_rr",
        ],
        "filters_failed_or_skipped": [],
    }
    return trade, None, trace


def build_trade(event, timestamp, prefix5, frame5, side, leg, settings, end):
    trade, rejection, _ = evaluate_event(
        event, timestamp, prefix5, frame5, side, leg, settings, end
    )
    return trade, rejection
