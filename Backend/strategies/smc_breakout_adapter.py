"""Adapter that makes the new SMC structure engine authoritative for V1's 15m break gate.

Each accepted 15m BOS/CHoCH event carries its immutable invalidation swing so
stop-loss selection can reuse that exact chronological event context. The
existing strict trader keeps its EMA, consolidation, 5m confirmation, TP/RR,
risk, cooldown and broker execution safeguards.
"""
from __future__ import annotations

import pandas as pd

from indicators.smc.engine import analyze_structure as analyze_xauusd_structure
from indicators.smc.legacy_engine import analyze_structure as analyze_legacy_structure
from . import shared

MAX_FRESH_15M_CANDLES = 4
BOS_EXECUTION_MINIMUM_STRUCTURE_POINTS = 100


def _point_size(symbol):
    normalized = shared.normalize_symbol(symbol)
    configured = shared.get_strategy_decimals(normalized)
    decimals = configured if configured is not None else (2 if normalized == "XAUUSD" else 5)
    return 10 ** (-int(decimals))


def _analyze_15m(data_15m, symbol):
    analyzer = (
        analyze_xauusd_structure
        if shared.normalize_symbol(symbol) == "XAUUSD"
        else analyze_legacy_structure
    )
    return analyzer(data_15m, timeframe="15m", point_size=_point_size(symbol))


def _iso(value):
    try:
        ts = pd.Timestamp(value)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        else:
            ts = ts.tz_convert("UTC")
        return ts.isoformat()
    except Exception:
        return None


def _close_time(value):
    try:
        ts = pd.Timestamp(value)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        else:
            ts = ts.tz_convert("UTC")
        return (ts + pd.Timedelta(minutes=15)).isoformat()
    except Exception:
        return None


def _structure_swings(structure):
    current = structure.get("current_structure") or {}
    output = []
    high = current.get("high")
    low = current.get("low")
    if high is not None:
        output.append({
            "type": "HIGH",
            "price": float(high),
            "time": current.get("high_start_timestamp"),
            "index": current.get("high_start_index"),
            "valid": True,
            "valid_reason": "smc_current_structure_high",
            "swing_size": float(current.get("range") or 0.0),
        })
    if low is not None:
        output.append({
            "type": "LOW",
            "price": float(low),
            "time": current.get("low_start_timestamp"),
            "index": current.get("low_start_index"),
            "valid": True,
            "valid_reason": "smc_current_structure_low",
            "swing_size": float(current.get("range") or 0.0),
        })
    return output


def evaluate_15m_breakout(data_15m, symbol, execution_settings=None):
    """Return strict_trader-compatible breakout data from the new SMC engine."""
    result = {
        "side": "WAIT",
        "level": None,
        "break_time": None,
        "break_close_time": None,
        "break_close": None,
        "remembered": False,
        "reason": "WAIT_NO_15M_BREAK",
        "swings": [],
        "raw_swings": [],
        "structure": {},
        "breakouts": [],
        "bos_buffer": 0.0,
        "source": "smc_structure_engine",
    }
    if data_15m is None or len(data_15m) < 10:
        result["reason"] = "WAIT_NOT_ENOUGH_15M_DATA"
        return result

    try:
        structure = _analyze_15m(data_15m, symbol)
    except Exception as exc:
        result["reason"] = "WAIT_SMC_STRUCTURE_ERROR"
        result["smc_error"] = str(exc)
        return result

    current = structure.get("current_structure") or {}
    bias = str(structure.get("bias") or "NEUTRAL").upper()
    pattern = "HH_HL" if bias == "BULLISH" else "LH_LL" if bias == "BEARISH" else "NEUTRAL"
    swings = _structure_swings(structure)
    result["swings"] = swings
    result["raw_swings"] = swings
    result["structure"] = {
        "pattern": pattern,
        "bias": bias,
        "reason": "SMC_STRUCTURE_ENGINE",
        "current_structure": current,
        "source": "smc_structure_engine",
    }
    result["smc_structure"] = structure

    events = list(structure.get("events") or [])
    if not events:
        return result

    latest = events[-1]
    try:
        break_index = int(latest.get("break_index"))
        age = (len(data_15m) - 1) - break_index
    except (TypeError, ValueError):
        age = MAX_FRESH_15M_CANDLES + 1
    result["latest_smc_event"] = latest
    result["smc_event_age_15m_candles"] = age

    # Preserve the existing strict strategy's freshness window. A historical
    # line may remain visible on the chart, but it is not a fresh entry setup.
    if age < 0 or age > MAX_FRESH_15M_CANDLES:
        result["reason"] = "WAIT_NO_FRESH_15M_SMC_BREAK"
        return result

    event_type = str(latest.get("event_type") or "BOS").upper()
    try:
        structure_distance_points = float(latest.get("structure_distance_points"))
    except (TypeError, ValueError):
        structure_distance_points = None
    result["structure_distance_points"] = structure_distance_points
    result["bos_execution_minimum_structure_points"] = BOS_EXECUTION_MINIMUM_STRUCTURE_POINTS
    if (
        event_type == "BOS"
        and structure_distance_points is not None
        and structure_distance_points < BOS_EXECUTION_MINIMUM_STRUCTURE_POINTS
    ):
        result["reason"] = "WAIT_BOS_STRUCTURE_DISTANCE_UNDER_100_POINTS"
        result["execution_blocked"] = True
        return result

    # Rebuild the same accepted SMC event at the setup candle. This prevents a
    # later candle (or a later internal working swing) from becoming the SL
    # reference for an already accepted setup.
    try:
        setup_frame = data_15m.iloc[:break_index + 1].copy()
        accepted_setup_structure = _analyze_15m(setup_frame, symbol)
    except Exception as exc:
        result["reason"] = "WAIT_SMC_SETUP_STRUCTURE_ERROR"
        result["smc_error"] = str(exc)
        return result
    setup_events = list(accepted_setup_structure.get("events") or [])
    setup_event = setup_events[-1] if setup_events else None
    if not setup_event or (
        int(setup_event.get("break_index", -1)) != break_index
        or str(setup_event.get("event_type") or "").upper()
        != str(latest.get("event_type") or "").upper()
        or str(setup_event.get("direction") or "").upper()
        != str(latest.get("direction") or "").upper()
    ):
        result["reason"] = "WAIT_SMC_SETUP_STRUCTURE_MISMATCH"
        return result
    accepted_current = accepted_setup_structure.get("current_structure") or {}
    event_invalidation_swing = setup_event.get("event_invalidation_swing")
    result["accepted_setup_structure"] = accepted_setup_structure
    result["event_invalidation_swing"] = event_invalidation_swing

    direction = str(latest.get("direction") or "").upper()
    side = "BUY" if direction == "BULLISH" else "SELL" if direction == "BEARISH" else "WAIT"
    if side == "WAIT":
        return result

    level = float(latest["broken_level"])
    break_close = float(latest["close"])
    break_time = _iso(latest.get("timestamp"))
    break_close_time = _close_time(latest.get("timestamp"))
    broken_swing_type = "HIGH" if side == "BUY" else "LOW"
    broken_swing = {
        "type": broken_swing_type,
        "price": level,
        "time": latest.get("broken_swing_timestamp"),
        "index": latest.get("structure_start_index"),
        "valid": True,
        "valid_reason": "smc_broken_structure_level",
        "swing_size": float(current.get("range") or 0.0),
    }
    invalidation = accepted_current.get("low") if side == "BUY" else accepted_current.get("high")
    candidate = {
        "side": side,
        "level": level,
        "break_time": break_time,
        "break_close_time": break_close_time,
        "break_close": break_close,
        "swing": broken_swing,
        "structure": result["structure"],
        "break_type": event_type,
        "invalidation_level": float(invalidation) if invalidation is not None else None,
        "remembered": age > 0,
        "bos_buffer": 0.0,
        "source": "smc_structure_engine",
        "smc_event_age_15m_candles": age,
        "accepted_setup_structure": accepted_setup_structure,
        "event_invalidation_swing": event_invalidation_swing,
        "structure_distance_points": structure_distance_points,
    }
    result["breakouts"] = [candidate]
    result.update(candidate)
    result["swings"] = swings or [broken_swing]
    result["raw_swings"] = result["swings"]
    result["reason"] = f"15M_{event_type}_SMC_BREAK_CLOSED"
    return result
