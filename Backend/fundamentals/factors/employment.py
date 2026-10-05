from fundamentals.config import ORDINARY_EVENT_HORIZON_DAYS, ORDINARY_STALE_AFTER_DAYS
from fundamentals.factors.common import normalized_delta, weighted_factor_result
from fundamentals.factors.surprises import DENOMINATOR_FLOOR, parse_numeric
from fundamentals.normalization.indicators import indicator_metadata


def calculate_employment_factor(observations, currency, now=None):
    candidates = []
    for event in observations or []:
        if str(event.get("currency") or "").upper() != str(currency).upper():
            continue
        metadata = indicator_metadata(event.get("indicator") or event.get("event_name"))
        if metadata["category"] != "employment":
            continue
        actual = parse_numeric(event.get("actual"))
        if actual is None:
            continue
        floor = DENOMINATOR_FLOOR.get(metadata["indicator"], 0.10)
        surprise = normalized_delta(actual, event.get("forecast"), floor=floor)
        trend = normalized_delta(actual, event.get("revised_previous") or event.get("previous"), floor=floor)
        if metadata["higher_is_currency_bullish"] is False:
            surprise = -surprise if surprise is not None else None
            trend = -trend if trend is not None else None
        components = []
        if surprise is not None:
            components.append((surprise, 0.60))
        if trend is not None:
            components.append((trend, 0.40))
        if not components:
            continue
        denominator = sum(weight for _value, weight in components)
        score = sum(value * weight for value, weight in components) / denominator
        candidates.append({
            "event": event,
            "score": score,
            "reason": "Employment combines directional surprise, recent trend, revisions, and recency.",
        })
    return weighted_factor_result(
        "employment_score",
        candidates,
        now=now,
        stale_after_days=ORDINARY_STALE_AFTER_DAYS,
        horizon_days=ORDINARY_EVENT_HORIZON_DAYS,
    )
