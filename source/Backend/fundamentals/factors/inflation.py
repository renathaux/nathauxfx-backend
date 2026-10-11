from fundamentals.config import ORDINARY_EVENT_HORIZON_DAYS, ORDINARY_STALE_AFTER_DAYS
from fundamentals.factors.common import normalized_delta, weighted_factor_result
from fundamentals.factors.surprises import parse_numeric
from fundamentals.normalization.indicators import indicator_metadata


def calculate_inflation_factor(observations, currency, now=None, policy_context=None):
    """Deterministic inflation model combining surprise, trend, and policy context.

    A positive surprise alone is not permanently bullish: 45% comes from the
    surprise, 40% from current-vs-prior trend, and 15% from policy context.
    Dovish policy also halves a positive inflation conclusion.
    """
    policy_score = (policy_context or {}).get("score")
    candidates = []
    for event in observations or []:
        if str(event.get("currency") or "").upper() != str(currency).upper():
            continue
        metadata = indicator_metadata(event.get("indicator") or event.get("event_name"))
        if metadata["category"] != "inflation":
            continue
        actual = parse_numeric(event.get("actual"))
        if actual is None:
            continue
        surprise = normalized_delta(actual, event.get("forecast"), floor=0.10)
        trend = normalized_delta(actual, event.get("revised_previous") or event.get("previous"), floor=0.10)
        components = []
        if surprise is not None:
            components.append((surprise, 0.45))
        if trend is not None:
            components.append((trend, 0.40))
        if policy_score is not None:
            components.append((float(policy_score), 0.15))
        if not components:
            continue
        denominator = sum(weight for _value, weight in components)
        score = sum(value * weight for value, weight in components) / denominator
        if score > 0 and policy_score is not None and float(policy_score) < -20:
            score *= 0.50
        candidates.append({
            "event": event,
            "score": score,
            "reason": "Inflation combines actual-versus-forecast, current trend, and structured policy context.",
        })
    return weighted_factor_result(
        "inflation_score",
        candidates,
        now=now,
        stale_after_days=ORDINARY_STALE_AFTER_DAYS,
        horizon_days=ORDINARY_EVENT_HORIZON_DAYS,
    )
