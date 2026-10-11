"""Reusable, analysis-only M5 confirmation quality variant helper."""
from __future__ import annotations

import pandas as pd

from . import baseline_v1
from . import production_math as calc


def candle_quality(candle, side):
    try:
        open_price = float(candle["Open"])
        high_price = float(candle["High"])
        low_price = float(candle["Low"])
        close_price = float(candle["Close"])
    except Exception:
        return {"direction_ok": False, "body_ratio": 0.0, "close_side_wick_ratio": 1.0}

    candle_range = high_price - low_price
    if candle_range <= 0:
        return {"direction_ok": False, "body_ratio": 0.0, "close_side_wick_ratio": 1.0}

    direction_ok = (
        (side == "BUY" and close_price > open_price)
        or (side == "SELL" and close_price < open_price)
    )
    upper_wick = max(0.0, high_price - max(open_price, close_price))
    lower_wick = max(0.0, min(open_price, close_price) - low_price)
    close_side_wick = upper_wick if side == "BUY" else lower_wick
    return {
        "direction_ok": direction_ok,
        "body_ratio": abs(close_price - open_price) / candle_range,
        "close_side_wick_ratio": close_side_wick / candle_range,
    }


def _filtered_quality_frame(frame5, side, minimum_body_ratio, maximum_close_side_wick_ratio):
    keep = []
    for _, candle in frame5.iterrows():
        quality = candle_quality(candle, side)
        keep.append(
            bool(
                quality["direction_ok"]
                and quality["body_ratio"] >= minimum_body_ratio
                and quality["close_side_wick_ratio"] <= maximum_close_side_wick_ratio
            )
        )
    return frame5.loc[keep].copy()


def evaluate_event(
    event,
    timestamp,
    prefix15,
    frame5,
    side,
    leg,
    settings,
    end,
    *,
    previous_close=None,
    minimum_body_ratio,
    maximum_close_side_wick_ratio,
):
    """Evaluate Baseline V1 with only the M5 confirmation quality rule changed."""
    buffer = calc.bos_buffer(prefix15, settings["bos_buffer_points"])

    # Determine whether Baseline V1 had a valid directional/beyond-level M5 close.
    baseline_confirmation = baseline_v1._confirmation(
        event, frame5, buffer, end, previous_close
    )

    quality_frame = _filtered_quality_frame(
        frame5,
        side,
        minimum_body_ratio,
        maximum_close_side_wick_ratio,
    )
    trade, rejection, trace = baseline_v1.evaluate_event(
        event,
        timestamp,
        prefix15,
        quality_frame,
        side,
        leg,
        settings,
        end,
        previous_close=previous_close,
    )

    trace["m5_quality_rule"] = {
        "minimum_body_ratio": minimum_body_ratio,
        "maximum_close_side_wick_ratio": maximum_close_side_wick_ratio,
    }

    if trade is None:
        # Preserve Baseline's own earlier gate failures. Convert only the case where
        # Baseline had an M5 confirmation but no quality-qualified confirmation.
        if (
            rejection == "rejected_by_m5_confirmation_expired"
            and baseline_confirmation is not None
        ):
            rejected_time, rejected_close_time, _ = baseline_confirmation
            rejected_candle = frame5.loc[rejected_time]
            quality = candle_quality(rejected_candle, side)
            trace["m5_confirmation_time"] = baseline_v1._iso(rejected_time)
            trace["m5_confirmation_body_ratio"] = quality["body_ratio"]
            trace["m5_confirmation_close_side_wick_ratio"] = quality[
                "close_side_wick_ratio"
            ]
            trace["final_action"] = "REJECT_M5_CONFIRMATION_QUALITY"
            return None, "rejected_by_m5_quality", trace
        return None, rejection, trace

    confirmation_time = pd.Timestamp(trade["m5_confirmation_timestamp"])
    candle = frame5.loc[confirmation_time]
    quality = candle_quality(candle, side)
    trade["m5_confirmation_body_ratio"] = quality["body_ratio"]
    trade["m5_confirmation_close_side_wick_ratio"] = quality[
        "close_side_wick_ratio"
    ]
    trace["m5_confirmation_body_ratio"] = quality["body_ratio"]
    trace["m5_confirmation_close_side_wick_ratio"] = quality[
        "close_side_wick_ratio"
    ]
    trade["filters_passed"] = list(trade.get("filters_passed") or [])
    if "m5_confirmation_quality" not in trade["filters_passed"]:
        trade["filters_passed"].append("m5_confirmation_quality")
    return trade, None, trace
