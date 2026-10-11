"""V3A: pure 5m BOS two-close model with a moderate BOS-body filter.

This keeps V3's exact two-close rule and 5m-only risk model, but requires the
BOS candle body to cover at least 50% of its full candle range. No wick filter,
EMA, session, 15m, or second-candle body filter is added so the experiment
changes only one variable.
"""
from __future__ import annotations

from . import v3_m5_two_close as base

MIN_BOS_BODY_RATIO = 0.50

candidates = base.candidates
resolve_trade = base.resolve_trade
_second_candle = base._second_candle
_build_5m_risk_levels = base._build_5m_risk_levels


def _bos_body_ratio(candle):
    high = float(candle.High)
    low = float(candle.Low)
    open_price = float(candle.Open)
    close_price = float(candle.Close)
    candle_range = high - low
    if candle_range <= 0:
        return 0.0
    return abs(close_price - open_price) / candle_range


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
    try:
        bos_candle = prefix5.loc[timestamp]
        if hasattr(bos_candle, "iloc") and getattr(bos_candle, "ndim", 1) > 1:
            bos_candle = bos_candle.iloc[-1]
        body_ratio = _bos_body_ratio(bos_candle)
    except Exception:
        body_ratio = 0.0

    if body_ratio < MIN_BOS_BODY_RATIO:
        trace = {
            "event_time": base._iso(timestamp),
            "event_type": "BOS",
            "direction": event["direction"],
            "structural_leg_points": None if leg is None else leg / base.calc.POINT_SIZE,
            "structure_qualified": True,
            "structure_qualification": "five_minute_bos",
            "buffered_m15": None,
            "ema_allowed": None,
            "consolidation_allowed": None,
            "m5_confirmation_time": None,
            "bos_body_ratio": body_ratio,
            "minimum_bos_body_ratio": MIN_BOS_BODY_RATIO,
            "risk_result": None,
            "entry": None,
            "sl": None,
            "tp1": None,
            "tp2": None,
            "rr": None,
            "skipped_active_position": False,
            "skipped_previous_close_freshness": False,
            "final_action": "REJECT_5M_BOS_BODY",
        }
        return None, "rejected_by_5m_bos_body", trace

    trade, rejection, trace = base.evaluate_event(
        event,
        timestamp,
        prefix5,
        frame5,
        side,
        leg,
        settings,
        end,
        previous_close=previous_close,
    )
    trace["bos_body_ratio"] = body_ratio
    trace["minimum_bos_body_ratio"] = MIN_BOS_BODY_RATIO
    if trade is not None:
        trade["bos_body_ratio"] = body_ratio
        trade["minimum_bos_body_ratio"] = MIN_BOS_BODY_RATIO
        trade["filters_passed"] = ["5m_bos_body_50"] + list(trade.get("filters_passed") or [])
    return trade, rejection, trace


def build_trade(event, timestamp, prefix5, frame5, side, leg, settings, end):
    trade, rejection, _ = evaluate_event(
        event, timestamp, prefix5, frame5, side, leg, settings, end
    )
    return trade, rejection
