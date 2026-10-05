"""Execution-boundary adapters for Strategy Studio LIVE candidates.

This module validates Strategy Studio levels without mutating them.  Broker SL
and TP2 must already satisfy cTrader distance rules; TP1 remains app-managed and
may be disabled.  It never places broker orders.
"""
from __future__ import annotations

import math

from ctrader_connector import normalize_symbol
from services.broker_execution_metadata import risk_metadata, value, number, MetadataError


def studio_risk_reward_details(symbol, action, entry, sl, tp2):
    side = str(action or "").strip().upper()
    try:
        entry_value = float(entry)
        sl_value = float(sl)
        tp2_value = float(tp2)
    except (TypeError, ValueError):
        return {"ok": False, "reason": "Strategy Studio entry, SL, and TP2 must be valid numbers"}
    if not all(math.isfinite(value) for value in (entry_value, sl_value, tp2_value)):
        return {"ok": False, "reason": "Strategy Studio levels must be finite real numbers"}
    if side == "BUY":
        risk = entry_value - sl_value
        reward = tp2_value - entry_value
    elif side == "SELL":
        risk = sl_value - entry_value
        reward = entry_value - tp2_value
    else:
        return {"ok": False, "reason": "Action must be BUY or SELL"}
    if risk <= 0 or reward <= 0:
        return {"ok": False, "reason": "LIVE BLOCKED: invalid Strategy Studio SL/TP direction."}
    return {
        "ok": True,
        "symbol": normalize_symbol(symbol),
        "action": side,
        "risk_distance": risk,
        "reward_distance": reward,
        "risk_reward_ratio": round(reward / risk, 4),
        "strategy_studio_defined_rr": True,
    }


def normalize_studio_trade_levels(symbol, action, entry, sl, tp1, tp2, *, tp1_enabled: bool, broker_metadata=None):
    public_symbol = normalize_symbol(symbol)
    side = str(action or "").strip().upper()

    def reject(reason):
        return {
            "ok": False,
            "symbol": public_symbol,
            "action": side,
            "entry": entry,
            "sl": sl,
            "tp1": tp1,
            "tp2": tp2,
            "reason": reason,
            "message": reason,
            "adjusted_for_broker_distance": False,
        }

    try:
        metadata = risk_metadata(broker_metadata)
        if metadata['symbol'] != public_symbol:
            raise MetadataError('BROKER_METADATA_SYMBOL_MISMATCH')
    except (MetadataError, TypeError, KeyError) as exc:
        return reject(str(exc) if isinstance(exc, MetadataError) else 'BROKER_METADATA_UNVERIFIED')
    if side not in {"BUY", "SELL"}:
        return reject("Action must be BUY or SELL")

    try:
        entry_value = float(entry)
        sl_value = float(sl)
        tp2_value = float(tp2)
        tp1_value = float(tp1) if tp1_enabled else None
    except (TypeError, ValueError):
        return reject("Strategy Studio entry, SL, TP1/TP2 must be valid numbers")

    numeric = [entry_value, sl_value, tp2_value]
    if tp1_value is not None:
        numeric.append(tp1_value)
    if not all(math.isfinite(value) for value in numeric):
        return reject("Strategy Studio trade levels must be finite real numbers")

    precision = int(value(broker_metadata, 'digits'))
    pip_size = metadata['pip_size']
    quantum = number(value(broker_metadata, 'tick_size'))
    if any(number(v) % quantum != 0 for v in numeric):
        return reject('BROKER_PRICE_PRECISION_INVALID')

    if side == "BUY":
        if sl_value >= entry_value:
            return reject("BUY SL must be below entry")
        if tp2_value <= entry_value:
            return reject("BUY TP2 must be above entry")
        if tp1_value is not None and not (entry_value < tp1_value <= tp2_value):
            return reject("BUY TP1 must be above entry and no farther than TP2")
        sl_distance = entry_value - sl_value
        tp2_distance = tp2_value - entry_value
        tp1_distance = tp1_value - entry_value if tp1_value is not None else None
    else:
        if sl_value <= entry_value:
            return reject("SELL SL must be above entry")
        if tp2_value >= entry_value:
            return reject("SELL TP2 must be below entry")
        if tp1_value is not None and not (entry_value > tp1_value >= tp2_value):
            return reject("SELL TP1 must be below entry and no farther than TP2")
        sl_distance = sl_value - entry_value
        tp2_distance = entry_value - tp2_value
        tp1_distance = entry_value - tp1_value if tp1_value is not None else None

    # Distance is quote-dependent, not entry-dependent. Final socket validation
    # checks the typed broker constraint against a fresh authoritative quote.
    minimum = None

    distance_details = {
        "symbol": public_symbol,
        "side": side,
        "entry_price": entry_value,
        "entry": entry_value,
        "sl_price": sl_value,
        "sl": sl_value,
        "tp_price": tp1_value,
        "tp1": tp1_value,
        "tp2_price": tp2_value,
        "tp2": tp2_value,
        "pip_size": pip_size,
        "broker_min_distance": minimum,
        "broker_minimum_distance": minimum,
        "sl_distance": round(sl_distance, precision),
        "tp1_distance": round(tp1_distance, precision) if tp1_distance is not None else None,
        "tp2_distance": round(tp2_distance, precision),
        "sl_distance_pips": round(sl_distance / pip_size, 2),
        "tp1_distance_pips": round(tp1_distance / pip_size, 2) if tp1_distance is not None else None,
        "tp2_distance_pips": round(tp2_distance / pip_size, 2),
        "broker_minimum_distance_pips": None,
        "broker_distance_validation": "REQUIRED_AT_DISPATCH",
        "failed_distance_fields": [],
        "adjusted": False,
        "studio_levels_preserved": True,
        "tp1_enabled": bool(tp1_enabled),
    }
    return {
        "ok": True,
        "symbol": public_symbol,
        "action": side,
        "entry": entry_value,
        "sl": sl_value,
        "tp1": tp1_value,
        "tp2": tp2_value,
        "adjusted_for_broker_distance": False,
        "distance_details": distance_details,
    }
