from __future__ import annotations

from fundamentals.factors.central_bank import calculate_policy_factor
from fundamentals.factors.employment import calculate_employment_factor
from fundamentals.factors.growth import calculate_growth_factor
from fundamentals.factors.inflation import calculate_inflation_factor
from fundamentals.factors.real_yields import calculate_real_yield_factor
from fundamentals.factors.risk_sentiment import calculate_risk_sentiment_factor
from fundamentals.factors.common import clamp
from fundamentals.gold_config import (
    GOLD_BUY_THRESHOLD,
    GOLD_CONFIDENCE_CAP,
    GOLD_FACTOR_WEIGHTS,
    GOLD_MINIMUM_ACTIVE_COVERAGE,
    GOLD_MISSING_YIELD_CONFIDENCE_CAP,
    GOLD_PROVISIONAL_CONFIDENCE_CAP,
    GOLD_REQUIRED_FACTORS,
    GOLD_SELL_THRESHOLD,
)


def inflation_gold_score(inflation_score, policy_score):
    """Interpret inflation through the policy/yield transmission channel."""
    if inflation_score is None:
        return None
    inflation = float(inflation_score)
    policy = float(policy_score) if policy_score is not None else 0.0
    if inflation > 0:
        return clamp(inflation * 0.35) if policy < -20 else clamp(-inflation)
    support = -inflation
    return clamp(support * 0.50) if policy > 20 else clamp(support)


def _gold_driver(name, source, gold_score):
    active = source.get("status") == "ACTIVE" and source.get("score") is not None
    normalized_score = 0.0 if gold_score is not None and abs(float(gold_score)) < 1e-12 else gold_score
    return {
        "name": name,
        "score": round(float(normalized_score), 2) if active and normalized_score is not None else None,
        "source_score": source.get("score"),
        "weight": GOLD_FACTOR_WEIGHTS[name],
        "status": source.get("status", "INSUFFICIENT_DATA"),
        "confidence": source.get("confidence", 0.0),
        "evidence_count": source.get("evidence_count", 0),
        "provisional_count": source.get("provisional_count", 0),
        "updated_at": source.get("updated_at"),
        "evidence": source.get("evidence") or [],
    }


def _confidence(drivers, score, coverage, required_present):
    active = [item for item in drivers.values() if item["status"] == "ACTIVE" and item["score"] is not None]
    if not active:
        return 0.0, {
            "coverage": 0.0, "freshness": 0.0, "source_quality": 0.0,
            "agreement": 0.0, "magnitude": 0.0, "evidence_depth": 0.0,
            "validated_data": 0.0,
        }
    signs = [1 if item["score"] > 0 else -1 if item["score"] < 0 else 0 for item in active]
    dominant = 1 if sum(signs) > 0 else -1 if sum(signs) < 0 else 0
    agreement = sum(1 for sign in signs if sign == dominant) / len(signs) if dominant else 0.5
    evidence = [e for item in active for e in item["evidence"]]
    qualities = [float(e.get("provider_quality") or 0) for e in evidence]
    source_quality = sum(qualities) / len(qualities) if qualities else 0.0
    freshness = sum(float(item.get("confidence") or 0) / 100 for item in active) / len(active)
    depth = min(1.0, sum(item["evidence_count"] for item in active) / 12.0)
    provisional_count = sum(item["provisional_count"] for item in active)
    validation = 1.0 if provisional_count == 0 else max(0.25, 1.0 - provisional_count / max(1, len(evidence)))
    components = {
        "coverage": coverage,
        "freshness": freshness,
        "source_quality": source_quality,
        "agreement": agreement,
        "magnitude": min(1.0, abs(float(score or 0)) / 60.0),
        "evidence_depth": depth,
        "validated_data": validation,
    }
    confidence = 100 * (
        0.25 * components["coverage"]
        + 0.20 * components["freshness"]
        + 0.15 * components["source_quality"]
        + 0.15 * components["agreement"]
        + 0.10 * components["magnitude"]
        + 0.10 * components["evidence_depth"]
        + 0.05 * components["validated_data"]
    )
    confidence = min(confidence, GOLD_CONFIDENCE_CAP)
    # Sparse coverage must never report stronger confidence than the fraction
    # of the configured model that is actually active.
    confidence = min(confidence, coverage * 100.0)
    if provisional_count:
        confidence = min(confidence, GOLD_PROVISIONAL_CONFIDENCE_CAP)
    if not required_present:
        confidence = min(confidence, GOLD_MISSING_YIELD_CONFIDENCE_CAP)
    return round(confidence, 2), {key: round(value, 4) for key, value in components.items()}


def _top_reasons(drivers):
    candidates = []
    for name, item in drivers.items():
        if item["score"] is None:
            continue
        contribution = item["score"] * item["weight"]
        candidates.append({
            "factor": name,
            "direction": "BULLISH_GOLD" if contribution > 0 else "BEARISH_GOLD" if contribution < 0 else "NEUTRAL",
            "score": item["score"],
            "weighted_contribution": round(contribution, 2),
            "summary": (item["evidence"][0].get("reason") if item["evidence"] else f"{name.replace('_', ' ').title()} evidence"),
            "evidence_event_ids": [e.get("event_id") for e in item["evidence"] if e.get("event_id")][:3],
        })
    return sorted(candidates, key=lambda item: abs(item["weighted_contribution"]), reverse=True)[:3]


def calculate_xauusd_state(observations, now=None):
    policy = calculate_policy_factor(observations, "USD", now=now)
    inflation = calculate_inflation_factor(observations, "USD", now=now, policy_context=policy)
    sources = {
        "policy": policy,
        "real_yields": calculate_real_yield_factor(observations, "USD", now=now),
        "inflation": inflation,
        "employment": calculate_employment_factor(observations, "USD", now=now),
        "growth": calculate_growth_factor(observations, "USD", now=now),
        "risk_sentiment": calculate_risk_sentiment_factor(observations, "USD", now=now),
    }
    drivers = {
        "policy": _gold_driver("policy", policy, -float(policy.get("score") or 0)),
        "real_yields": _gold_driver("real_yields", sources["real_yields"], -float(sources["real_yields"].get("score") or 0)),
        "inflation": _gold_driver("inflation", inflation, inflation_gold_score(inflation.get("score"), policy.get("score"))),
        "employment": _gold_driver("employment", sources["employment"], -float(sources["employment"].get("score") or 0)),
        "growth": _gold_driver("growth", sources["growth"], -float(sources["growth"].get("score") or 0)),
        "risk_sentiment": _gold_driver("risk_sentiment", sources["risk_sentiment"], float(sources["risk_sentiment"].get("score") or 0)),
    }
    active = {name: item for name, item in drivers.items() if item["status"] == "ACTIVE" and item["score"] is not None}
    active_weight = sum(GOLD_FACTOR_WEIGHTS[name] for name in active)
    score = (
        sum(item["score"] * GOLD_FACTOR_WEIGHTS[name] for name, item in active.items()) / active_weight
        if active_weight else None
    )
    score = round(clamp(score), 2) if score is not None else None
    required_present = GOLD_REQUIRED_FACTORS.issubset(active)
    status = "ACTIVE" if active_weight >= GOLD_MINIMUM_ACTIVE_COVERAGE and required_present else "INSUFFICIENT_DATA"
    direction = "NEUTRAL"
    if status == "ACTIVE" and score is not None:
        if score >= GOLD_BUY_THRESHOLD:
            direction = "BUY"
        elif score <= GOLD_SELL_THRESHOLD:
            direction = "SELL"
    confidence, confidence_components = _confidence(drivers, score, active_weight, required_present)
    usd_active = [
        source for name, source in sources.items()
        if name != "risk_sentiment"
        and source.get("status") == "ACTIVE"
        and source.get("score") is not None
    ]
    usd_macro_score = round(sum(float(item["score"]) for item in usd_active) / len(usd_active), 2) if usd_active else None
    return {
        "direction": direction,
        "score": score,
        "gold_support_score": score,
        "usd_macro_score": usd_macro_score,
        "confidence": confidence,
        "status": status,
        "coverage": round(active_weight, 4),
        "drivers": drivers,
        "active_factors": sorted(active),
        "missing_factors": sorted(set(GOLD_FACTOR_WEIGHTS) - set(active)),
        "required_factors_present": required_present,
        "confidence_components": confidence_components,
        "top_reasons": _top_reasons(drivers),
    }
