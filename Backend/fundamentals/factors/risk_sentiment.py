from fundamentals.factors.common import normalized_delta, weighted_factor_result
from fundamentals.normalization.indicators import indicator_metadata


def calculate_risk_sentiment_factor(observations, currency="USD", now=None):
    """Use only structured financial-stress evidence; no free-form sentiment."""
    candidates = []
    for event in observations or []:
        if str(event.get("currency") or "").upper() != str(currency).upper():
            continue
        metadata = indicator_metadata(event.get("indicator") or event.get("event_name"))
        if metadata["base_indicator"] != "financial_stress_index":
            continue
        score = normalized_delta(
            event.get("actual"),
            event.get("revised_previous") or event.get("previous"),
            floor=0.10,
        )
        if score is None:
            continue
        candidates.append({
            "event": event,
            "score": score,
            "reason": "Rising structured financial stress supports safe-haven demand for gold.",
        })
    return weighted_factor_result(
        "risk_sentiment", candidates, now=now, horizon_days=30, stale_after_days=14
    )
