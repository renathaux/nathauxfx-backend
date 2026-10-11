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
    """Compatibility helper retained for tests/tools; not the BOS engine."""
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


def _highest_index(highs: list[float], end: int, lookback: int = LOOKBACK) -> int:
    start = max(0, end - lookback + 1)
    return max(range(start, end + 1), key=lambda idx: (highs[idx], idx))


def _lowest_index(lows: list[float], end: int, lookback: int = LOOKBACK) -> int:
    start = max(0, end - lookback + 1)
    return min(range(start, end + 1), key=lambda idx: (lows[idx], -idx))


def _structure_highest_index(highs: list[float], end: int, lookback: int = LOOKBACK) -> int:
    start = max(0, end - lookback + 1)
    fallback = _highest_index(highs, end, lookback)
    chosen = None
    for idx in range(end - 1, max(start, 1) - 1, -1):
        if idx + 1 > end:
            continue
        if highs[idx] > highs[idx - 1] and highs[idx + 1] <= highs[idx] and idx >= fallback:
            chosen = idx
    return fallback if chosen is None else chosen


def _structure_lowest_index(lows: list[float], end: int, lookback: int = LOOKBACK) -> int:
    start = max(0, end - lookback + 1)
    fallback = _lowest_index(lows, end, lookback)
    chosen = None
    for idx in range(end - 1, max(start, 1) - 1, -1):
        if idx + 1 > end:
            continue
        if lows[idx] < lows[idx - 1] and lows[idx + 1] >= lows[idx] and idx >= fallback:
            chosen = idx
    return fallback if chosen is None else chosen


def _fib_levels(direction, structure_high, structure_low, high_start, low_start, timestamps):
    structure_range = abs(structure_high - structure_low)
    if structure_range <= 0:
        return []
    output = []
    for value in FIB_LEVELS:
        if direction == 1:
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


def _legacy_event_invalidation_swing(swing_type, price, swing_index, break_index, timestamps):
    return {
        "type": swing_type,
        "price": float(price),
        "swing_time": timestamps[swing_index].isoformat(),
        "swing_index": int(swing_index),
        # The legacy engine establishes its opposite structural extremum when
        # the closed break candle is processed; it does not use right-bar pivot
        # confirmation. That acceptance close is therefore its confirmation.
        "confirmation_time": timestamps[break_index].isoformat(),
        "confirmation_index": int(break_index),
        "source": "LEGACY_CURRENT_STRUCTURE",
    }


def analyze_structure(
    frame: pd.DataFrame,
    *,
    left_bars: int = 2,
    right_bars: int = 2,
    timeframe: str | None = None,
    point_size: float | None = None,
) -> dict:
    """Port of the TradingView structure state machine, using closed candles.

    left_bars/right_bars are accepted for API compatibility but are not used by
    this structure engine; the source algorithm uses a 10-bar structure lookback.
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

    opens = data["Open"].astype(float).tolist()
    highs = data["High"].astype(float).tolist()
    lows = data["Low"].astype(float).tolist()
    closes = data["Close"].astype(float).tolist()
    timestamps = list(data.index)

    structure_high = highs[0]
    structure_low = lows[0]
    structure_high_start = 0
    structure_low_start = 0
    # Source mapping: 1=bearish structure, 2=bullish structure, 0=unset.
    structure_direction = 0
    distance_gate = StructureDistanceGate(timeframe=timeframe, point_size=point_size)
    events = []

    for index in range(1, len(data)):
        close = closes[index]
        prev1 = closes[index - 1] if index >= 1 else None
        prev2 = closes[index - 2] if index >= 2 else None
        prev3 = closes[index - 3] if index >= 3 else None

        enough_low = (
            index >= 3
            and index - 1 > structure_low_start
            and index - 2 > structure_low_start
            and index - 3 > structure_low_start
        )
        enough_high = (
            index >= 3
            and index - 1 > structure_high_start
            and index - 2 > structure_high_start
            and index - 3 > structure_high_start
        )

        low_broken = (
            index >= 3
            and close < structure_low
            and prev1 >= structure_low and prev2 >= structure_low and prev3 >= structure_low
            and enough_low
        ) or (structure_direction == 2 and close < structure_low)

        high_broken = (
            index >= 3
            and close > structure_high
            and prev1 <= structure_high and prev2 <= structure_high and prev3 <= structure_high
            and enough_high
        ) or (structure_direction == 1 and close > structure_high)

        if low_broken:
            if distance_gate.accept(structure_low):
                event_type = "BOS" if structure_direction == 1 else "CHOCH"
                event_high_start = _structure_highest_index(highs, index, LOOKBACK)
                events.append({
                    "event_type": event_type,
                    "direction": "BEARISH",
                    "timestamp": timestamps[index].isoformat(),
                    "close": float(close),
                    "broken_swing_timestamp": timestamps[structure_low_start].isoformat(),
                    "broken_level": float(structure_low),
                    "structure_start_index": int(structure_low_start),
                    "break_index": int(index),
                    "previous_direction": int(structure_direction),
                    "new_direction": 1,
                    "previous_structure_event_level": distance_gate.previous_level,
                    "structure_distance_points": distance_gate.last_distance_points,
                    "event_invalidation_swing": _legacy_event_invalidation_swing(
                        "HIGH",
                        highs[event_high_start],
                        event_high_start,
                        index,
                        timestamps,
                    ),
                })
                structure_direction = 1
            structure_high_start = _structure_highest_index(highs, index, LOOKBACK)
            structure_low_start = index
            structure_high = highs[structure_high_start]
            structure_low = lows[index]

        elif high_broken:
            if distance_gate.accept(structure_high):
                event_type = "BOS" if structure_direction == 2 else "CHOCH"
                event_low_start = _structure_lowest_index(lows, index, LOOKBACK)
                events.append({
                    "event_type": event_type,
                    "direction": "BULLISH",
                    "timestamp": timestamps[index].isoformat(),
                    "close": float(close),
                    "broken_swing_timestamp": timestamps[structure_high_start].isoformat(),
                    "broken_level": float(structure_high),
                    "structure_start_index": int(structure_high_start),
                    "break_index": int(index),
                    "previous_direction": int(structure_direction),
                    "new_direction": 2,
                    "previous_structure_event_level": distance_gate.previous_level,
                    "structure_distance_points": distance_gate.last_distance_points,
                    "event_invalidation_swing": _legacy_event_invalidation_swing(
                        "LOW",
                        lows[event_low_start],
                        event_low_start,
                        index,
                        timestamps,
                    ),
                })
                structure_direction = 2
            structure_high_start = index
            structure_low_start = _structure_lowest_index(lows, index, LOOKBACK)
            structure_high = highs[index]
            structure_low = lows[structure_low_start]

        else:
            if highs[index] > structure_high and structure_direction in (0, 2):
                can_update = not (
                    index >= 3
                    and index - 1 > structure_high_start
                    and index - 2 > structure_high_start
                    and index - 3 > structure_high_start
                )
                if can_update:
                    structure_high = highs[index]
                    structure_high_start = index
            elif lows[index] < structure_low and structure_direction in (0, 1):
                can_update = not (
                    index >= 3
                    and index - 1 > structure_low_start
                    and index - 2 > structure_low_start
                    and index - 3 > structure_low_start
                )
                if can_update:
                    structure_low = lows[index]
                    structure_low_start = index

    bias: StructureBias = (
        "BULLISH" if structure_direction == 2
        else "BEARISH" if structure_direction == 1
        else "NEUTRAL"
    )
    current_structure = {
        "direction": int(structure_direction),
        "bias": bias,
        "high": float(structure_high),
        "low": float(structure_low),
        "high_start_index": int(structure_high_start),
        "low_start_index": int(structure_low_start),
        "high_start_timestamp": timestamps[structure_high_start].isoformat(),
        "low_start_timestamp": timestamps[structure_low_start].isoformat(),
        "end_timestamp": timestamps[-1].isoformat(),
        "range": float(abs(structure_high - structure_low)),
    }

    return {
        "bias": bias,
        "events": events,
        "current_structure": current_structure,
        "fib_levels": _fib_levels(
            structure_direction,
            structure_high,
            structure_low,
            structure_high_start,
            structure_low_start,
            timestamps,
        ),
        "swings": [],
        "config": {
            "lookback": LOOKBACK,
            "break_with_candle_body": True,
            "current_structure": True,
            "fib_values": list(FIB_LEVELS),
            "fvg": False,
            "closed_candles_only": True,
            "repainting": False,
            "source_algorithm": "LudoGH68_SMC_Structures",
            **distance_gate.config(),
        },
    }
