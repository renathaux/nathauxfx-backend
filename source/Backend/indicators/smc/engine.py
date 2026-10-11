"""FlowSignal SMC structure analysis.

Structure logic adapted from "SMC Structures and FVG" © LudoGH68,
licensed under MPL-2.0. Fair Value Gap (FVG) logic is intentionally excluded.

Safety boundary: pure analysis only. No broker, execution, LIVE/PAPER,
risk, cooldown, or order-management imports.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pandas as pd

from .structure_distance import StructureDistanceGate


StructureBias = Literal["BULLISH", "BEARISH", "NEUTRAL"]
SwingType = Literal["HIGH", "LOW"]
LOOKBACK = 10
FIB_LEVELS = (0.786, 0.705, 0.618, 0.5, 0.382)


@dataclass(frozen=True)
class SwingPoint:
    swing_type: SwingType
    index: int
    confirmed_index: int
    timestamp: str
    confirmed_timestamp: str
    price: float


def _normalize_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or not isinstance(frame, pd.DataFrame) or frame.empty:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close"])
    required = {"Open", "High", "Low", "Close"}
    if not required.issubset(frame.columns):
        missing = ", ".join(sorted(required - set(frame.columns)))
        raise ValueError(f"SMC frame missing required columns: {missing}")
    data = frame.copy().sort_index()
    data = data.dropna(subset=["Open", "High", "Low", "Close"])
    data.index = pd.to_datetime(data.index, utc=True)
    return data


def detect_confirmed_swings(
    frame: pd.DataFrame,
    *,
    left_bars: int = 2,
    right_bars: int = 2,
) -> list[SwingPoint]:
    """Return non-repainting pivots confirmed by bars on both sides."""
    data = _normalize_frame(frame)
    if len(data) < left_bars + right_bars + 1:
        return []
    highs = data["High"].astype(float).tolist()
    lows = data["Low"].astype(float).tolist()
    timestamps = list(data.index)
    swings: list[SwingPoint] = []
    for index in range(left_bars, len(data) - right_bars):
        high = highs[index]
        low = lows[index]
        left_highs = highs[index-left_bars:index]
        right_highs = highs[index+1:index+1+right_bars]
        left_lows = lows[index-left_bars:index]
        right_lows = lows[index+1:index+1+right_bars]
        confirmed_index = index + right_bars
        if high > max(left_highs) and high >= max(right_highs):
            swings.append(SwingPoint(
                "HIGH", index, confirmed_index,
                timestamps[index].isoformat(), timestamps[confirmed_index].isoformat(), float(high)
            ))
        if low < min(left_lows) and low <= min(right_lows):
            swings.append(SwingPoint(
                "LOW", index, confirmed_index,
                timestamps[index].isoformat(), timestamps[confirmed_index].isoformat(), float(low)
            ))
    return sorted(swings, key=lambda item: (item.confirmed_index, item.index, item.swing_type))


def _latest_swing(swings, swing_type, *, before_index=None, after_index=None):
    candidates = [s for s in swings if s.swing_type == swing_type]
    if before_index is not None:
        candidates = [s for s in candidates if s.index < before_index]
    if after_index is not None:
        candidates = [s for s in candidates if s.index > after_index]
    return candidates[-1] if candidates else None


def _latest_expanding_swing(
    swings,
    swing_type,
    *,
    frontier,
    before_index=None,
    after_index=None,
):
    """Return the latest local pivot that genuinely expands a regime frontier."""
    candidates = [s for s in swings if s.swing_type == swing_type]
    if before_index is not None:
        candidates = [s for s in candidates if s.index < before_index]
    if after_index is not None:
        candidates = [s for s in candidates if s.index > after_index]
    if frontier is not None:
        if swing_type == "HIGH":
            candidates = [s for s in candidates if s.price > frontier.price]
        else:
            candidates = [s for s in candidates if s.price < frontier.price]
    return candidates[-1] if candidates else None


def _serialise_swing(swing):
    if swing is None:
        return None
    return {
        "type": swing.swing_type,
        "index": int(swing.index),
        "confirmed_index": int(swing.confirmed_index),
        "timestamp": swing.timestamp,
        "confirmed_timestamp": swing.confirmed_timestamp,
        "price": float(swing.price),
    }


def _serialise_event_invalidation_swing(swing, source):
    if swing is None:
        return None
    return {
        "type": swing.swing_type,
        "price": float(swing.price),
        "swing_time": swing.timestamp,
        "swing_index": int(swing.index),
        "confirmation_time": swing.confirmed_timestamp,
        "confirmation_index": int(swing.confirmed_index),
        "source": source,
    }


def _fib_levels(direction, structure_high, structure_low, high_start, low_start, timestamps):
    structure_range = abs(structure_high - structure_low)
    if structure_range <= 0:
        return []
    output = []
    for value in FIB_LEVELS:
        if direction == 2:
            price = structure_high - (structure_range - structure_range * value)
            start = high_start
        else:
            price = structure_low + (structure_range - structure_range * value)
            start = low_start
        output.append({
            "value": value,
            "price": float(price),
            "start_index": int(start),
            "start_timestamp": timestamps[start].isoformat(),
        })
    return output


def analyze_structure(
    frame: pd.DataFrame,
    *,
    left_bars: int = 2,
    right_bars: int = 2,
    timeframe: str | None = None,
    point_size: float | None = None,
) -> dict:
    """Analyze conservative external market structure from confirmed pivots.

    Local pivots remain available as internal swing context, but they cannot
    flip the market by themselves. Once a bullish regime is established, the
    external low that originated that regime stays protected through ordinary
    higher-low pullbacks and continuation BOS events. A bearish CHoCH requires
    a candle-body close through that external protected low. The bearish case
    is symmetric.

    This deliberately trades fewer reversals in exchange for avoiding rapid
    BUY/SELL flipping on internal Gold pullbacks.
    """
    data = _normalize_frame(frame)
    if data.empty:
        return {
            "bias": "NEUTRAL",
            "events": [],
            "current_structure": None,
            "fib_levels": [],
            "swings": [],
            "config": {"lookback": LOOKBACK, "fvg": False},
        }

    highs = data["High"].astype(float).tolist()
    lows = data["Low"].astype(float).tolist()
    closes = data["Close"].astype(float).tolist()
    timestamps = list(data.index)
    swings = detect_confirmed_swings(
        data,
        left_bars=left_bars,
        right_bars=right_bars,
    )

    confirmed_by_index: dict[int, list[SwingPoint]] = {}
    for swing in swings:
        confirmed_by_index.setdefault(swing.confirmed_index, []).append(swing)

    available: list[SwingPoint] = []
    bias: StructureBias = "NEUTRAL"
    direction_code = 0
    events = []
    external_low: SwingPoint | None = None
    external_high: SwingPoint | None = None
    bullish_continuation_frontier: SwingPoint | None = None
    bearish_continuation_frontier: SwingPoint | None = None
    last_event_index = -1
    distance_gate = StructureDistanceGate(timeframe=timeframe, point_size=point_size)

    for index in range(len(data)):
        available.extend(confirmed_by_index.get(index, []))
        if index == 0:
            continue

        close = closes[index]
        previous_close = closes[index - 1]

        if bias == "BULLISH":
            high_reference = _latest_expanding_swing(
                available,
                "HIGH",
                frontier=bullish_continuation_frontier,
                before_index=index,
                after_index=last_event_index,
            )
            low_reference = external_low
        elif bias == "BEARISH":
            high_reference = external_high
            low_reference = _latest_expanding_swing(
                available,
                "LOW",
                frontier=bearish_continuation_frontier,
                before_index=index,
                after_index=last_event_index,
            )
        else:
            high_reference = _latest_swing(available, "HIGH", before_index=index)
            low_reference = _latest_swing(available, "LOW", before_index=index)

        broke_high = bool(
            high_reference is not None
            and index > high_reference.confirmed_index
            and close > high_reference.price
            and previous_close <= high_reference.price
        )
        broke_low = bool(
            low_reference is not None
            and index > low_reference.confirmed_index
            and close < low_reference.price
            and previous_close >= low_reference.price
        )

        if broke_high:
            if not distance_gate.accept(high_reference.price):
                continue
            previous_bias = bias
            event_type = "CHOCH" if bias == "BEARISH" else "BOS"
            outgoing_bearish_frontier = bearish_continuation_frontier
            current_leg_low = _latest_swing(
                available,
                "LOW",
                before_index=index,
                after_index=last_event_index,
            )
            event_low = current_leg_low or _latest_swing(
                available,
                "LOW",
                before_index=index,
            )
            event_low_source = (
                "CURRENT_LEG" if current_leg_low is not None else "FALLBACK_HISTORY"
            )
            events.append({
                "event_type": event_type,
                "direction": "BULLISH",
                "timestamp": timestamps[index].isoformat(),
                "close": float(close),
                "broken_swing_timestamp": high_reference.timestamp,
                "broken_level": float(high_reference.price),
                "structure_start_index": int(high_reference.index),
                "break_index": int(index),
                "previous_direction": 2 if previous_bias == "BULLISH" else 1 if previous_bias == "BEARISH" else 0,
                "new_direction": 2,
                "importance": "EXTERNAL",
                "previous_structure_event_level": distance_gate.previous_level,
                "structure_distance_points": distance_gate.last_distance_points,
                "event_invalidation_swing": _serialise_event_invalidation_swing(
                    event_low,
                    event_low_source,
                ),
            })

            # On the first bullish break or a true bullish CHoCH, lock the
            # regime's origin low. Continuation BOS must not ratchet this level
            # upward to every small internal higher low.
            if previous_bias != "BULLISH":
                protected_reset_low = (
                    outgoing_bearish_frontier
                    if previous_bias == "BEARISH"
                    else event_low
                )
                if protected_reset_low is not None:
                    external_low = protected_reset_low
                bearish_continuation_frontier = None
            bullish_continuation_frontier = high_reference
            external_high = None
            bias = "BULLISH"
            direction_code = 2
            last_event_index = index
            continue

        if broke_low:
            if not distance_gate.accept(low_reference.price):
                continue
            previous_bias = bias
            event_type = "CHOCH" if bias == "BULLISH" else "BOS"
            outgoing_bullish_frontier = bullish_continuation_frontier
            current_leg_high = _latest_swing(
                available,
                "HIGH",
                before_index=index,
                after_index=last_event_index,
            )
            event_high = current_leg_high or _latest_swing(
                available,
                "HIGH",
                before_index=index,
            )
            event_high_source = (
                "CURRENT_LEG" if current_leg_high is not None else "FALLBACK_HISTORY"
            )
            events.append({
                "event_type": event_type,
                "direction": "BEARISH",
                "timestamp": timestamps[index].isoformat(),
                "close": float(close),
                "broken_swing_timestamp": low_reference.timestamp,
                "broken_level": float(low_reference.price),
                "structure_start_index": int(low_reference.index),
                "break_index": int(index),
                "previous_direction": 2 if previous_bias == "BULLISH" else 1 if previous_bias == "BEARISH" else 0,
                "new_direction": 1,
                "importance": "EXTERNAL",
                "previous_structure_event_level": distance_gate.previous_level,
                "structure_distance_points": distance_gate.last_distance_points,
                "event_invalidation_swing": _serialise_event_invalidation_swing(
                    event_high,
                    event_high_source,
                ),
            })

            if previous_bias != "BEARISH":
                protected_reset_high = (
                    outgoing_bullish_frontier
                    if previous_bias == "BULLISH"
                    else event_high
                )
                if protected_reset_high is not None:
                    external_high = protected_reset_high
                bullish_continuation_frontier = None
            bearish_continuation_frontier = low_reference
            external_low = None
            bias = "BEARISH"
            direction_code = 1
            last_event_index = index

    latest_high = _latest_swing(swings, "HIGH")
    latest_low = _latest_swing(swings, "LOW")
    structure_high_swing = external_high if bias == "BEARISH" else latest_high
    structure_low_swing = external_low if bias == "BULLISH" else latest_low

    structure_high = float(structure_high_swing.price if structure_high_swing else max(highs))
    structure_low = float(structure_low_swing.price if structure_low_swing else min(lows))
    high_start = int(structure_high_swing.index if structure_high_swing else highs.index(max(highs)))
    low_start = int(structure_low_swing.index if structure_low_swing else lows.index(min(lows)))

    current_structure = {
        "direction": int(direction_code),
        "bias": bias,
        "high": structure_high,
        "low": structure_low,
        "high_start_index": high_start,
        "low_start_index": low_start,
        "high_start_timestamp": timestamps[high_start].isoformat(),
        "low_start_timestamp": timestamps[low_start].isoformat(),
        "end_timestamp": timestamps[-1].isoformat(),
        "range": float(abs(structure_high - structure_low)),
        "protected_high": _serialise_swing(external_high),
        "protected_low": _serialise_swing(external_low),
        "bullish_continuation_frontier": _serialise_swing(
            bullish_continuation_frontier
        ),
        "bearish_continuation_frontier": _serialise_swing(
            bearish_continuation_frontier
        ),
    }

    return {
        "bias": bias,
        "events": events,
        "current_structure": current_structure,
        "fib_levels": _fib_levels(
            direction_code,
            structure_high,
            structure_low,
            high_start,
            low_start,
            timestamps,
        ) if direction_code else [],
        "swings": [_serialise_swing(swing) for swing in swings],
        "config": {
            "lookback": LOOKBACK,
            "left_bars": left_bars,
            "right_bars": right_bars,
            "break_with_candle_body": True,
            "protected_external_structure": True,
            "continuation_bos_does_not_move_external_invalidation": True,
            "continuation_frontier_uses_accepted_broken_swing": True,
            "continuation_frontier_is_monotonic_within_regime": True,
            "internal_swings_are_not_reversal_triggers": True,
            "fib_values": list(FIB_LEVELS),
            "fvg": False,
            "closed_candles_only": True,
            "repainting": False,
            "source_algorithm": "FlowSignal_protected_external_SMC_structure",
            **distance_gate.config(),
        },
    }
