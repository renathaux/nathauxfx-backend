"""XAUUSD-specific risk-level hardening.

Gold remains a 15-minute structure strategy. The stop loss is anchored to the
immutable opposite swing owned by the accepted BOS/CHoCH event and then placed
500 quoted points ($5.00) beyond that swing. The configured minimum SL distance
is only a validation floor; it never manufactures an arbitrary stop.
"""
from __future__ import annotations

import pandas as pd

from . import shared
from . import strict_trader


XAUUSD_SL_BUFFER_PIPS = 50.0
XAUUSD_SL_BUFFER_QUOTED_POINTS = 500


def _gold_sl_buffer():
    # FlowSignal Gold quotes to 0.01. A 500-point structural buffer therefore
    # means 500 * 0.01 = $5.00 beyond the event-owned wick.
    return XAUUSD_SL_BUFFER_QUOTED_POINTS * strict_trader.point_size("XAUUSD")


def _slice_to_setup(data_15m, setup_break_time):
    if data_15m is None or data_15m.empty:
        return data_15m
    source = data_15m.copy()
    setup_timestamp = strict_trader.utc_timestamp(setup_break_time)
    if setup_timestamp is None:
        return source.iloc[:-1].copy()
    index = pd.DatetimeIndex(source.index)
    if index.tz is None:
        index = index.tz_localize("UTC")
    else:
        index = index.tz_convert("UTC")
    return source.loc[index <= setup_timestamp].copy()


def _event_owned_15m_stop(
    data_15m,
    side,
    entry,
    minimum_sl_points,
    event_invalidation_swing=None,
    setup_break_time=None,
):
    if data_15m is None or len(data_15m) < 5:
        return {
            "ok": False,
            "reason": "WAIT_NO_STRUCTURAL_SL_SWING",
            "sl_structure_source": "event_owned_15m_smc_swing",
        }

    side = str(side or "").upper()
    required = "LOW" if side == "BUY" else "HIGH"
    swing = event_invalidation_swing
    if not isinstance(swing, dict) or swing.get("price") is None:
        return {"ok": False, "reason": "WAIT_NO_STRUCTURAL_SL_SWING"}
    if str(swing.get("type") or "").upper() != required:
        return {"ok": False, "reason": "WAIT_STRUCTURAL_SL_SWING_TYPE_MISMATCH"}

    swing_price = float(swing["price"])
    swing_time = swing.get("swing_time") or swing.get("time")
    confirmation_time = swing.get("confirmation_time")

    setup_ts = strict_trader.utc_timestamp(setup_break_time)
    swing_ts = strict_trader.utc_timestamp(swing_time)
    confirmation_ts = strict_trader.utc_timestamp(confirmation_time)
    if setup_ts is not None and swing_ts is not None and swing_ts > setup_ts:
        return {
            "ok": False,
            "reason": "WAIT_SL_SWING_AFTER_SETUP",
            "sl_swing_time": swing_time,
            "setup_break_time": setup_break_time,
        }
    if setup_ts is not None and confirmation_ts is not None and confirmation_ts > setup_ts:
        return {
            "ok": False,
            "reason": "WAIT_SL_SWING_CONFIRMED_AFTER_SETUP",
            "sl_swing_confirmation_time": confirmation_time,
            "setup_break_time": setup_break_time,
        }

    entry = float(entry)
    buffer = _gold_sl_buffer()
    stop = swing_price - buffer if side == "BUY" else swing_price + buffer
    distance = abs(entry - stop)
    minimum = strict_trader.minimum_sl_distance(
        "XAUUSD",
        minimum_sl_points,
    )

    if not (stop < entry if side == "BUY" else stop > entry):
        return {"ok": False, "reason": "WAIT_15M_SWING_WRONG_SIDE"}
    if distance < minimum:
        return {
            "ok": False,
            "reason": "WAIT_SL_TOO_SMALL",
            "minimum_distance": minimum,
            "distance": distance,
            "sl_swing_used": swing_price,
            "sl_swing_time": swing_time,
        }

    return {
        "ok": True,
        "stop_loss": stop,
        "swing": {
            "type": required,
            "price": swing_price,
            "time": swing_time,
            "confirmation_time": confirmation_time,
            "source": swing.get("source"),
        },
        "distance": distance,
        "distance_points": distance / strict_trader.point_size("XAUUSD"),
        "buffer": buffer,
        "buffer_pips": XAUUSD_SL_BUFFER_PIPS,
        "sl_structure_source": "event_owned_15m_smc_swing",
    }


def build_xauusd_risk_levels(
    data_15m,
    side,
    entry,
    symbol,
    *,
    setup_break_time=None,
    execution_settings=None,
    event_invalidation_swing=None,
):
    """Build XAUUSD levels from the event-owned 15m swing + 500-point SL buffer."""
    if shared.normalize_symbol(symbol) != "XAUUSD":
        raise ValueError("build_xauusd_risk_levels is XAUUSD-only")

    configured = execution_settings or strict_trader.get_cached_execution_settings()
    configured_minimum_sl_points = configured.get(
        "minimum_sl_distance_points",
        strict_trader.MIN_SL_POINTS,
    )

    swing_source = _slice_to_setup(data_15m, setup_break_time)
    stop = _event_owned_15m_stop(
        swing_source,
        side,
        float(entry),
        configured_minimum_sl_points,
        event_invalidation_swing=event_invalidation_swing,
        setup_break_time=setup_break_time,
    )
    if not stop.get("ok"):
        return stop

    # Keep the existing 15m TP/RR selection unchanged.
    swings_15m = strict_trader.detect_valid_swings(swing_source, symbol)
    risk = float(stop["distance"])
    tp2 = strict_trader.select_tp2(
        swings_15m,
        side,
        float(entry),
        risk,
        symbol,
        minimum_rr=configured.get("minimum_rr"),
        maximum_rr=configured.get("maximum_rr"),
    )
    tp2_price = float(tp2["tp2"])
    tp1_ratio = shared.get_tp1_ratio_of_tp2()
    protected_fraction = strict_trader.PROTECTED_SL_TP2_FRACTION
    if side == "BUY":
        tp1 = float(entry) + ((tp2_price - float(entry)) * tp1_ratio)
        protected = float(entry) + ((tp2_price - float(entry)) * protected_fraction)
    else:
        tp1 = float(entry) - ((float(entry) - tp2_price) * tp1_ratio)
        protected = float(entry) - ((float(entry) - tp2_price) * protected_fraction)

    dec = strict_trader.decimals(symbol)
    return {
        "ok": True,
        "entry": round(float(entry), dec),
        "stop_loss": round(float(stop["stop_loss"]), dec),
        "tp1": round(tp1, dec),
        "tp2": round(tp2_price, dec),
        "protected_sl_price": round(protected, dec),
        "risk": round(risk, dec),
        "reward": round(abs(tp2_price - float(entry)), dec),
        "risk_reward_ratio": round(float(tp2["rr"]), 4),
        "risk_reward": f"1:{round(float(tp2['rr']), 2):g}",
        "sl_buffer": round(float(stop["buffer"]), dec),
        "sl_buffer_pips": XAUUSD_SL_BUFFER_PIPS,
        "sl_buffer_points": XAUUSD_SL_BUFFER_QUOTED_POINTS,
        "minimum_sl_points": int(configured_minimum_sl_points),
        "sl_distance_points": round(float(stop["distance_points"]), 2),
        "sl_swing_used": round(float(stop["swing"]["price"]), dec),
        "sl_swing_time": stop["swing"].get("time"),
        "sl_swing_confirmation_time": stop["swing"].get("confirmation_time"),
        "sl_swing_source": stop["swing"].get("source"),
        "sl_structure_source": stop.get("sl_structure_source"),
        "tp_structure_used": (
            round(float(tp2["swing"]["price"]), dec)
            if tp2.get("swing")
            else None
        ),
        "tp_structure_source": tp2["source"],
        "rejected_tp_candidates": tp2.get("rejected_tp_candidates", []),
        "tp1_rule": "80% of entry-to-TP2 unless admin overrides it",
        "protected_sl_rule": "50% of entry-to-TP2 after TP1 wick touch",
    }
