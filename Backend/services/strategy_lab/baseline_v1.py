"""Pure, deterministic approximation of the production EURUSD baseline."""
from __future__ import annotations

import hashlib
import json

import pandas as pd

from indicators.smc.legacy_engine import analyze_structure

POINT_SIZE = 0.00001
MAX_EVENT_AGE = pd.Timedelta(minutes=60)


def _iso(value):
    return pd.Timestamp(value).tz_convert("UTC").isoformat()


def _identity(event):
    payload = {
        "symbol": "EURUSD", "timeframe": "15m",
        "timestamp": event["timestamp"], "event_type": event["event_type"],
        "direction": event["direction"], "broken_level": event["broken_level"],
    }
    return "lab_smc1_" + hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _atr14(frame):
    high, low, close = frame.High, frame.Low, frame.Close
    tr = pd.concat([high-low, (high-close.shift()).abs(), (low-close.shift()).abs()], axis=1).max(axis=1)
    return float(tr.tail(14).mean()) if len(tr) >= 14 else 0.0


def _buffer(frame, settings):
    return max(float(settings["bos_buffer_points"]) * POINT_SIZE, 0.10 * _atr14(frame))


def _ema_allowed(frame, side, settings):
    if not settings.get("ema_filter_enabled", True):
        return True
    if len(frame) < int(settings.get("ema_slow_period", 21)):
        return False
    close = frame.Close.astype(float)
    fast = close.ewm(span=int(settings.get("ema_fast_period", 9)), adjust=False).mean().iloc[-1]
    slow = close.ewm(span=int(settings.get("ema_slow_period", 21)), adjust=False).mean().iloc[-1]
    return (close.iloc[-1] > slow and fast > slow) if side == "BUY" else (close.iloc[-1] < slow and fast < slow)


def _consolidating(frame):
    if len(frame) < 21:
        return False
    recent, atr = frame.tail(8), _atr14(frame)
    overlaps = 0
    for pos in range(1, len(recent)):
        previous, current = recent.iloc[pos-1], recent.iloc[pos]
        denominator = min(previous.High-previous.Low, current.High-current.Low)
        overlap = min(previous.High, current.High)-max(previous.Low, current.Low)
        overlaps += bool(denominator > 0 and max(0.0, overlap)/denominator >= .60)
    close = frame.Close.astype(float)
    ema9, ema21 = close.ewm(span=9, adjust=False).mean(), close.ewm(span=21, adjust=False).mean()
    checks = [overlaps >= 5, recent.High.max()-recent.Low.min() <= 3*atr,
              abs(ema9.iloc[-1]-ema21.iloc[-1]) <= .2*atr and abs(ema9.iloc[-1]-ema9.iloc[-4]) <= .15*atr]
    return sum(checks) >= 2


def _leg_size(event):
    swing = event.get("event_invalidation_swing") or {}
    try:
        return abs(float(event["broken_level"])-float(swing["price"]))
    except (KeyError, TypeError, ValueError):
        return None


def _confirmation(event, frame5, buffer, end):
    side = "BUY" if event["direction"] == "BULLISH" else "SELL"
    anchor = pd.Timestamp(event["timestamp"]).tz_convert("UTC") + pd.Timedelta(minutes=15)
    for timestamp, candle in frame5.iterrows():
        close_time = timestamp + pd.Timedelta(minutes=5)
        if close_time <= anchor or close_time > anchor + MAX_EVENT_AGE or close_time > end:
            continue
        directional = candle.Close > candle.Open if side == "BUY" else candle.Close < candle.Open
        beyond = candle.Close >= event["broken_level"]+buffer if side == "BUY" else candle.Close <= event["broken_level"]-buffer
        if directional and beyond:
            return timestamp, close_time, float(candle.Close)
    return None


def _risk(event, entry, prefix15, side, settings):
    invalidation = event.get("event_invalidation_swing") or {}
    if invalidation.get("price") is None:
        return None
    sl = float(invalidation["price"]) - 50*POINT_SIZE if side == "BUY" else float(invalidation["price"]) + 50*POINT_SIZE
    risk = abs(entry-sl)
    if (side == "BUY" and sl >= entry) or (side == "SELL" and sl <= entry):
        return None
    if risk < float(settings["minimum_sl_distance_points"])*POINT_SIZE:
        return None
    rr = min(max(2.0, float(settings["minimum_rr"])), float(settings["maximum_rr"]))
    if not float(settings["minimum_rr"]) <= rr <= float(settings["maximum_rr"]):
        return None
    tp2 = entry+risk*rr if side == "BUY" else entry-risk*rr
    tp1_ratio = float(settings.get("tp1_percent_of_tp2", 80.0))/100.0
    protected_ratio = float(settings.get("protected_sl_percent_of_tp2", 50.0))/100.0
    tp1 = entry+(tp2-entry)*tp1_ratio
    protected = entry+(tp2-entry)*protected_ratio
    return sl, tp1, tp2, protected, rr


def candidates(frame15, frame5, start, end, settings):
    analysis = analyze_structure(frame15.loc[frame15.index <= end], timeframe="15m", point_size=POINT_SIZE)
    prior_small_bos = None
    for event in analysis.get("events", []):
        timestamp = pd.Timestamp(event["timestamp"]).tz_convert("UTC")
        close_time = timestamp + pd.Timedelta(minutes=15)
        if close_time < start or close_time > end:
            continue
        prefix = frame15.loc[frame15.index <= timestamp]
        side = "BUY" if event["direction"] == "BULLISH" else "SELL"
        leg = _leg_size(event)
        structure_ok = leg is not None and leg >= 100*POINT_SIZE
        # Production permits two same-direction sub-minimum BOS events. This is
        # reproduced as an explicit approximation because the production helper
        # also uses durable event identities/lifecycle state.
        if not structure_ok and event["event_type"] == "BOS":
            structure_ok = bool(prior_small_bos == side)
            prior_small_bos = side
        yield event, timestamp, prefix, side, leg, structure_ok


def build_trade(event, timestamp, prefix15, frame5, side, leg, settings, end):
    buffer = _buffer(prefix15, settings)
    buffered = float(event["close"]) >= float(event["broken_level"])+buffer if side == "BUY" else float(event["close"]) <= float(event["broken_level"])-buffer
    if not buffered:
        return None, "rejected_by_m15_buffer"
    if not _ema_allowed(prefix15, side, settings):
        return None, "rejected_by_ema"
    if settings.get("consolidation_filter_enabled", True) and _consolidating(prefix15):
        return None, "rejected_by_consolidation"
    confirmation = _confirmation(event, frame5, buffer, end)
    if not confirmation:
        return None, "rejected_by_m5_confirmation_expired"
    candle_time, entry_time, entry = confirmation
    risk = _risk(event, entry, prefix15, side, settings)
    if not risk:
        return None, "rejected_by_risk_rr"
    sl, tp1, tp2, protected, rr = risk
    return {
        "event_timestamp": _iso(timestamp), "event_type": event["event_type"],
        "side": side, "broken_level": float(event["broken_level"]),
        "event_structural_leg_size": leg, "m15_break_close": float(event["close"]),
        "m5_confirmation_timestamp": _iso(candle_time), "entry": entry,
        "sl": sl, "original_sl": sl, "tp1": tp1, "tp2": tp2,
        "protected_sl": protected, "protected_sl_price": protected,
        "rr": rr, "entry_timestamp": _iso(entry_time), "exit_timestamp": None,
        "exit_price": None, "exit_reason": None, "result": "UNRESOLVED_OPEN",
        "r_result": None, "exact_r_before_rounding": None, "tp1_reached": False,
        "source_event_identity": _identity(event),
        "filters_passed": ["structure", "m15_buffer", "ema", "consolidation", "m5_confirmation", "risk_rr"],
        "filters_failed_or_skipped": [],
    }, None


def resolve_trade(trade, frame5, end):
    entry_time = pd.Timestamp(trade["entry_timestamp"])
    tp1_reached = False
    original_sl = float(trade.get("original_sl", trade["sl"]))
    risk = abs(float(trade["entry"])-original_sl)

    def realized_r(exit_price):
        if trade["side"] == "BUY":
            return (float(exit_price)-float(trade["entry"])) / (float(trade["entry"])-original_sl)
        return (float(trade["entry"])-float(exit_price)) / (original_sl-float(trade["entry"]))
    for timestamp, candle in frame5.iterrows():
        close_time = timestamp + pd.Timedelta(minutes=5)
        if close_time <= entry_time or close_time > end:
            continue
        stop = trade["protected_sl"] if tp1_reached else trade["sl"]
        stop_hit = candle.Low <= stop if trade["side"] == "BUY" else candle.High >= stop
        protected_touched = candle.Low <= trade["protected_sl"] if trade["side"] == "BUY" else candle.High >= trade["protected_sl"]
        tp1_hit = candle.High >= trade["tp1"] if trade["side"] == "BUY" else candle.Low <= trade["tp1"]
        tp2_hit = candle.High >= trade["tp2"] if trade["side"] == "BUY" else candle.Low <= trade["tp2"]
        if (stop_hit and (tp2_hit or (tp1_hit and not tp1_reached))) or (
            not tp1_reached and protected_touched and (tp1_hit or tp2_hit)
        ):
            result, exit_price, r_value = "AMBIGUOUS_INTRABAR", None, None
        elif tp2_hit:
            result, exit_price = "FULL_TP2_WIN", float(trade["tp2"])
            r_value = realized_r(exit_price)
        elif stop_hit:
            result = "PROTECTED_WIN" if tp1_reached else "LOSS"
            exit_price = float(stop)
            r_value = realized_r(exit_price)
        elif tp1_hit:
            tp1_reached = True
            trade["tp1_reached"] = True
            continue
        else:
            continue
        trade.update(
            result=result, exit_price=exit_price, r_result=r_value,
            exact_r_before_rounding=r_value, exit_reason=result,
            exit_timestamp=_iso(close_time), tp1_reached=tp1_reached,
        )
        return
