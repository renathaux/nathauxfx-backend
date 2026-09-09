import hashlib
import json
import math
from datetime import datetime, timezone

import pandas as pd

from . import shared
from services.strategy_settings_service import (
    get_cached_execution_settings,
    get_configured_rr_window,
)
from services.indicator_event_stream_service import update_event_lifecycle


MIN_SWING_POINTS = 100
SL_BUFFER_POINTS = 50
MIN_SL_POINTS = 100
DEFAULT_FALLBACK_RR = 2.00
PULLBACK_MIN_POINTS = 30
BOS_MIN_BUFFER_POINTS = 10
REMEMBERED_BREAKOUT_MAX_15M_CANDLES = 4
PROTECTED_SL_TP2_FRACTION = 0.50

STAGE_NOT_EVALUATED = "NOT_EVALUATED"
STAGE_PASSED = "PASSED"
STAGE_FAILED = "FAILED"
STAGE_BLOCKED = "BLOCKED"
BLOCKED_BREAKOUT_STATUS = "BLOCKED_BY_CONSOLIDATION"


def record_indicator_setup_status(
    event_id,
    status,
    reason=None,
    *,
    confirmation=None,
    signal_setup_id=None,
    mode="LIVE",
):
    confirmation = confirmation if isinstance(confirmation, dict) else {}
    return update_event_lifecycle(
        event_id,
        mode,
        status,
        blocking_reason=reason,
        m5_confirmation_id=confirmation.get("confirmation_id"),
        m5_confirmation_identity=confirmation.get("confirmation_identity"),
        signal_setup_id=signal_setup_id,
    )


def point_size(symbol):
    try:
        size = 10 ** (-decimals(symbol))
    except (TypeError, ValueError, OverflowError):
        try:
            size = float(shared.get_strategy_pip_size(symbol))
        except (TypeError, ValueError):
            size = 0.0001
    return size if math.isfinite(size) and size > 0 else 0.0001


def decimals(symbol):
    try:
        value = int(shared.get_strategy_decimals(symbol))
    except (TypeError, ValueError):
        value = 2 if shared.normalize_symbol(symbol) == "XAUUSD" else 5
    return value


def minimum_swing_size(symbol):
    return MIN_SWING_POINTS * point_size(symbol)


def sl_buffer(symbol):
    return SL_BUFFER_POINTS * point_size(symbol)


def minimum_sl_distance(symbol, configured_points=None):
    points = MIN_SL_POINTS if configured_points is None else configured_points
    try:
        points = float(points)
        if not math.isfinite(points) or points < 1:
            raise ValueError("invalid minimum SL points")
    except (TypeError, ValueError):
        points = MIN_SL_POINTS
    return points * point_size(symbol)


def atr14(data):
    if data is None or len(data) < 14:
        return None
    high = data["High"].astype(float)
    low = data["Low"].astype(float)
    close = data["Close"].astype(float)
    previous_close = close.shift(1)
    true_range = pd.concat(
        [
            high - low,
            (high - previous_close).abs(),
            (low - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    value = true_range.tail(14).mean()
    return float(value) if pd.notna(value) and value > 0 else None


def bos_buffer(data_15m, symbol, configured_floor_points=None):
    atr = atr14(data_15m) or 0.0
    floor_points = (
        BOS_MIN_BUFFER_POINTS
        if configured_floor_points is None
        else configured_floor_points
    )
    try:
        floor_points = float(floor_points)
        if not math.isfinite(floor_points) or floor_points < 0:
            raise ValueError("invalid BOS floor points")
    except (TypeError, ValueError):
        floor_points = BOS_MIN_BUFFER_POINTS
    return max(floor_points * point_size(symbol), 0.10 * atr)


def minimum_pullback_size(data_15m, symbol):
    atr = atr14(data_15m) or 0.0
    return max(PULLBACK_MIN_POINTS * point_size(symbol), 0.25 * atr)


def closed_frame(data, minutes):
    return shared.remove_current_forming_candle(data, minutes)


def candle_time(index_value):
    try:
        return pd.Timestamp(index_value).isoformat()
    except Exception:
        return None


def candle_close_time(index_value, minutes):
    try:
        timestamp = pd.Timestamp(index_value)
        if timestamp.tzinfo is None:
            timestamp = timestamp.tz_localize("UTC")
        else:
            timestamp = timestamp.tz_convert("UTC")
        return (timestamp + pd.Timedelta(minutes=minutes)).isoformat()
    except Exception:
        return None


def utc_timestamp(value):
    if value in [None, "", "--"]:
        return None
    try:
        timestamp = pd.Timestamp(value)
        if timestamp.tzinfo is None:
            timestamp = timestamp.tz_localize("UTC")
        else:
            timestamp = timestamp.tz_convert("UTC")
        return timestamp
    except Exception:
        return None


def last_position_closed_time(symbol):
    try:
        value = float(shared.LAST_POSITION_CLOSED_AT.get(shared.normalize_symbol(symbol), 0) or 0)
        return pd.Timestamp(value, unit="s", tz="UTC") if value > 0 else None
    except (AttributeError, TypeError, ValueError):
        return None


def trend_filter(data_15m, symbol):
    if data_15m is None or data_15m.empty or len(data_15m) < 21:
        return {
            "trend": "NEUTRAL",
            "buy_allowed": False,
            "sell_allowed": False,
            "reason": "WAIT_NO_15M_TREND",
        }

    close = data_15m["Close"].astype(float)
    ema_fast = close.ewm(span=9, adjust=False).mean().iloc[-1]
    ema_slow = close.ewm(span=21, adjust=False).mean().iloc[-1]
    last_close = close.iloc[-1]

    bullish = last_close > ema_slow and ema_fast > ema_slow
    bearish = last_close < ema_slow and ema_fast < ema_slow

    return {
        "trend": "BULLISH" if bullish else "BEARISH" if bearish else "NEUTRAL",
        "buy_allowed": bullish,
        "sell_allowed": bearish,
        "ema_fast": round(float(ema_fast), decimals(symbol)),
        "ema_slow": round(float(ema_slow), decimals(symbol)),
        "close": round(float(last_close), decimals(symbol)),
        "reason": None if bullish or bearish else "WAIT_EMA_NEUTRAL",
    }


def detect_raw_swings(data_15m, symbol, left=2, right=2):
    if data_15m is None or len(data_15m) < left + right + 3:
        return []

    min_size = minimum_swing_size(symbol)
    highs = data_15m["High"].astype(float).tolist()
    lows = data_15m["Low"].astype(float).tolist()
    index = list(data_15m.index)
    raw = []

    for pos in range(left, len(data_15m) - right):
        high_window = highs[pos - left:pos + right + 1]
        low_window = lows[pos - left:pos + right + 1]
        high = highs[pos]
        low = lows[pos]

        if high == max(high_window) and high > max(highs[pos - left:pos] + highs[pos + 1:pos + right + 1]):
            raw.append({
                "type": "HIGH",
                "price": high,
                "index": pos,
                "time": candle_time(index[pos]),
            })

        if low == min(low_window) and low < min(lows[pos - left:pos] + lows[pos + 1:pos + right + 1]):
            raw.append({
                "type": "LOW",
                "price": low,
                "index": pos,
                "time": candle_time(index[pos]),
            })

    raw.sort(key=lambda item: item["index"])
    return qualify_raw_swings(raw, data_15m, symbol)


def qualify_raw_swings(raw, data_15m, symbol):
    """Mark real legs without letting a tiny intervening pivot reset the anchor."""
    min_size = minimum_swing_size(symbol)
    accepted = []

    for swing in raw:
        opposite_type = "LOW" if swing["type"] == "HIGH" else "HIGH"
        # Measure from the nearest already-qualified opposite pivot.  Do not
        # skip a recent local leg merely to find an older, farther anchor that
        # makes the candidate appear large enough.
        opposite = next(
            (
                candidate
                for candidate in reversed(accepted)
                if candidate.get("type") == opposite_type
            ),
            None,
        )

        if opposite is not None:
            valid_size = abs(float(swing["price"]) - float(opposite["price"]))
        else:
            row = data_15m.iloc[swing["index"]]
            valid_size = abs(float(row["High"]) - float(row["Low"]))

        swing["swing_size"] = valid_size
        swing["valid"] = valid_size >= min_size
        swing["valid_reason"] = (
            "single_100_point_swing"
            if swing["valid"]
            else None
        )

        swing["reference_swing"] = opposite
        if swing["valid"]:
            accepted.append(swing)

    return raw


def detect_valid_swings(data_15m, symbol, left=2, right=2):
    return [
        swing
        for swing in detect_raw_swings(data_15m, symbol, left=left, right=right)
        if swing.get("valid")
    ]


def latest_swing(swings, swing_type):
    candidates = [s for s in swings if s.get("type") == swing_type]
    return candidates[-1] if candidates else None


def older_swings(swings, swing_type):
    return [s for s in swings if s.get("type") == swing_type]


def detect_swing_structure(swings):
    highs = [s for s in swings if s.get("type") == "HIGH"]
    lows = [s for s in swings if s.get("type") == "LOW"]
    structure = {
        "pattern": "NEUTRAL",
        "bias": "NEUTRAL",
        "hh": False,
        "hl": False,
        "lh": False,
        "ll": False,
        "previous_high": None,
        "last_high": None,
        "previous_low": None,
        "last_low": None,
        "reason": "WAIT_NEED_TWO_VALID_HIGHS_AND_LOWS",
    }
    if len(highs) < 2 or len(lows) < 2:
        return structure

    previous_high = highs[-2]
    last_high = highs[-1]
    previous_low = lows[-2]
    last_low = lows[-1]
    hh = float(last_high["price"]) > float(previous_high["price"])
    hl = float(last_low["price"]) > float(previous_low["price"])
    lh = float(last_high["price"]) < float(previous_high["price"])
    ll = float(last_low["price"]) < float(previous_low["price"])

    structure.update({
        "hh": hh,
        "hl": hl,
        "lh": lh,
        "ll": ll,
        "previous_high": previous_high,
        "last_high": last_high,
        "previous_low": previous_low,
        "last_low": last_low,
        "reason": "WAIT_NO_CLEAR_HH_HL_OR_LH_LL_STRUCTURE",
    })
    if hh and hl:
        structure.update({
            "pattern": "HH_HL",
            "bias": "BULLISH",
            "reason": "BULLISH_HH_HL_CONFIRMED",
        })
    elif lh and ll:
        structure.update({
            "pattern": "LH_LL",
            "bias": "BEARISH",
            "reason": "BEARISH_LH_LL_CONFIRMED",
        })
    return structure


def validate_continuation_structure(raw_swings, data_15m, symbol):
    base = {
        "pattern": "NEUTRAL",
        "bias": "NEUTRAL",
        "reason": "WAIT_NO_CLEAR_HH_HL_OR_LH_LL_STRUCTURE",
        "completed_impulse_size": None,
        "completed_impulse_points": None,
        "pullback_size": None,
        "pullback_points": None,
        "minimum_pullback_size": minimum_pullback_size(data_15m, symbol),
        "minimum_impulse_size": minimum_swing_size(symbol),
        "sequence": [],
    }
    if len(raw_swings) < 4:
        return base

    sequence = raw_swings[-4:]
    types = [swing.get("type") for swing in sequence]
    base["sequence"] = sequence
    bullish_sequence = types == ["HIGH", "LOW", "HIGH", "LOW"]
    bearish_sequence = types == ["LOW", "HIGH", "LOW", "HIGH"]

    if bullish_sequence:
        previous_high, previous_low, last_high, last_low = sequence
        if float(last_high["price"]) <= float(previous_high["price"]):
            return base
        side = "BUY"
        pattern = "HH_HL"
        impulse_size = float(last_high["price"]) - float(previous_low["price"])
        pullback_size = float(last_high["price"]) - float(last_low["price"])
        preserved_structure = float(last_low["price"]) > float(previous_low["price"])
        invalidation_level = float(last_low["price"])
        pullback_closes = data_15m.iloc[int(last_low["index"]) + 1:]["Close"].astype(float)
        invalidated = bool(
            not pullback_closes.empty
            and (pullback_closes < invalidation_level).any()
        )
    elif bearish_sequence:
        previous_low, previous_high, last_low, last_high = sequence
        if float(last_low["price"]) >= float(previous_low["price"]):
            return base
        side = "SELL"
        pattern = "LH_LL"
        impulse_size = float(previous_high["price"]) - float(last_low["price"])
        pullback_size = float(last_high["price"]) - float(last_low["price"])
        preserved_structure = float(last_high["price"]) < float(previous_high["price"])
        invalidation_level = float(last_high["price"])
        pullback_closes = data_15m.iloc[int(last_high["index"]) + 1:]["Close"].astype(float)
        invalidated = bool(
            not pullback_closes.empty
            and (pullback_closes > invalidation_level).any()
        )
    else:
        return base

    point = point_size(symbol)
    minimum_impulse = minimum_swing_size(symbol)
    minimum_pullback = minimum_pullback_size(data_15m, symbol)
    base.update({
        "pattern": pattern,
        "bias": "BULLISH" if side == "BUY" else "BEARISH",
        "side": side,
        "completed_impulse_size": impulse_size,
        "completed_impulse_points": impulse_size / point,
        "pullback_size": pullback_size,
        "pullback_points": pullback_size / point,
        "invalidation_level": invalidation_level,
    })

    if impulse_size < minimum_impulse:
        base["reason"] = "WAIT_NO_VALID_100_POINT_IMPULSE"
        return base
    if pullback_size < minimum_pullback:
        base["reason"] = "WAIT_PULLBACK_TOO_SMALL"
        return base
    if pullback_size >= impulse_size:
        base["reason"] = "WAIT_PULLBACK_TOO_LARGE"
        return base
    if not preserved_structure or invalidated:
        base["reason"] = "WAIT_STRUCTURE_INVALIDATED"
        return base

    base.update({
        "valid": True,
        "reason": (
            "BULLISH_HH_HL_VALID_IMPULSE_AND_PULLBACK"
            if side == "BUY"
            else "BEARISH_LH_LL_VALID_IMPULSE_AND_PULLBACK"
        ),
    })
    return base


def classify_consolidation(data_15m, symbol):
    result = {
        "is_consolidation": False,
        "reason": None,
        "high_overlap": False,
        "compressed_range": False,
        "ema_compressed": False,
        "conditions_met": 0,
    }
    if data_15m is None or len(data_15m) < 21:
        return result

    recent = data_15m.tail(8)
    atr = atr14(data_15m)
    if atr is None:
        return result

    overlap_count = 0
    for index in range(1, len(recent)):
        previous = recent.iloc[index - 1]
        current = recent.iloc[index]
        previous_range = float(previous["High"]) - float(previous["Low"])
        current_range = float(current["High"]) - float(current["Low"])
        denominator = min(previous_range, current_range)
        overlap = min(float(previous["High"]), float(current["High"])) - max(
            float(previous["Low"]), float(current["Low"])
        )
        ratio = max(0.0, overlap) / denominator if denominator > 0 else 0.0
        if ratio >= 0.60:
            overlap_count += 1

    eight_candle_range = float(recent["High"].max() - recent["Low"].min())
    close = data_15m["Close"].astype(float)
    ema9 = close.ewm(span=9, adjust=False).mean()
    ema21 = close.ewm(span=21, adjust=False).mean()
    ema_spread = abs(float(ema9.iloc[-1]) - float(ema21.iloc[-1]))
    ema9_slope = abs(float(ema9.iloc[-1]) - float(ema9.iloc[-4]))

    result.update({
        "atr14": atr,
        "overlap_pairs": overlap_count,
        "high_overlap": overlap_count >= 5,
        "eight_candle_range": eight_candle_range,
        "compressed_range": eight_candle_range <= 3.0 * atr,
        "ema9": float(ema9.iloc[-1]),
        "ema21": float(ema21.iloc[-1]),
        "ema_spread": ema_spread,
        "ema9_three_candle_slope": ema9_slope,
        "ema_compressed": (
            ema_spread <= 0.20 * atr
            and ema9_slope <= 0.15 * atr
        ),
    })
    result["conditions_met"] = sum(
        bool(result[key])
        for key in ["high_overlap", "compressed_range", "ema_compressed"]
    )
    result["is_consolidation"] = result["conditions_met"] >= 2
    result["reason"] = "WAIT_CONSOLIDATION" if result["is_consolidation"] else None
    result["symbol"] = shared.normalize_symbol(symbol)
    return result


def get_watch_key(symbol, side):
    return shared.get_15m_swing_watch_key(symbol, side)


def clear_opposite_watch(symbol, side, reason):
    opposite = "SELL" if side == "BUY" else "BUY"
    key = get_watch_key(symbol, opposite)
    if key in shared.FIFTEEN_M_SWING_WATCH:
        shared.FIFTEEN_M_SWING_WATCH.pop(key, None)
        shared.save_fifteen_m_swing_watch()
        return True
    return False


def clear_breakout_watch(symbol, side, reason):
    key = get_watch_key(symbol, side)
    if key not in shared.FIFTEEN_M_SWING_WATCH:
        return False
    shared.FIFTEEN_M_SWING_WATCH.pop(key, None)
    shared.save_fifteen_m_swing_watch()
    print("STRICT_BREAKOUT_WATCH_CLEARED =", {
        "symbol": shared.normalize_symbol(symbol),
        "side": side,
        "reason": reason,
    })
    return True


def clear_symbol_breakout_watches(symbol, reason):
    changed = False
    for side in ["BUY", "SELL"]:
        changed = clear_breakout_watch(symbol, side, reason) or changed
    return changed


def save_remembered_breakout(
    symbol,
    side,
    level,
    break_time,
    break_close,
    reason,
    break_close_time=None,
    required_buffer=None,
    swing=None,
    break_type=None,
    invalidation_level=None,
    status="PENDING",
    indicator_event_id=None,
    indicator_event_identity=None,
):
    close_timestamp = utc_timestamp(
        break_close_time or candle_close_time(break_time, 15)
    )
    expires_at = (
        close_timestamp
        + pd.Timedelta(
            minutes=15 * REMEMBERED_BREAKOUT_MAX_15M_CANDLES
        )
        if close_timestamp is not None
        else None
    )
    key = get_watch_key(symbol, side)
    swing_identity = None
    if isinstance(swing, dict):
        swing_identity = {
            "type": swing.get("type"),
            "time": swing.get("time"),
            "price": float(swing.get("price")) if swing.get("price") is not None else None,
            "swing_size": (
                float(swing.get("swing_size"))
                if swing.get("swing_size") is not None
                else None
            ),
        }
    normalized_status = str(status or "PENDING").upper()
    shared.FIFTEEN_M_SWING_WATCH[key] = {
        "symbol": shared.normalize_symbol(symbol),
        "side": side,
        "direction": side,
        "swing_level": float(level),
        "swing_timestamp": (swing_identity or {}).get("time"),
        "swing_price": (swing_identity or {}).get("price"),
        "swing_size": (swing_identity or {}).get("swing_size"),
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "break_confirmed": True,
        "break_candle_time": break_time,
        "break_timestamp": break_time,
        "break_close_time": break_close_time,
        "break_close": float(break_close),
        "break_price": float(break_close),
        "bos_buffer": float(required_buffer or 0.0),
        "swing": swing_identity,
        "break_type": break_type,
        "invalidation_level": invalidation_level,
        "expires_at": expires_at.isoformat() if expires_at is not None else None,
        "maximum_closed_15m_candles": REMEMBERED_BREAKOUT_MAX_15M_CANDLES,
        "status": normalized_status,
        "reason": reason,
        "indicator_event_id": indicator_event_id,
        "indicator_event_identity": indicator_event_identity,
    }
    shared.save_fifteen_m_swing_watch()


def remembered_breakout(symbol, side, current_close_time=None, current_close=None):
    watch = shared.FIFTEEN_M_SWING_WATCH.get(get_watch_key(symbol, side))
    if not isinstance(watch, dict):
        return None
    watch_status = str(watch.get("status") or "PENDING").upper()
    if watch_status not in ["PENDING", BLOCKED_BREAKOUT_STATUS]:
        return None
    current_timestamp = utc_timestamp(current_close_time)
    expires_at = utc_timestamp(watch.get("expires_at"))
    if (
        current_timestamp is not None
        and expires_at is not None
        and current_timestamp > expires_at
    ):
        record_indicator_setup_status(
            watch.get("indicator_event_id"), "EXPIRED", "remembered breakout expired"
        )
        clear_breakout_watch(symbol, side, "remembered breakout expired")
        return None
    try:
        invalidation_level = float(watch.get("invalidation_level"))
        close_value = float(current_close)
    except (TypeError, ValueError):
        invalidation_level = None
        close_value = None
    invalidated = bool(
        invalidation_level is not None
        and close_value is not None
        and (
            (side == "BUY" and close_value <= invalidation_level)
            or (side == "SELL" and close_value >= invalidation_level)
        )
    )
    if invalidated:
        record_indicator_setup_status(
            watch.get("indicator_event_id"), "INVALIDATED", "remembered breakout structure invalidated"
        )
        clear_breakout_watch(symbol, side, "remembered breakout structure invalidated")
        return None
    try:
        return {
            "side": side,
            "level": float(watch.get("swing_level")),
            "break_time": watch.get("break_candle_time"),
            "break_close_time": (
                watch.get("break_close_time")
                or candle_close_time(watch.get("break_candle_time"), 15)
            ),
            "break_close": float(watch.get("break_close")),
            "bos_buffer": float(watch.get("bos_buffer") or 0.0),
            "swing": watch.get("swing"),
            "break_type": watch.get("break_type"),
            "invalidation_level": invalidation_level,
            "expires_at": watch.get("expires_at"),
            "remembered": True,
            "watch_status": watch_status,
            "watch": watch,
            "indicator_event_id": watch.get("indicator_event_id"),
            "indicator_event_identity": watch.get("indicator_event_identity"),
        }
    except (TypeError, ValueError):
        return None


def evaluate_15m_breakout(data_15m, symbol, execution_settings=None):
    result = {
        "side": "WAIT",
        "level": None,
        "break_time": None,
        "break_close_time": None,
        "break_close": None,
        "remembered": False,
        "reason": "WAIT_NO_15M_BREAK",
        "swings": [],
        "structure": {},
        "breakouts": [],
        "bos_buffer": None,
    }

    if data_15m is None or len(data_15m) < 10:
        result["reason"] = "WAIT_NOT_ENOUGH_15M_DATA"
        return result

    swing_source = data_15m.iloc[:-1].copy()
    raw_swings = detect_raw_swings(swing_source, symbol)
    swings = [swing for swing in raw_swings if swing.get("valid")]
    result["raw_swings"] = raw_swings
    result["swings"] = swings

    if not swings:
        result["reason"] = (
            "WAIT_SWING_UNDER_100_POINTS"
            if raw_swings
            else "WAIT_NO_VALID_SWING"
        )
        return result

    # HH/HL and LH/LL remain useful display context, but are not an entry gate.
    # A valid confirmed swing plus a buffered closed-candle break is sufficient
    # for either a continuation BOS or reversal CHOCH.
    structure = detect_swing_structure(swings)
    result["structure"] = structure

    last = data_15m.iloc[-1]
    previous = data_15m.iloc[-2]
    last_close = float(last["Close"])
    last_high = float(last["High"])
    last_low = float(last["Low"])
    previous_close = float(previous["Close"])
    break_time = candle_time(data_15m.index[-1])
    break_close_time = candle_close_time(data_15m.index[-1], 15)
    configured = execution_settings or get_cached_execution_settings()
    required_buffer = bos_buffer(
        data_15m,
        symbol,
        configured.get("bos_buffer_points", BOS_MIN_BUFFER_POINTS),
    )
    result["bos_buffer"] = required_buffer

    high_swing = latest_swing(swings, "HIGH")
    low_swing = latest_swing(swings, "LOW")
    candidates = []

    if high_swing:
        level = float(high_swing["price"])
        confirmed = (
            last_high > level
            and last_close >= level + required_buffer
            and previous_close <= level
        )
        if confirmed:
            candidates.append({
                "side": "BUY",
                "level": level,
                "break_time": break_time,
                "break_close_time": break_close_time,
                "break_close": last_close,
                "swing": high_swing,
                "structure": structure,
                "break_type": (
                    "BOS" if structure.get("bias") == "BULLISH" else "CHOCH"
                ),
                "invalidation_level": (
                    float(low_swing["price"]) if low_swing else None
                ),
                "remembered": False,
                "bos_buffer": required_buffer,
            })

    if low_swing:
        level = float(low_swing["price"])
        confirmed = (
            last_low < level
            and last_close <= level - required_buffer
            and previous_close >= level
        )
        if confirmed:
            candidates.append({
                "side": "SELL",
                "level": level,
                "break_time": break_time,
                "break_close_time": break_close_time,
                "break_close": last_close,
                "swing": low_swing,
                "structure": structure,
                "break_type": (
                    "BOS" if structure.get("bias") == "BEARISH" else "CHOCH"
                ),
                "invalidation_level": (
                    float(high_swing["price"]) if high_swing else None
                ),
                "remembered": False,
                "bos_buffer": required_buffer,
            })

    if not candidates:
        for side in ["BUY", "SELL"]:
            remembered = remembered_breakout(
                symbol,
                side,
                current_close_time=break_close_time,
                current_close=last_close,
            )
            if not remembered:
                continue
            candidates.append({
                **remembered,
                "structure": structure,
                "bos_buffer": float(remembered.get("bos_buffer") or required_buffer),
            })

    result["breakouts"] = candidates
    if not candidates:
        raw_buy_break = bool(
            high_swing
            and last_high > float(high_swing["price"])
            and last_close > float(high_swing["price"])
            and previous_close <= float(high_swing["price"])
        )
        raw_sell_break = bool(
            low_swing
            and last_low < float(low_swing["price"])
            and last_close < float(low_swing["price"])
            and previous_close >= float(low_swing["price"])
        )
        if raw_buy_break or raw_sell_break:
            result["reason"] = "WAIT_WEAK_15M_BOS"
        return result

    chosen = candidates[-1]
    clear_opposite_watch(symbol, chosen["side"], "opposite_15m_breakout")
    result.update({
        "side": chosen["side"],
        "level": chosen["level"],
        "break_time": chosen["break_time"],
        "break_close_time": (
            chosen.get("break_close_time")
            or candle_close_time(chosen.get("break_time"), 15)
        ),
        "break_close": chosen["break_close"],
        "remembered": bool(chosen.get("remembered")),
        "bos_buffer": float(chosen.get("bos_buffer") or required_buffer),
        "swing": chosen.get("swing"),
        "break_type": chosen.get("break_type") or "CHOCH",
        "invalidation_level": chosen.get("invalidation_level"),
        "expires_at": chosen.get("expires_at"),
        "watch_status": chosen.get("watch_status"),
        "structure": chosen.get("structure") or structure,
        "reason": f"15M_{chosen.get('break_type') or 'CHOCH'}_SWING_BREAK_CLOSED",
    })
    return result


def confirm_5m(
    data_5m,
    side,
    level,
    break_time,
    break_close_time=None,
    not_before=None,
    required_buffer=0.0,
    indicator_event_id=None,
):
    base = {
        "side": "WAIT",
        "close_confirmed": False,
        "closed_candle_time": None,
        "confirmation_close_time": None,
        "reason": "WAIT_5M_CONFIRMATION",
        "source_indicator_event_id": indicator_event_id,
    }
    if side not in ["BUY", "SELL"] or level is None or not break_time:
        return base

    closed = closed_frame(data_5m, 5)
    if closed is None or closed.empty:
        return base

    try:
        anchor = pd.Timestamp(
            break_close_time
            or candle_close_time(break_time, 15)
        )
        if anchor.tzinfo is None:
            anchor = anchor.tz_localize("UTC")
        else:
            anchor = anchor.tz_convert("UTC")
    except Exception:
        return base

    weak_confirmation_seen = False
    threshold_buffer = max(0.0, float(required_buffer or 0.0))
    for candle_index, candle in closed.iterrows():
        try:
            ts = pd.Timestamp(candle_index)
            if ts.tzinfo is None:
                ts = ts.tz_localize("UTC")
            else:
                ts = ts.tz_convert("UTC")
            confirmation_close = ts + pd.Timedelta(minutes=5)
            if confirmation_close <= anchor:
                continue
            freshness_floor = utc_timestamp(not_before)
            if freshness_floor is not None and confirmation_close <= freshness_floor:
                continue
            open_price = float(candle["Open"])
            close_price = float(candle["Close"])
            high_price = float(candle["High"])
            low_price = float(candle["Low"])
        except Exception:
            continue

        direction_matches = (
            side == "BUY"
            and close_price > open_price
        ) or (
            side == "SELL"
            and close_price < open_price
        )
        passed = direction_matches and (
            (
                side == "BUY"
                and close_price >= float(level) + threshold_buffer
            )
            or (
                side == "SELL"
                and close_price <= float(level) - threshold_buffer
            )
        )
        if passed:
            confirmation_identity = {
                "source_indicator_event_id": indicator_event_id,
                "timeframe": "5m",
                "direction": side,
                "candle_timestamp": ts.isoformat(),
                "confirmation_close_time": confirmation_close.isoformat(),
                "close": close_price,
            }
            confirmation_id = "m5_" + hashlib.sha256(
                json.dumps(
                    confirmation_identity,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            return {
                "side": side,
                "close_confirmed": True,
                "closed_candle_time": ts.isoformat(),
                "confirmation_close_time": confirmation_close.isoformat(),
                "open": open_price,
                "high": high_price,
                "low": low_price,
                "close": close_price,
                "setup_level": float(level),
                "bos_buffer": threshold_buffer,
                "reason": "5M_CLOSE_CONFIRMED",
                "confirmation_id": confirmation_id,
                "confirmation_identity": confirmation_identity,
                "source_indicator_event_id": indicator_event_id,
            }
        if direction_matches and (
            (side == "BUY" and close_price > float(level))
            or (side == "SELL" and close_price < float(level))
        ):
            weak_confirmation_seen = True

    if weak_confirmation_seen:
        base["reason"] = "WAIT_WEAK_5M_CONFIRMATION"
        base["bos_buffer"] = threshold_buffer
    return base


def select_stop_loss(
    swings,
    side,
    entry,
    symbol,
    minimum_sl_distance_points=None,
):
    required = "LOW" if side == "BUY" else "HIGH"
    buffer = sl_buffer(symbol)
    minimum = minimum_sl_distance(symbol, minimum_sl_distance_points)
    candidates = older_swings(swings, required)

    for swing in reversed(candidates):
        swing_price = float(swing["price"])
        stop = swing_price - buffer if side == "BUY" else swing_price + buffer
        distance = abs(float(entry) - stop)
        side_ok = stop < entry if side == "BUY" else stop > entry
        if side_ok and distance >= minimum:
            return {
                "ok": True,
                "stop_loss": stop,
                "swing": swing,
                "distance": distance,
                "distance_points": distance / point_size(symbol),
                "buffer": buffer,
            }

    return {
        "ok": False,
        "reason": (
            "WAIT_SL_TOO_SMALL" if candidates else "WAIT_NO_VALID_SWING_SL"
        ),
        "minimum_distance": minimum,
        "buffer": buffer,
    }


def select_structural_stop_loss(
    event_invalidation_swing,
    side,
    entry,
    symbol,
    setup_break_time=None,
    minimum_sl_distance_points=None,
):
    required = "LOW" if side == "BUY" else "HIGH"
    swing = event_invalidation_swing
    if not isinstance(swing, dict) or swing.get("price") is None:
        return {"ok": False, "reason": "WAIT_NO_STRUCTURAL_SL_SWING"}
    if str(swing.get("type") or "").upper() != required:
        return {"ok": False, "reason": "WAIT_STRUCTURAL_SL_SWING_TYPE_MISMATCH"}

    swing_price = swing.get("price")
    swing_time = swing.get("swing_time")
    confirmation_time = swing.get("confirmation_time")

    setup_ts = utc_timestamp(setup_break_time)
    swing_ts = utc_timestamp(swing_time)
    confirmation_ts = utc_timestamp(confirmation_time)
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

    swing_price = float(swing_price)
    entry = float(entry)
    buffer = sl_buffer(symbol)
    stop = swing_price - buffer if side == "BUY" else swing_price + buffer
    distance = abs(entry - stop)
    minimum = minimum_sl_distance(symbol, minimum_sl_distance_points)
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
        "distance_points": distance / point_size(symbol),
        "buffer": buffer,
        "sl_structure_source": "event_owned_15m_smc_swing",
    }


def select_tp2(
    swings,
    side,
    entry,
    risk,
    symbol,
    minimum_rr=None,
    maximum_rr=None,
):
    if minimum_rr is None or maximum_rr is None:
        configured_minimum, configured_maximum = get_configured_rr_window()
        minimum_rr = configured_minimum if minimum_rr is None else float(minimum_rr)
        maximum_rr = configured_maximum if maximum_rr is None else float(maximum_rr)
    minimum_rr = float(minimum_rr)
    maximum_rr = float(maximum_rr)
    inverse = "HIGH" if side == "BUY" else "LOW"
    candidates = [
        swing
        for swing in older_swings(swings, inverse)
        if (
            float(swing["price"]) > entry
            if side == "BUY"
            else float(swing["price"]) < entry
        )
    ]
    candidates.sort(
        key=lambda swing: abs(float(swing["price"]) - float(entry))
    )
    rejected = []

    for swing in candidates:
        price = float(swing["price"])
        reward = price - entry if side == "BUY" else entry - price
        if reward <= 0:
            continue
        rr = reward / risk
        if minimum_rr <= rr <= maximum_rr:
            return {
                "tp2": price,
                "rr": rr,
                "source": "inverse_15m_swing",
                "swing": swing,
                "rejected_tp_candidates": rejected,
            }
        rejected.append({
            "price": price,
            "rr": rr,
            "reason": (
                f"TP swing reward below {minimum_rr:.2f}R"
                if rr < minimum_rr
                else f"TP swing reward above {maximum_rr:.2f}R"
            ),
            "swing": swing,
        })

    fallback_rr = min(
        max(DEFAULT_FALLBACK_RR, minimum_rr),
        maximum_rr,
    )
    tp2 = entry + (risk * fallback_rr) if side == "BUY" else entry - (risk * fallback_rr)
    return {
        "tp2": tp2,
        "rr": fallback_rr,
        "source": f"fallback_{fallback_rr:g}r",
        "swing": None,
        "rejected_tp_candidates": rejected,
    }


def build_risk_levels(
    data_15m,
    side,
    entry,
    symbol,
    setup_break_time=None,
    execution_settings=None,
    event_invalidation_swing=None,
):
    dec = decimals(symbol)
    swing_source = data_15m.copy()
    setup_timestamp = utc_timestamp(setup_break_time)
    if setup_timestamp is not None:
        try:
            source_index = pd.DatetimeIndex(swing_source.index)
            if source_index.tz is None:
                source_index = source_index.tz_localize("UTC")
            else:
                source_index = source_index.tz_convert("UTC")
            swing_source = swing_source.loc[source_index <= setup_timestamp]
        except Exception:
            return {"ok": False, "reason": "WAIT_NO_VALID_SWING_SL"}
    else:
        swing_source = swing_source.iloc[:-1].copy()
    swings = detect_valid_swings(swing_source, symbol)
    configured = execution_settings or get_cached_execution_settings()
    configured_minimum_sl_points = configured.get(
        "minimum_sl_distance_points",
        MIN_SL_POINTS,
    )
    stop = select_structural_stop_loss(
        event_invalidation_swing,
        side,
        float(entry),
        symbol,
        setup_break_time=setup_break_time,
        minimum_sl_distance_points=configured_minimum_sl_points,
    )
    if not stop.get("ok"):
        return {**stop, "ok": False}

    risk = float(stop["distance"])
    tp2 = select_tp2(
        swings,
        side,
        float(entry),
        risk,
        symbol,
        minimum_rr=configured.get("minimum_rr"),
        maximum_rr=configured.get("maximum_rr"),
    )
    tp2_price = float(tp2["tp2"])
    tp1_ratio = shared.get_tp1_ratio_of_tp2()
    if side == "BUY":
        tp1 = float(entry) + ((tp2_price - float(entry)) * tp1_ratio)
        protected = float(entry) + ((tp2_price - float(entry)) * PROTECTED_SL_TP2_FRACTION)
    else:
        tp1 = float(entry) - ((float(entry) - tp2_price) * tp1_ratio)
        protected = float(entry) - ((float(entry) - tp2_price) * PROTECTED_SL_TP2_FRACTION)

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
        "sl_buffer": round(stop["buffer"], dec),
        "sl_buffer_points": SL_BUFFER_POINTS,
        "minimum_sl_points": int(configured_minimum_sl_points),
        "sl_distance_points": round(stop["distance_points"], 2),
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


def bias_scores_from_context(trend=None, breakout=None, confirmation=None):
    buy_pct = 50
    sell_pct = 50
    confidence = 20

    trend_side = str((trend or {}).get("trend") or "NEUTRAL").upper()
    if trend_side == "BULLISH":
        buy_pct, sell_pct, confidence = 60, 40, 35
    elif trend_side == "BEARISH":
        buy_pct, sell_pct, confidence = 40, 60, 35

    breakout_side = str((breakout or {}).get("side") or "WAIT").upper()
    if breakout_side == "BUY":
        buy_pct, sell_pct, confidence = 70, 30, 55
    elif breakout_side == "SELL":
        buy_pct, sell_pct, confidence = 30, 70, 55

    confirmation_side = str((confirmation or {}).get("side") or "WAIT").upper()
    if confirmation_side == "BUY":
        buy_pct, sell_pct, confidence = 78, 22, 65
    elif confirmation_side == "SELL":
        buy_pct, sell_pct, confidence = 22, 78, 65

    return {
        "buy_pct": buy_pct,
        "sell_pct": sell_pct,
        "confidence": confidence,
        "bias_source": "closed_15m_structure",
        "bias_note": "Market bias only; entry still requires strict setup checks.",
    }


def strategy_stage_states(**overrides):
    states = {
        "market_data": STAGE_PASSED,
        "swing_detection": STAGE_NOT_EVALUATED,
        "fifteen_m_bos": STAGE_NOT_EVALUATED,
        "fifteen_m_close": STAGE_NOT_EVALUATED,
        "ema": STAGE_NOT_EVALUATED,
        "five_m_confirmation": STAGE_NOT_EVALUATED,
        "consolidation_gate": STAGE_NOT_EVALUATED,
        "swing_sl": STAGE_NOT_EVALUATED,
        "tp_rr": STAGE_NOT_EVALUATED,
        "execution": STAGE_BLOCKED,
    }
    states.update(overrides)
    return states


def strategy_cycle_diagnostics(
    symbol,
    closed_15m,
    breakout=None,
    trend=None,
    confirmation=None,
    consolidation=None,
    stage_states=None,
    execution_decision="WAIT",
    block_reason=None,
):
    breakout = breakout if isinstance(breakout, dict) else {}
    trend = trend if isinstance(trend, dict) else {}
    confirmation = confirmation if isinstance(confirmation, dict) else {}
    consolidation = consolidation if isinstance(consolidation, dict) else {}
    swings = breakout.get("swings") if isinstance(breakout.get("swings"), list) else []
    raw_swings = breakout.get("raw_swings") if isinstance(breakout.get("raw_swings"), list) else []
    selected_swing = breakout.get("swing") if isinstance(breakout.get("swing"), dict) else None
    evaluated_candle = None
    if closed_15m is not None and not closed_15m.empty:
        evaluated_candle = candle_time(closed_15m.index[-1])
    return {
        "symbol": shared.normalize_symbol(symbol),
        "evaluation_time": datetime.now(timezone.utc).isoformat(),
        "evaluated_m15_candle": evaluated_candle,
        "evaluated_m15_close_time": candle_close_time(evaluated_candle, 15),
        "raw_swing_count": len(raw_swings),
        "qualified_swing_count": len(swings),
        "qualified_swing": {
            key: selected_swing.get(key)
            for key in ["type", "time", "price", "swing_size"]
        } if selected_swing else None,
        "bos_found": breakout.get("side") in ["BUY", "SELL"],
        "bos_side": breakout.get("side"),
        "bos_level": breakout.get("level"),
        "bos_close": breakout.get("break_close"),
        "bos_close_time": breakout.get("break_close_time"),
        "bos_blocked": bool(
            consolidation.get("is_consolidation")
            and breakout.get("side") in ["BUY", "SELL"]
        ),
        "ema_trend": trend.get("trend"),
        "ema_buy_allowed": bool(trend.get("buy_allowed")),
        "ema_sell_allowed": bool(trend.get("sell_allowed")),
        "five_m_confirmation": bool(confirmation.get("close_confirmed")),
        "five_m_confirmation_time": confirmation.get("confirmation_close_time"),
        "consolidation": bool(consolidation.get("is_consolidation")),
        "consolidation_conditions_met": consolidation.get("conditions_met"),
        "stage_states": dict(stage_states or {}),
        "execution_decision": execution_decision,
        "block_reason": block_reason,
    }


def wait_result(symbol, reason, extra=None):
    normalized_symbol = shared.normalize_symbol(symbol)
    payload = {
        "symbol": normalized_symbol,
        "signal": "WAIT",
        "final_signal": "WAIT",
        "signal_before_filters": "WAIT",
        "signal_after_filters": "WAIT",
        "signal_text": "WAIT",
        "buy_pct": 0,
        "sell_pct": 0,
        "confidence": 0,
        "strategy_model": "strict_15m_trader",
        "strategy_setup_timeframe": "15m",
        "strategy_confirmation_timeframe": "5m",
        "strategy_trend_timeframe": "15m EMA",
        "setup_timeframe_used": "15m",
        "final_signal_source": "strict_trader",
        "fifteen_m_setup": "WAIT",
        "fifteen_m_uses_closed_candle_only": True,
        "fifteen_m_forming_candle_removed": False,
        "confirmation_5m": "WAIT",
        "confirmation_5m_raw": "WAIT",
        "current_5m_entry_confirmation": False,
        "strategy_setup_complete": False,
        "blocked_by": reason,
        "blocked_reason": reason,
        "block_reason": reason,
        "blocker_rule_name": reason,
        "debug_reasons": [reason],
    }
    if extra:
        payload.update(extra)

    breakout_for_identity = payload.get("fifteen_m_swing_break")
    if isinstance(breakout_for_identity, dict):
        event_id = breakout_for_identity.get("indicator_event_id")
        event_identity = breakout_for_identity.get("indicator_event_identity")
        if event_id:
            payload["source_indicator_event_id"] = event_id
            payload["indicator_event_identity"] = event_identity
            upper_reason = str(reason or "").upper()
            if "EXPIRED" in upper_reason:
                payload["setup_status"] = "EXPIRED"
            elif "INVALID" in upper_reason or "CHANGED" in upper_reason:
                payload["setup_status"] = "INVALIDATED"
            elif "5M_CONFIRMATION" in upper_reason:
                payload["setup_status"] = "WAITING"
            else:
                payload["setup_status"] = "BLOCKED"
            payload["setup_blocking_reason"] = reason
            record_indicator_setup_status(
                event_id,
                payload["setup_status"],
                reason,
                confirmation=(
                    payload.get("confirmation_5m")
                    if isinstance(payload.get("confirmation_5m"), dict)
                    else None
                ),
            )

    stages = payload.get("strategy_stage_states")
    if not isinstance(stages, dict):
        stages = strategy_stage_states()
    payload["strategy_stage_states"] = stages

    breakout = payload.get("fifteen_m_swing_break")
    confirmation = payload.get("confirmation_5m")
    trend = payload.get("trend_15m") or {}
    structure = breakout.get("structure") if isinstance(breakout, dict) else {}
    if not isinstance(structure, dict):
        structure = {}
    setup_side = str((breakout or {}).get("side") or payload.get("fifteen_m_setup") or "WAIT").upper()
    if setup_side not in ["BUY", "SELL"]:
        setup_side = "WAIT"

    diagnostics = {
        "symbol": normalized_symbol,
        "final_signal": "WAIT",
        "signal_before_filters": payload.get("signal_before_filters") or setup_side,
        "signal_after_filters": "WAIT",
        "blocked": True,
        "blocked_by": payload.get("blocked_by"),
        "blocked_reason": payload.get("blocked_reason"),
        "block_reason": payload.get("blocked_reason"),
        "fifteen_m_setup": setup_side,
        "fifteen_m_swing_break": bool(
            isinstance(breakout, dict) and breakout.get("side") in ["BUY", "SELL"]
        ),
        "fifteen_m_swing_break_confirmed": bool(
            isinstance(breakout, dict) and breakout.get("side") in ["BUY", "SELL"]
        ),
        "fifteen_m_close_confirmed": bool(
            isinstance(breakout, dict) and breakout.get("side") in ["BUY", "SELL"]
        ),
        "fifteen_m_structure_pattern": structure.get("pattern"),
        "fifteen_m_structure_bias": structure.get("bias"),
        "hh_hl_confirmed": structure.get("pattern") == "HH_HL",
        "lh_ll_confirmed": structure.get("pattern") == "LH_LL",
        "fifteen_m_break_level": (
            breakout.get("level")
            if isinstance(breakout, dict)
            else None
        ),
        "fifteen_m_break_time": (
            breakout.get("break_time")
            if isinstance(breakout, dict)
            else None
        ),
        "five_m_confirmation": bool(
            isinstance(confirmation, dict) and confirmation.get("close_confirmed")
        ),
        "five_m_signal": (
            confirmation.get("side")
            if isinstance(confirmation, dict)
            else "WAIT"
        ),
        "trend_bias": str(trend.get("trend") or "NEUTRAL").lower(),
        "entry": payload.get("entry_price"),
        "sl": payload.get("stop_loss"),
        "tp1": payload.get("tp1"),
        "tp2": payload.get("tp2"),
        "stage_states": stages,
        "swing_detection_state": stages.get("swing_detection"),
        "fifteen_m_bos_state": stages.get("fifteen_m_bos"),
        "fifteen_m_close_state": stages.get("fifteen_m_close"),
        "ema_state": stages.get("ema"),
        "five_m_confirmation_state": stages.get("five_m_confirmation"),
        "consolidation_state": stages.get("consolidation_gate"),
        "swing_sl_state": stages.get("swing_sl"),
        "tp_rr_state": stages.get("tp_rr"),
        "execution_state": stages.get("execution"),
        "strategy_cycle": payload.get("strategy_cycle"),
    }
    payload["signal_diagnostics"] = diagnostics
    payload["entry_strategy_debug"] = diagnostics.copy()
    payload["strategy_debug"] = diagnostics.copy()
    if isinstance(payload.get("strategy_cycle"), dict):
        print("STRICT_STRATEGY_CYCLE =", payload["strategy_cycle"])
    return payload


def get_mtf_signal(data_5m, data_15m, data_1h, symbol):
    normalized_symbol = shared.normalize_symbol(symbol)
    closed_15m = closed_frame(data_15m, 15)
    closed_5m = closed_frame(data_5m, 5)
    removed_forming_15m = (
        data_15m is not None
        and closed_15m is not None
        and len(data_15m) > len(closed_15m)
    )
    base_meta = {
        "fifteen_m_uses_closed_candle_only": True,
        "fifteen_m_forming_candle_removed": removed_forming_15m,
        "market_condition": "STRUCTURE",
    }

    if data_5m is not None and not data_5m.empty:
        try:
            base_meta["price"] = float(data_5m["Close"].iloc[-1])
        except (KeyError, TypeError, ValueError):
            pass

    if closed_15m is None or len(closed_15m) < 25:
        stages = strategy_stage_states(
            market_data=STAGE_FAILED,
            execution=STAGE_BLOCKED,
        )
        return wait_result(
            normalized_symbol,
            "WAIT_NOT_ENOUGH_15M_DATA",
            {
                **base_meta,
                "strategy_stage_states": stages,
                "strategy_cycle": strategy_cycle_diagnostics(
                    normalized_symbol,
                    closed_15m,
                    stage_states=stages,
                    block_reason="WAIT_NOT_ENOUGH_15M_DATA",
                ),
            },
        )

    # One atomic cached settings snapshot is shared by BOS qualification,
    # SL validation, and RR selection for this complete evaluation.
    execution_settings = get_cached_execution_settings()
    trend = trend_filter(closed_15m, normalized_symbol)
    consolidation = classify_consolidation(closed_15m, normalized_symbol)
    consolidation_filter_enabled = bool(
        execution_settings.get("consolidation_filter_enabled", True)
    )
    consolidation_blocked = bool(
        consolidation_filter_enabled and consolidation.get("is_consolidation")
    )
    consolidation = {
        **consolidation,
        "filter_enabled": consolidation_filter_enabled,
        "blocking": consolidation_blocked,
    }
    base_meta["consolidation"] = consolidation
    base_meta["market_condition"] = (
        "CONSOLIDATION" if consolidation.get("is_consolidation") else "STRUCTURE"
    )
    breakout = evaluate_15m_breakout(
        closed_15m,
        normalized_symbol,
        execution_settings=execution_settings,
    )
    base_meta = {
        **base_meta,
        **bias_scores_from_context(trend=trend, breakout=breakout),
    }

    if breakout["side"] not in ["BUY", "SELL"]:
        stages = strategy_stage_states(
            swing_detection=(
                STAGE_PASSED if breakout.get("swings") else STAGE_FAILED
            ),
            fifteen_m_bos=STAGE_FAILED,
            consolidation_gate=(
                STAGE_BLOCKED
                if consolidation_blocked
                else STAGE_PASSED
            ),
            execution=STAGE_BLOCKED,
        )
        return wait_result(normalized_symbol, breakout["reason"], {
            **base_meta,
            "trend_15m": trend,
            "fifteen_m_swing_break": breakout,
            "strategy_stage_states": stages,
            "strategy_cycle": strategy_cycle_diagnostics(
                normalized_symbol,
                closed_15m,
                breakout=breakout,
                trend=trend,
                consolidation=consolidation,
                stage_states=stages,
                block_reason=breakout["reason"],
            ),
        })

    side = breakout["side"]
    watch_status = str(breakout.get("watch_status") or "").upper()
    if (
        breakout.get("remembered")
        and watch_status == BLOCKED_BREAKOUT_STATUS
        and not consolidation_blocked
    ):
        clear_breakout_watch(
            normalized_symbol,
            side,
            "consolidation ended; fresh BOS required",
        )
        stages = strategy_stage_states(
            swing_detection=STAGE_PASSED,
            fifteen_m_bos=STAGE_FAILED,
            consolidation_gate=STAGE_PASSED,
            execution=STAGE_BLOCKED,
        )
        return wait_result(
            normalized_symbol,
            "WAIT_FRESH_15M_BOS_AFTER_CONSOLIDATION",
            {
                **base_meta,
                "trend_15m": trend,
                "expired_15m_setup": breakout.get("watch"),
                "strategy_stage_states": stages,
                "strategy_cycle": strategy_cycle_diagnostics(
                    normalized_symbol,
                    closed_15m,
                    breakout=breakout,
                    trend=trend,
                    consolidation=consolidation,
                    stage_states=stages,
                    block_reason="WAIT_FRESH_15M_BOS_AFTER_CONSOLIDATION",
                ),
            },
        )

    ema_allowed = (
        side == "BUY" and bool(trend.get("buy_allowed"))
    ) or (
        side == "SELL" and bool(trend.get("sell_allowed"))
    )
    if not ema_allowed:
        clear_breakout_watch(
            normalized_symbol,
            side,
            "EMA no longer permits remembered direction",
        )
        stages = strategy_stage_states(
            swing_detection=STAGE_PASSED,
            fifteen_m_bos=STAGE_PASSED,
            fifteen_m_close=STAGE_PASSED,
            ema=STAGE_FAILED,
            consolidation_gate=(
                STAGE_BLOCKED
                if consolidation_blocked
                else STAGE_PASSED
            ),
            execution=STAGE_BLOCKED,
        )
        return wait_result(normalized_symbol, "WAIT_EMA_NOT_ALLOWED", {
            **base_meta,
            "trend_15m": trend,
            "fifteen_m_swing_break": breakout,
            "blocked_by": "WAIT_EMA_NOT_ALLOWED",
            "blocker_rule_name": "strict_15m_ema_permission",
            "strategy_stage_states": stages,
            "strategy_cycle": strategy_cycle_diagnostics(
                normalized_symbol,
                closed_15m,
                breakout=breakout,
                trend=trend,
                consolidation=consolidation,
                stage_states=stages,
                block_reason="WAIT_EMA_NOT_ALLOWED",
            ),
        })

    prior_close = last_position_closed_time(normalized_symbol)
    bos_close = utc_timestamp(
        breakout.get("break_close_time")
        or candle_close_time(breakout.get("break_time"), 15)
    )
    if prior_close is not None and (bos_close is None or bos_close <= prior_close):
        shared.FIFTEEN_M_SWING_WATCH.pop(
            get_watch_key(normalized_symbol, side),
            None,
        )
        shared.save_fifteen_m_swing_watch()
        stages = strategy_stage_states(
            swing_detection=STAGE_PASSED,
            fifteen_m_bos=STAGE_PASSED,
            fifteen_m_close=STAGE_PASSED,
            ema=STAGE_PASSED,
            consolidation_gate=(
                STAGE_BLOCKED
                if consolidation_blocked
                else STAGE_PASSED
            ),
            execution=STAGE_BLOCKED,
        )
        return wait_result(normalized_symbol, "WAIT_SETUP_BEFORE_PREVIOUS_CLOSE", {
            **base_meta,
            "trend_15m": trend,
            "fifteen_m_swing_break": breakout,
            "previous_position_closed_at": prior_close.isoformat(),
            "blocked_by": "post_close_setup_freshness",
            "blocker_rule_name": "bos_after_previous_position_close",
            "strategy_stage_states": stages,
            "strategy_cycle": strategy_cycle_diagnostics(
                normalized_symbol,
                closed_15m,
                breakout=breakout,
                trend=trend,
                consolidation=consolidation,
                stage_states=stages,
                block_reason="WAIT_SETUP_BEFORE_PREVIOUS_CLOSE",
            ),
        })

    breakout_meta = {
        **base_meta,
        "fifteen_m_setup": side,
        "trend_15m": trend,
        "fifteen_m_swing_break": breakout,
    }

    five_m = confirm_5m(
        closed_5m,
        side,
        breakout["level"],
        breakout["break_time"],
        break_close_time=breakout.get("break_close_time"),
        not_before=prior_close,
        required_buffer=breakout.get("bos_buffer"),
        indicator_event_id=breakout.get("indicator_event_id"),
    )
    setup_meta = {
        **breakout_meta,
        **bias_scores_from_context(
            trend=trend,
            breakout=breakout,
            confirmation=five_m,
        ),
        "confirmation_5m": five_m,
    }
    if not five_m.get("close_confirmed"):
        if not breakout.get("remembered"):
            save_remembered_breakout(
                normalized_symbol,
                side,
                breakout["level"],
                breakout["break_time"],
                breakout["break_close"],
                five_m.get("reason") or "WAIT_5M_CONFIRMATION",
                break_close_time=breakout.get("break_close_time"),
                required_buffer=breakout.get("bos_buffer"),
                swing=breakout.get("swing"),
                break_type=breakout.get("break_type"),
                invalidation_level=breakout.get("invalidation_level"),
                status=(
                    BLOCKED_BREAKOUT_STATUS
                    if consolidation_blocked
                    else "PENDING"
                ),
                indicator_event_id=breakout.get("indicator_event_id"),
                indicator_event_identity=breakout.get("indicator_event_identity"),
            )
        stages = strategy_stage_states(
            swing_detection=STAGE_PASSED,
            fifteen_m_bos=(
                STAGE_BLOCKED
                if consolidation_blocked
                else STAGE_PASSED
            ),
            fifteen_m_close=STAGE_PASSED,
            ema=STAGE_PASSED,
            five_m_confirmation=STAGE_FAILED,
            consolidation_gate=(
                STAGE_BLOCKED
                if consolidation_blocked
                else STAGE_PASSED
            ),
            execution=STAGE_BLOCKED,
        )
        return wait_result(normalized_symbol, five_m.get("reason") or "WAIT_5M_CONFIRMATION", {
            **setup_meta,
            "remembered_breakout": True,
            "remembered_breakout_status": (
                "WAIT_REMEMBERED_BREAKOUT"
                if breakout.get("remembered")
                else "WAIT_5M_CONFIRMATION"
            ),
            "strategy_stage_states": stages,
            "strategy_cycle": strategy_cycle_diagnostics(
                normalized_symbol,
                closed_15m,
                breakout=breakout,
                trend=trend,
                confirmation=five_m,
                consolidation=consolidation,
                stage_states=stages,
                block_reason=five_m.get("reason") or "WAIT_5M_CONFIRMATION",
            ),
        })

    if consolidation_blocked:
        save_remembered_breakout(
            normalized_symbol,
            side,
            breakout["level"],
            breakout["break_time"],
            breakout["break_close"],
            BLOCKED_BREAKOUT_STATUS,
            break_close_time=breakout.get("break_close_time"),
            required_buffer=breakout.get("bos_buffer"),
            swing=breakout.get("swing"),
            break_type=breakout.get("break_type"),
            invalidation_level=breakout.get("invalidation_level"),
            status=BLOCKED_BREAKOUT_STATUS,
        )
        stages = strategy_stage_states(
            swing_detection=STAGE_PASSED,
            fifteen_m_bos=STAGE_BLOCKED,
            fifteen_m_close=STAGE_PASSED,
            ema=STAGE_PASSED,
            five_m_confirmation=STAGE_PASSED,
            consolidation_gate=STAGE_BLOCKED,
            execution=STAGE_BLOCKED,
        )
        return wait_result(normalized_symbol, "WAIT_CONSOLIDATION", {
            **setup_meta,
            "blocked_breakout_status": BLOCKED_BREAKOUT_STATUS,
            "strategy_stage_states": stages,
            "strategy_cycle": strategy_cycle_diagnostics(
                normalized_symbol,
                closed_15m,
                breakout=breakout,
                trend=trend,
                confirmation=five_m,
                consolidation=consolidation,
                stage_states=stages,
                block_reason="WAIT_CONSOLIDATION",
            ),
        })

    shared.FIFTEEN_M_SWING_WATCH.pop(get_watch_key(normalized_symbol, side), None)
    shared.save_fifteen_m_swing_watch()

    entry = five_m.get("close") or breakout["break_close"]
    levels = build_risk_levels(
        closed_15m,
        side,
        entry,
        normalized_symbol,
        setup_break_time=breakout.get("break_time"),
        execution_settings=execution_settings,
        event_invalidation_swing=breakout.get("event_invalidation_swing"),
    )
    if not levels.get("ok"):
        stages = strategy_stage_states(
            swing_detection=STAGE_PASSED,
            fifteen_m_bos=STAGE_PASSED,
            fifteen_m_close=STAGE_PASSED,
            ema=STAGE_PASSED,
            five_m_confirmation=STAGE_PASSED,
            consolidation_gate=STAGE_PASSED,
            swing_sl=STAGE_FAILED,
            execution=STAGE_BLOCKED,
        )
        return wait_result(normalized_symbol, levels.get("reason") or "WAIT_INVALID_RISK_LEVELS", {
            **setup_meta,
            "swing_sl_debug": levels,
            "strategy_stage_states": stages,
            "strategy_cycle": strategy_cycle_diagnostics(
                normalized_symbol,
                closed_15m,
                breakout=breakout,
                trend=trend,
                confirmation=five_m,
                consolidation=consolidation,
                stage_states=stages,
                block_reason=levels.get("reason") or "WAIT_INVALID_RISK_LEVELS",
            ),
        })

    buy_pct = 85 if side == "BUY" else 15
    sell_pct = 85 if side == "SELL" else 15
    completed_stages = strategy_stage_states(
        swing_detection=STAGE_PASSED,
        fifteen_m_bos=STAGE_PASSED,
        fifteen_m_close=STAGE_PASSED,
        ema=STAGE_PASSED,
        five_m_confirmation=STAGE_PASSED,
        consolidation_gate=STAGE_PASSED,
        swing_sl=STAGE_PASSED,
        tp_rr=STAGE_PASSED,
        execution=STAGE_PASSED,
    )
    completed_cycle = strategy_cycle_diagnostics(
        normalized_symbol,
        closed_15m,
        breakout=breakout,
        trend=trend,
        confirmation=five_m,
        consolidation=consolidation,
        stage_states=completed_stages,
        execution_decision=side,
    )
    result = {
        "symbol": normalized_symbol,
        "signal": side,
        "final_signal": side,
        "signal_before_filters": side,
        "signal_after_filters": side,
        "signal_text": f"{side} (15m swing + 5m close)",
        "buy_pct": buy_pct,
        "sell_pct": sell_pct,
        "confidence": 85,
        "market_condition": "STRUCTURE",
        "consolidation": consolidation,
        "entry_quality": "STRICT",
        "entry_timing": "5M CLOSED CONFIRMATION",
        "strategy_model": "strict_15m_trader",
        "strategy_setup_timeframe": "15m",
        "strategy_confirmation_timeframe": "5m",
        "strategy_trend_timeframe": "15m EMA",
        "setup_timeframe_used": "15m",
        "final_signal_source": "strict_trader",
        "fifteen_m_setup": side,
        "fifteen_m_uses_closed_candle_only": True,
        "fifteen_m_forming_candle_removed": removed_forming_15m,
        "strategy_setup_complete": True,
        "strategy_setup_type": f"{side}_15M_SWING_BREAK_5M_CONFIRMED",
        "plan_type": f"STRICT {side}",
        "plan_reason": (
            f"{side}: valid 15m swing {breakout.get('break_type') or 'CHOCH'}, "
            "closed buffered break, and later 5m close confirmed"
        ),
        "entry_price": levels["entry"],
        "price": levels["entry"],
        "stop_loss": levels["stop_loss"],
        "tp1": levels["tp1"],
        "tp2": levels["tp2"],
        "protected_sl_price": levels["protected_sl_price"],
        "risk_reward": levels["risk_reward"],
        "risk_reward_ratio": levels["risk_reward_ratio"],
        "trend_15m": trend,
        "fifteen_m_swing_break": breakout,
        "source_indicator_event_id": breakout.get("indicator_event_id"),
        "indicator_event_identity": breakout.get("indicator_event_identity"),
        "m5_confirmation_id": five_m.get("confirmation_id"),
        "m5_confirmation_identity": five_m.get("confirmation_identity"),
        "setup_status": "ELIGIBLE",
        "fifteen_m_swing_break_confirmed": True,
        "fifteen_m_swing_level": breakout["level"],
        "fifteen_m_break_classification": breakout.get("break_type"),
        "fifteen_m_structure_pattern": (breakout.get("structure") or {}).get("pattern"),
        "fifteen_m_structure_bias": (breakout.get("structure") or {}).get("bias"),
        "fifteen_m_closed_candle_close": breakout["break_close"],
        "fifteen_m_break_time": breakout["break_time"],
        "fifteen_m_break_close_time": breakout.get("break_close_time"),
        "setup_candle_time": five_m.get("confirmation_close_time"),
        "five_m_closed_candle_time": five_m.get("confirmation_close_time"),
        "previous_position_closed_at": (
            prior_close.isoformat() if prior_close is not None else None
        ),
        "confirmation_5m": five_m,
        "confirmation_5m_raw": five_m.get("side"),
        "current_5m_entry_confirmation": True,
        "swing_sl_debug": levels,
        "tp1_rule": levels["tp1_rule"],
        "tp2_rule": levels["tp_structure_source"],
        "protected_sl_rule": levels["protected_sl_rule"],
        "blocked_by": None,
        "blocked_reason": None,
        "block_reason": None,
        "blocker_rule_name": None,
        "strategy_stage_states": completed_stages,
        "strategy_cycle": completed_cycle,
        "signal_diagnostics": {
            "symbol": normalized_symbol,
            "final_signal": side,
            "signal_before_filters": side,
            "signal_after_filters": side,
            "blocked": False,
            "blocked_by": None,
            "blocked_reason": None,
            "block_reason": None,
            "fifteen_m_setup": side,
            "fifteen_m_swing_break": True,
            "fifteen_m_swing_break_confirmed": True,
            "fifteen_m_close_confirmed": True,
            "fifteen_m_structure_pattern": (breakout.get("structure") or {}).get("pattern"),
            "fifteen_m_structure_bias": (breakout.get("structure") or {}).get("bias"),
            "hh_hl_confirmed": (breakout.get("structure") or {}).get("pattern") == "HH_HL",
            "lh_ll_confirmed": (breakout.get("structure") or {}).get("pattern") == "LH_LL",
            "fifteen_m_break_level": breakout["level"],
            "fifteen_m_break_time": breakout["break_time"],
            "fifteen_m_break_close_time": breakout.get("break_close_time"),
            "five_m_confirmation": True,
            "five_m_confirmation_close_time": five_m.get("confirmation_close_time"),
            "five_m_signal": side,
            "trend_bias": str(trend.get("trend") or "NEUTRAL").lower(),
            "entry": levels["entry"],
            "sl": levels["stop_loss"],
            "tp1": levels["tp1"],
            "tp2": levels["tp2"],
            "stage_states": completed_stages,
            "swing_detection_state": completed_stages["swing_detection"],
            "fifteen_m_bos_state": completed_stages["fifteen_m_bos"],
            "fifteen_m_close_state": completed_stages["fifteen_m_close"],
            "ema_state": completed_stages["ema"],
            "five_m_confirmation_state": completed_stages["five_m_confirmation"],
            "consolidation_state": completed_stages["consolidation_gate"],
            "swing_sl_state": completed_stages["swing_sl"],
            "tp_rr_state": completed_stages["tp_rr"],
            "execution_state": completed_stages["execution"],
            "strategy_cycle": completed_cycle,
        },
        "debug_reasons": [
            "15m EMA supports trade",
            "15m candle closed beyond valid 100-point swing",
            "5m candle closed in same direction",
            "SL/TP built from strict swing rules",
        ],
    }
    result["setup_identity"] = {
        "symbol": normalized_symbol,
        "direction": side,
        "swing_type": (breakout.get("swing") or {}).get("type"),
        "swing_timestamp": (breakout.get("swing") or {}).get("time"),
        "swing_price": (breakout.get("swing") or {}).get("price"),
        "bos_candle_timestamp": breakout.get("break_time"),
        "bos_level": breakout.get("level"),
        "confirmation_timestamp": five_m.get("confirmation_close_time"),
        "indicator_event_id": breakout.get("indicator_event_id"),
        "m5_confirmation_id": five_m.get("confirmation_id"),
    }
    record_indicator_setup_status(
        breakout.get("indicator_event_id"),
        "ELIGIBLE",
        confirmation=five_m,
    )
    diagnostics = dict(result["signal_diagnostics"])
    result["entry_strategy_debug"] = diagnostics.copy()
    result["strategy_debug"] = diagnostics.copy()
    result["bos_detected"] = breakout.get("break_type") == "BOS"
    result["choch_detected"] = breakout.get("break_type") == "CHOCH"
    result["smc_direction"] = side
    result["fifteen_m_close_confirmed"] = True
    result["five_m_confirmation"] = True
    print("STRICT_STRATEGY_CYCLE =", completed_cycle)
    return result


def update_trade_with_wick_management(trade, candle):
    if not isinstance(trade, dict) or not isinstance(candle, dict):
        return trade

    side = str(trade.get("side") or trade.get("action") or "").upper()
    if side not in ["BUY", "SELL"]:
        return trade

    updated = dict(trade)
    high = float(candle.get("high", candle.get("High")))
    low = float(candle.get("low", candle.get("Low")))
    tp1 = float(updated["tp1"])
    tp2 = float(updated["tp2"])
    sl = float(updated.get("sl", updated.get("stop_loss")))
    protected = float(
        updated.get("protected_sl_price")
        or build_protected_sl(updated["entry"], tp2, side)
    )

    def touched(price):
        return high >= price if side == "BUY" else low <= price

    def touched_sl(price):
        return low <= price if side == "BUY" else high >= price

    if touched_sl(sl) and not updated.get("hit_tp1"):
        updated.update({"status": "CLOSED", "result": "LOSS", "closed_price": sl})
        return updated

    if touched(tp2):
        updated.update({"status": "CLOSED", "result": "WIN", "closed_price": tp2})
        return updated

    if touched(tp1) and not updated.get("hit_tp1"):
        updated["hit_tp1"] = True
        updated["profit_protected"] = True
        updated["protected_sl_price"] = protected
        updated["sl"] = protected
        updated["result"] = "TP1 HIT"

    if updated.get("hit_tp1") and touched_sl(protected):
        updated.update({
            "status": "CLOSED",
            "result": "PROTECTED WIN",
            "closed_price": protected,
        })

    return updated


def build_protected_sl(entry, tp2, side):
    entry = float(entry)
    tp2 = float(tp2)
    if side == "BUY":
        return entry + ((tp2 - entry) * PROTECTED_SL_TP2_FRACTION)
    return entry - ((entry - tp2) * PROTECTED_SL_TP2_FRACTION)
