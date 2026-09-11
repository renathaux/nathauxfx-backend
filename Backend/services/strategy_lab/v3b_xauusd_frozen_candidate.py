"""Frozen XAUUSD companion to the V3B EURUSD Strategy Lab candidate.

This is analysis-only. It preserves the Gold rules that survived the 5m, M1,
and historical tick audit:
- XAUUSD 5m BOS only; no 15m dependency.
- BOS candle body >= 50% of its full range.
- The immediately following 5m candle must close in the BOS direction and
  remain beyond the broken BOS level; entry at that candle close.
- Event-owned 5m invalidation swing.
- Gold point size = 0.01.
- Structural SL buffer = 50 Gold points = $0.50.
- Minimum stop distance = 100 Gold points = $1.00.
- Fixed TP2 = 1.90R.
- Arm protection at 70% of the TP2 path = 1.33R.
- Protected stop = 60% of the TP2 path = +1.14R.
- No partial close at the protection trigger.

Treat these constants as frozen. Any rule change requires a new strategy
version. This module does not place PAPER or LIVE orders.
"""
from __future__ import annotations

import hashlib
import json

import pandas as pd

from indicators.smc import analyze_structure
from . import v3_m5_two_close as base
from . import v3b_m5_frozen_candidate as eur_v3b
from .v3a_m5_bos_body_50 import _bos_body_ratio

POINT_SIZE = 0.01
MIN_BOS_BODY_RATIO = 0.50
SL_BUFFER_POINTS = 50
MIN_SL_POINTS = 100
TARGET_RR = 1.90
PROTECTION_TRIGGER_TP2_FRACTION = 0.70
PROTECTED_STOP_TP2_FRACTION = 0.60
EVENT_CLOSE_MINUTES = 5

_second_candle = base._second_candle
resolve_trade = eur_v3b.resolve_trade


def _identity(event):
    payload = {
        "symbol": "XAUUSD",
        "timeframe": "5m",
        "timestamp": event["timestamp"],
        "event_type": event["event_type"],
        "direction": event["direction"],
        "broken_level": event["broken_level"],
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return "lab_xauusd_5m_bos_" + digest


def _event_leg(event):
    try:
        return abs(
            float(event["broken_level"])
            - float(event["event_invalidation_swing"]["price"])
        )
    except (KeyError, TypeError, ValueError):
        return None


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
        swing_price - SL_BUFFER_POINTS * POINT_SIZE
        if side == "BUY"
        else swing_price + SL_BUFFER_POINTS * POINT_SIZE
    )
    risk = abs(float(entry) - stop)
    correct_side = stop < entry if side == "BUY" else stop > entry
    if not correct_side:
        return {"ok": False, "reason": "WAIT_5M_SWING_WRONG_SIDE"}
    if risk < MIN_SL_POINTS * POINT_SIZE:
        return {"ok": False, "reason": "WAIT_SL_TOO_SMALL"}

    sign = 1.0 if side == "BUY" else -1.0
    tp2 = float(entry) + sign * TARGET_RR * risk
    protection_trigger = float(entry) + (
        tp2 - float(entry)
    ) * PROTECTION_TRIGGER_TP2_FRACTION
    protected_stop = float(entry) + (
        tp2 - float(entry)
    ) * PROTECTED_STOP_TP2_FRACTION
    return {
        "ok": True,
        "entry": round(float(entry), 2),
        "stop_loss": round(float(stop), 2),
        "tp1": round(float(protection_trigger), 2),
        "tp2": round(float(tp2), 2),
        "protected_sl_price": round(float(protected_stop), 2),
        "risk_reward_ratio": TARGET_RR,
        "tp_structure_source": "fixed_1_9r",
        "tp_structure_used": None,
        "sl_structure_source": "event_owned_5m_smc_swing",
    }


def candidates(frame15, frame5, start, end, settings):
    del frame15, settings
    analysis = analyze_structure(
        frame5.loc[frame5.index <= end],
        timeframe="5m",
        point_size=POINT_SIZE,
    )
    for event in analysis.get("events", []):
        if str(event.get("event_type", "")).upper() != "BOS":
            continue
        timestamp = base.calc.utc(event["timestamp"])
        close_time = timestamp + pd.Timedelta(minutes=5)
        if close_time < start or close_time > end:
            continue
        prefix5 = frame5.loc[frame5.index <= timestamp]
        side = "BUY" if event["direction"] == "BULLISH" else "SELL"
        leg = _event_leg(event)
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
        "structural_leg_points": None if leg is None else leg / POINT_SIZE,
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
        "symbol": "XAUUSD",
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
        "source_event_identity": _identity(event),
        "filters_passed": [
            "5m_bos",
            "5m_bos_body_50",
            "immediate_second_5m_same_direction",
            "second_5m_stays_beyond_bos_level",
            "event_owned_5m_structural_sl",
            "gold_point_size_0_01",
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
