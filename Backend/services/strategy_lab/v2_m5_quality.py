"""Experimental Strategy V2: baseline structure plus stronger M5 confirmation quality.

Analysis-only. This module shares the production-parity structure/risk math from
baseline_v1 but changes only the M5 confirmation rule. It does not touch PAPER,
LIVE, broker state, lifecycle state, or production strategy behavior.
"""
from __future__ import annotations

import pandas as pd

from . import production_math as calc
from .baseline_v1 import MAX_EVENT_AGE, _identity, _iso, candidates, resolve_trade

# Existing FlowSignal continuation-candle quality thresholds. Kept explicit in
# this strategy version so the experiment is reproducible and auditable.
MIN_BODY_RATIO = 0.55
MAX_CLOSE_SIDE_WICK_RATIO = 0.25


def _candle_quality(candle, side):
    try:
        open_price = float(candle["Open"])
        high_price = float(candle["High"])
        low_price = float(candle["Low"])
        close_price = float(candle["Close"])
    except Exception:
        return {
            "direction_ok": False,
            "body_ratio": 0.0,
            "close_side_wick_ratio": 1.0,
        }

    candle_range = high_price - low_price
    if candle_range <= 0:
        return {
            "direction_ok": False,
            "body_ratio": 0.0,
            "close_side_wick_ratio": 1.0,
        }

    direction_ok = (
        (side == "BUY" and close_price > open_price)
        or (side == "SELL" and close_price < open_price)
    )
    upper_wick = max(0.0, high_price - max(open_price, close_price))
    lower_wick = max(0.0, min(open_price, close_price) - low_price)
    close_side_wick = upper_wick if side == "BUY" else lower_wick
    return {
        "direction_ok": direction_ok,
        "body_ratio": abs(close_price - open_price) / candle_range,
        "close_side_wick_ratio": close_side_wick / candle_range,
    }


def _quality_confirmation(event, frame5, buffer, end, not_before=None):
    side = "BUY" if event["direction"] == "BULLISH" else "SELL"
    anchor = pd.Timestamp(event["timestamp"])
    anchor = (
        anchor.tz_localize("UTC") if anchor.tzinfo is None else anchor.tz_convert("UTC")
    ) + pd.Timedelta(minutes=15)
    floor = pd.Timestamp(not_before) if not_before is not None else None
    if floor is not None:
        floor = floor.tz_localize("UTC") if floor.tzinfo is None else floor.tz_convert("UTC")

    baseline_confirmation_seen = False
    best_rejected_quality = None
    for timestamp, candle in frame5.iterrows():
        close_time = timestamp + pd.Timedelta(minutes=5)
        if (
            close_time <= anchor
            or close_time > anchor + MAX_EVENT_AGE
            or close_time > end
            or (floor is not None and close_time <= floor)
        ):
            continue

        quality = _candle_quality(candle, side)
        beyond = (
            float(candle.Close) >= float(event["broken_level"]) + buffer
            if side == "BUY"
            else float(candle.Close) <= float(event["broken_level"]) - buffer
        )
        if not quality["direction_ok"] or not beyond:
            continue

        baseline_confirmation_seen = True
        quality_passed = (
            quality["body_ratio"] >= MIN_BODY_RATIO
            and quality["close_side_wick_ratio"] <= MAX_CLOSE_SIDE_WICK_RATIO
        )
        if quality_passed:
            return {
                "found": True,
                "candle_time": timestamp,
                "entry_time": close_time,
                "entry": float(candle.Close),
                "body_ratio": quality["body_ratio"],
                "close_side_wick_ratio": quality["close_side_wick_ratio"],
                "baseline_confirmation_seen": True,
            }
        best_rejected_quality = {
            "body_ratio": quality["body_ratio"],
            "close_side_wick_ratio": quality["close_side_wick_ratio"],
        }

    return {
        "found": False,
        "baseline_confirmation_seen": baseline_confirmation_seen,
        "last_rejected_quality": best_rejected_quality,
    }


def evaluate_event(
    event,
    timestamp,
    prefix15,
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
        "event_type": event["event_type"],
        "direction": event["direction"],
        "structural_leg_points": None if leg is None else leg / calc.POINT_SIZE,
        "structure_qualified": True,
        "buffered_m15": None,
        "ema_allowed": None,
        "consolidation_allowed": None,
        "m5_confirmation_time": None,
        "m5_confirmation_body_ratio": None,
        "m5_confirmation_close_side_wick_ratio": None,
        "m5_quality_rule": {
            "minimum_body_ratio": MIN_BODY_RATIO,
            "maximum_close_side_wick_ratio": MAX_CLOSE_SIDE_WICK_RATIO,
        },
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

    buffer = calc.bos_buffer(prefix15, settings["bos_buffer_points"])
    buffered = (
        float(event["close"]) >= float(event["broken_level"]) + buffer
        if side == "BUY"
        else float(event["close"]) <= float(event["broken_level"]) - buffer
    )
    trace["buffered_m15"] = bool(buffered)
    if not buffered:
        trace["final_action"] = "REJECT_M15_BUFFER"
        return None, "rejected_by_m15_buffer", trace

    trend = calc.trend_filter(prefix15)
    allowed = trend["buy_allowed"] if side == "BUY" else trend["sell_allowed"]
    trace["ema_allowed"] = bool(allowed)
    if settings.get("ema_filter_enabled", True) and not allowed:
        trace["final_action"] = "REJECT_EMA"
        return None, "rejected_by_ema", trace

    consolidation = calc.classify_consolidation(prefix15)
    trace["consolidation_allowed"] = not consolidation["is_consolidation"]
    close_time = timestamp + pd.Timedelta(minutes=15)
    if previous_close is not None and close_time <= previous_close:
        trace.update(
            skipped_previous_close_freshness=True,
            final_action="SKIP_SETUP_BEFORE_PREVIOUS_CLOSE",
        )
        return None, "skipped_previous_position_close_freshness", trace

    confirmation = _quality_confirmation(event, frame5, buffer, end, previous_close)
    if not confirmation["found"]:
        if confirmation["baseline_confirmation_seen"]:
            rejected = confirmation.get("last_rejected_quality") or {}
            trace["m5_confirmation_body_ratio"] = rejected.get("body_ratio")
            trace["m5_confirmation_close_side_wick_ratio"] = rejected.get(
                "close_side_wick_ratio"
            )
            trace["final_action"] = "REJECT_M5_CONFIRMATION_QUALITY"
            return None, "rejected_by_m5_quality", trace
        trace["final_action"] = "REJECT_M5_CONFIRMATION_EXPIRED"
        return None, "rejected_by_m5_confirmation_expired", trace

    candle_time = confirmation["candle_time"]
    entry_time = confirmation["entry_time"]
    entry = confirmation["entry"]
    trace["m5_confirmation_time"] = _iso(candle_time)
    trace["m5_confirmation_body_ratio"] = confirmation["body_ratio"]
    trace["m5_confirmation_close_side_wick_ratio"] = confirmation[
        "close_side_wick_ratio"
    ]

    if settings.get("consolidation_filter_enabled", True) and consolidation[
        "is_consolidation"
    ]:
        trace["final_action"] = "REJECT_CONSOLIDATION"
        return None, "rejected_by_consolidation", trace

    levels = calc.build_risk_levels(
        prefix15,
        side,
        entry,
        timestamp,
        settings,
        event.get("event_invalidation_swing"),
        float(settings.get("tp1_percent_of_tp2", 80)) / 100,
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
        "event_type": event["event_type"],
        "side": side,
        "broken_level": float(event["broken_level"]),
        "event_structural_leg_size": leg,
        "m15_break_close": float(event["close"]),
        "m5_confirmation_timestamp": _iso(candle_time),
        "m5_confirmation_body_ratio": confirmation["body_ratio"],
        "m5_confirmation_close_side_wick_ratio": confirmation[
            "close_side_wick_ratio"
        ],
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
        "entry_timestamp": _iso(entry_time),
        "exit_timestamp": None,
        "exit_price": None,
        "exit_reason": None,
        "result": "UNRESOLVED_OPEN",
        "r_result": None,
        "exact_r_before_rounding": None,
        "tp1_reached": False,
        "source_event_identity": _identity(event),
        "filters_passed": [
            "structure",
            "m15_buffer",
            "ema",
            "consolidation",
            "m5_confirmation",
            "m5_confirmation_quality",
            "risk_rr",
        ],
        "filters_failed_or_skipped": [],
    }
    return trade, None, trace


def build_trade(event, timestamp, prefix15, frame5, side, leg, settings, end):
    trade, rejection, _ = evaluate_event(
        event, timestamp, prefix15, frame5, side, leg, settings, end
    )
    return trade, rejection
