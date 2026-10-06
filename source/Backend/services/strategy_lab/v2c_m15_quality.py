"""Strategy Lab V2C: Baseline V1 plus stronger M15 breakout-candle quality."""
from . import baseline_v1

MIN_BODY_RATIO = 0.60
MAX_CLOSE_SIDE_WICK_RATIO = 0.30

candidates = baseline_v1.candidates
resolve_trade = baseline_v1.resolve_trade


def _m15_quality(candle, side):
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


def evaluate_event(event, timestamp, prefix15, frame5, side, leg, settings, end, *, previous_close=None):
    # First require the exact Baseline V1 setup. V2C changes only whether an
    # otherwise-valid Baseline setup is accepted based on the M15 break candle.
    trade, rejection, trace = baseline_v1.evaluate_event(
        event,
        timestamp,
        prefix15,
        frame5,
        side,
        leg,
        settings,
        end,
        previous_close=previous_close,
    )
    trace["m15_quality_rule"] = {
        "minimum_body_ratio": MIN_BODY_RATIO,
        "maximum_close_side_wick_ratio": MAX_CLOSE_SIDE_WICK_RATIO,
    }
    if trade is None:
        return trade, rejection, trace

    candle = prefix15.iloc[-1]
    quality = _m15_quality(candle, side)
    trace["m15_break_body_ratio"] = quality["body_ratio"]
    trace["m15_break_close_side_wick_ratio"] = quality["close_side_wick_ratio"]
    passed = (
        quality["direction_ok"]
        and quality["body_ratio"] >= MIN_BODY_RATIO
        and quality["close_side_wick_ratio"] <= MAX_CLOSE_SIDE_WICK_RATIO
    )
    if not passed:
        trace["final_action"] = "REJECT_M15_BREAK_CANDLE_QUALITY"
        return None, "rejected_by_m15_quality", trace

    trade["m15_break_body_ratio"] = quality["body_ratio"]
    trade["m15_break_close_side_wick_ratio"] = quality["close_side_wick_ratio"]
    trade["filters_passed"] = list(trade.get("filters_passed") or [])
    trade["filters_passed"].append("m15_break_candle_quality")
    return trade, None, trace
