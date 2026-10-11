from fundamentals.config import ORDINARY_EVENT_HORIZON_DAYS, ORDINARY_STALE_AFTER_DAYS
from fundamentals.factors.common import clamp, normalized_delta, weighted_factor_result
from fundamentals.factors.surprises import parse_numeric
from fundamentals.normalization.indicators import indicator_metadata


def calculate_growth_factor(observations, currency, now=None):
    candidates = []
    for event in observations or []:
        if str(event.get("currency") or "").upper() != str(currency).upper():
            continue
        metadata = indicator_metadata(event.get("indicator") or event.get("event_name"))
        if metadata["category"] != "growth":
            continue
        actual = parse_numeric(event.get("actual"))
        if actual is None:
            continue
        surprise = normalized_delta(actual, event.get("forecast"), floor=0.10)
        trend = normalized_delta(actual, event.get("revised_previous") or event.get("previous"), floor=0.10)
        components = []
        if surprise is not None:
            components.append((surprise, 0.50 if "pmi" not in metadata["indicator"] else 0.45))
        if trend is not None:
            components.append((trend, 0.50 if "pmi" not in metadata["indicator"] else 0.30))
        if "pmi" in metadata["indicator"]:
            components.append((clamp((actual - 50.0) / 5.0 * 100.0), 0.25))
        if not components:
            continue
        denominator = sum(weight for _value, weight in components)
        score = sum(value * weight for value, weight in components) / denominator
        candidates.append({
            "event": event,
            "score": score,
            "reason": "Growth combines surprise, trend, revisions and, for PMI, expansion versus the 50 level.",
        })
    return weighted_factor_result(
        "growth_score",
        candidates,
        now=now,
        stale_after_days=ORDINARY_STALE_AFTER_DAYS,
        horizon_days=ORDINARY_EVENT_HORIZON_DAYS,
    )
