"""V3B frozen Strategy Lab research candidate.

Exact rules frozen from the current EURUSD research pass:
- 5m BOS only; no 15m dependency.
- BOS candle body >= 50% of its full range.
- The immediately following 5m candle must close in the BOS direction and
  remain beyond the broken BOS level; enter at that candle close.
- Event-owned 5m invalidation swing with a fixed 50-point buffer.
- Minimum stop distance 100 points.
- Fixed TP2 = 1.90R.
- When price reaches 70% of the TP2 path (1.33R), protection is armed.
- Protected stop = 60% of the TP2 path (1.14R).
- No partial close at the protection trigger.

This module is analysis-only.  It intentionally ignores runtime RR/TP settings
so future replay results cannot silently change when production settings change.
"""
from __future__ import annotations

import pandas as pd

from . import v3_m5_two_close as base
from .v3a_m5_bos_body_50 import _bos_body_ratio

MIN_BOS_BODY_RATIO = 0.50
SL_BUFFER_POINTS = 50
MIN_SL_POINTS = 100
TARGET_RR = 1.90
PROTECTION_TRIGGER_TP2_FRACTION = 0.70
PROTECTED_STOP_TP2_FRACTION = 0.60

candidates = base.candidates
_second_candle = base._second_candle


def _fixed_levels(side, entry, invalidation):
    required = "LOW" if side == "BUY" else "HIGH"
    if (
        not isinstance(invalidation, dict)
        or invalidation.get("price") is None
        or str(invalidation.get("type", "")).upper() != required
    ):
        return {"ok": False, "reason": "WAIT_NO_5M_STRUCTURAL_SL_SWING"}

    swing_price = float(invalidation["price"])
    stop = (
        swing_price - SL_BUFFER_POINTS * base.calc.POINT_SIZE
        if side == "BUY"
        else swing_price + SL_BUFFER_POINTS * base.calc.POINT_SIZE
    )
    risk = abs(float(entry) - stop)
    correct_side = stop < entry if side == "BUY" else stop > entry
    if not correct_side:
        return {"ok": False, "reason": "WAIT_5M_SWING_WRONG_SIDE"}
    if risk < MIN_SL_POINTS * base.calc.POINT_SIZE:
        return {"ok": False, "reason": "WAIT_SL_TOO_SMALL"}

    sign = 1.0 if side == "BUY" else -1.0
    tp2 = float(entry) + sign * TARGET_RR * risk
    protection_trigger = float(entry) + (tp2 - float(entry)) * PROTECTION_TRIGGER_TP2_FRACTION
    protected_stop = float(entry) + (tp2 - float(entry)) * PROTECTED_STOP_TP2_FRACTION
    return {
        "ok": True,
        "entry": round(float(entry), 5),
        "stop_loss": round(float(stop), 5),
        "tp1": round(float(protection_trigger), 5),
        "tp2": round(float(tp2), 5),
        "protected_sl_price": round(float(protected_stop), 5),
        "risk_reward_ratio": TARGET_RR,
        "tp_structure_source": "fixed_1_9r",
        "tp_structure_used": None,
        "sl_structure_source": "event_owned_5m_smc_swing",
    }


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
    del settings
    try:
        bos_candle = prefix5.loc[timestamp]
        if hasattr(bos_candle, "iloc") and getattr(bos_candle, "ndim", 1) > 1:
            bos_candle = bos_candle.iloc[-1]
        body_ratio = _bos_body_ratio(bos_candle)
    except Exception:
        body_ratio = 0.0

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
        "final_action": None,
    }

    if body_ratio < MIN_BOS_BODY_RATIO:
        trace["final_action"] = "REJECT_5M_BOS_BODY"
        return None, "rejected_by_5m_bos_body", trace

    event_close = timestamp + pd.Timedelta(minutes=5)
    if previous_close is not None and event_close <= base.calc.utc(previous_close):
        trace.update(
            skipped_previous_close_freshness=True,
            final_action="SKIP_SETUP_BEFORE_PREVIOUS_CLOSE",
        )
        return None, "skipped_previous_position_close_freshness", trace

    confirmation = _second_candle(event, frame5, end, previous_close)
    if confirmation is None or not confirmation["accepted"]:
        if confirmation is not None:
            trace["m5_confirmation_time"] = base._iso(confirmation["candle_time"])
            trace["second_5m_same_direction"] = confirmation["same_direction"]
            trace["second_5m_stays_beyond_bos_level"] = confirmation[
                "stays_beyond_bos_level"
            ]
        trace["final_action"] = "REJECT_SECOND_5M"
        return None, "rejected_by_second_5m", trace

    levels = _fixed_levels(
        side,
        confirmation["entry"],
        event.get("event_invalidation_swing"),
    )
    trace["m5_confirmation_time"] = base._iso(confirmation["candle_time"])
    trace["second_5m_same_direction"] = confirmation["same_direction"]
    trace["second_5m_stays_beyond_bos_level"] = confirmation[
        "stays_beyond_bos_level"
    ]
    trace["risk_result"] = levels.get("reason") if not levels.get("ok") else "fixed_1_9r"
    if not levels.get("ok"):
        trace["final_action"] = "REJECT_RISK_RR"
        return None, "rejected_by_risk_rr", trace

    trace.update(
        entry=levels["entry"],
        sl=levels["stop_loss"],
        tp1=levels["tp1"],
        tp2=levels["tp2"],
        rr=TARGET_RR,
        final_action="SIMULATED_TRADE",
    )
    trade = {
        "event_timestamp": base._iso(timestamp),
        "event_type": "BOS",
        "side": side,
        "broken_level": float(event["broken_level"]),
        "event_structural_leg_size": leg,
        "m5_bos_close": float(event["close"]),
        "m5_confirmation_timestamp": base._iso(confirmation["candle_time"]),
        "bos_body_ratio": body_ratio,
        "minimum_bos_body_ratio": MIN_BOS_BODY_RATIO,
        "entry": levels["entry"],
        "sl": levels["stop_loss"],
        "original_sl": levels["stop_loss"],
        "tp1": levels["tp1"],
        "tp2": levels["tp2"],
        "protected_sl": levels["protected_sl_price"],
        "protected_sl_price": levels["protected_sl_price"],
        "rr": TARGET_RR,
        "tp_structure_source": "fixed_1_9r",
        "tp_structure_used": None,
        "entry_timestamp": base._iso(confirmation["entry_time"]),
        "exit_timestamp": None,
        "exit_price": None,
        "exit_reason": None,
        "result": "UNRESOLVED_OPEN",
        "r_result": None,
        "exact_r_before_rounding": None,
        "tp1_reached": False,
        "protection_armed": False,
        "protection_trigger_tp2_fraction": PROTECTION_TRIGGER_TP2_FRACTION,
        "protected_stop_tp2_fraction": PROTECTED_STOP_TP2_FRACTION,
        "source_event_identity": base._identity(event),
        "filters_passed": [
            "5m_bos",
            "5m_bos_body_50",
            "immediate_second_5m_same_direction",
            "second_5m_stays_beyond_bos_level",
            "event_owned_5m_structural_sl",
            "fixed_1_9r_target",
        ],
        "filters_failed_or_skipped": [],
    }
    return trade, None, trace


def build_trade(event, timestamp, prefix5, frame5, side, leg, settings, end):
    trade, rejection, _ = evaluate_event(
        event, timestamp, prefix5, frame5, side, leg, settings, end
    )
    return trade, rejection


def resolve_trade(trade, frame5, end):
    """Resolve V3B conservatively from 5m OHLC.

    If one candle touches levels whose ordering matters (for example trigger and
    stop, or protected stop and TP2), mark the outcome ambiguous instead of
    assuming a favorable intrabar path.
    """
    entry_time = pd.Timestamp(trade["entry_timestamp"])
    armed = False
    original_sl = float(trade["original_sl"])
    trigger = float(trade["tp1"])
    protected = float(trade["protected_sl"])
    tp2 = float(trade["tp2"])

    def realized(price):
        if trade["side"] == "BUY":
            return (float(price) - trade["entry"]) / (trade["entry"] - original_sl)
        return (trade["entry"] - float(price)) / (original_sl - trade["entry"])

    for timestamp, candle in frame5.iterrows():
        close_time = timestamp + pd.Timedelta(minutes=5)
        if close_time <= entry_time or close_time > end:
            continue

        if trade["side"] == "BUY":
            original_stop_hit = candle.Low <= original_sl
            trigger_hit = candle.High >= trigger
            protected_hit = candle.Low <= protected
            tp2_hit = candle.High >= tp2
        else:
            original_stop_hit = candle.High >= original_sl
            trigger_hit = candle.Low <= trigger
            protected_hit = candle.High >= protected
            tp2_hit = candle.Low <= tp2

        if not armed:
            if original_stop_hit and (trigger_hit or tp2_hit):
                result, price, r = "AMBIGUOUS_INTRABAR", None, None
            elif tp2_hit and protected_hit:
                result, price, r = "AMBIGUOUS_INTRABAR", None, None
            elif tp2_hit:
                result, price = "FULL_TP2_WIN", tp2
                r = realized(price)
            elif original_stop_hit:
                result, price = "LOSS", original_sl
                r = realized(price)
            elif trigger_hit and protected_hit:
                result, price, r = "AMBIGUOUS_INTRABAR", None, None
            elif trigger_hit:
                armed = True
                trade["tp1_reached"] = True
                trade["protection_armed"] = True
                continue
            else:
                continue
        else:
            if protected_hit and tp2_hit:
                result, price, r = "AMBIGUOUS_INTRABAR", None, None
            elif tp2_hit:
                result, price = "FULL_TP2_WIN", tp2
                r = realized(price)
            elif protected_hit:
                result, price = "PROTECTED_WIN", protected
                r = realized(price)
            else:
                continue

        trade.update(
            result=result,
            exit_price=price,
            r_result=r,
            exact_r_before_rounding=r,
            exit_reason=result,
            exit_timestamp=base._iso(close_time),
            tp1_reached=armed,
            protection_armed=armed,
        )
        return
