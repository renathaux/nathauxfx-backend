from fundamentals.config import POLICY_EVENT_HORIZON_DAYS, POLICY_STALE_AFTER_DAYS
from fundamentals.factors.common import normalized_delta, weighted_factor_result
from fundamentals.factors.surprises import parse_numeric
from fundamentals.normalization.indicators import indicator_metadata


def calculate_policy_factor(observations, currency, now=None):
    """Score only structured policy-rate evidence; speeches are intentionally excluded."""
    candidates = []
    for event in observations or []:
        if str(event.get("currency") or "").upper() != str(currency).upper():
            continue
        metadata = indicator_metadata(event.get("indicator") or event.get("event_name"))
        if metadata["category"] != "policy":
            continue
        actual = parse_numeric(event.get("actual"))
        if actual is None:
            continue
        surprise = normalized_delta(actual, event.get("forecast"), floor=0.25)
        rate_change = normalized_delta(actual, event.get("revised_previous") or event.get("previous"), floor=0.25)
        components = []
        if surprise is not None:
            components.append((surprise, 0.40))
        if rate_change is not None:
            components.append((rate_change, 0.60))
        if not components:
            continue
        denominator = sum(weight for _value, weight in components)
        score = sum(value * weight for value, weight in components) / denominator
        candidates.append({
            "event": event,
            "score": score,
            "reason": "Higher-than-expected or rising structured policy rates are hawkish; lower rates are dovish.",
        })
    return weighted_factor_result(
        "policy_score",
        candidates,
        now=now,
        stale_after_days=POLICY_STALE_AFTER_DAYS,
        horizon_days=POLICY_EVENT_HORIZON_DAYS,
    )
