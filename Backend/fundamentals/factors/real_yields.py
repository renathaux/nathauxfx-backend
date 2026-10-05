from __future__ import annotations

from fundamentals.factors.common import clamp, trusted_event, utc
from fundamentals.factors.surprises import PROVIDER_QUALITY, parse_numeric
from fundamentals.freshness import evidence_freshness
from fundamentals.gold_config import (
    GOLD_YIELD_BASELINE_OBSERVATIONS,
    GOLD_YIELD_DEADBAND_PERCENTAGE_POINTS,
    GOLD_YIELD_FULL_SIGNAL_PERCENTAGE_POINTS,
    GOLD_YIELD_MINIMUM_PRIOR_OBSERVATIONS,
)
from fundamentals.normalization.indicators import indicator_metadata


def _empty(reason="INSUFFICIENT_HISTORY"):
    return {
        "factor": "real_yields",
        "score": None,
        "confidence": 0.0,
        "status": "INSUFFICIENT_DATA",
        "coverage": 0.0,
        "evidence_count": 0,
        "provisional_count": 0,
        "revision_stability": 0.0,
        "evidence": [],
        "updated_at": None,
        "method": "LATEST_VS_PRIOR_MARKET_OBSERVATION_BASELINE",
        "reason": reason,
    }


def _series(observations, base_indicator, currency):
    by_date = {}
    for event in observations or []:
        if str(event.get("currency") or "").upper() != str(currency).upper():
            continue
        metadata = indicator_metadata(event.get("indicator") or event.get("event_name"))
        if metadata["base_indicator"] != base_indicator or not trusted_event(event):
            continue
        value = parse_numeric(event.get("actual"))
        released = utc(event.get("release_time"))
        if value is None or released is None:
            continue
        provider = str(event.get("provider") or "unknown").lower()
        quality = PROVIDER_QUALITY.get(provider, PROVIDER_QUALITY["unknown"])
        key = released.date()
        current = by_date.get(key)
        if current is None or quality > current[2]:
            by_date[key] = (released, event, quality, value)
    return [by_date[key] for key in sorted(by_date)]


def _trend_result(observations, base_indicator, currency, now):
    values = _series(observations, base_indicator, currency)
    required = GOLD_YIELD_MINIMUM_PRIOR_OBSERVATIONS + 1
    if len(values) < required:
        return _empty()
    latest_time, latest_event, latest_quality, latest_value = values[-1]
    prior = values[-(GOLD_YIELD_BASELINE_OBSERVATIONS + 1):-1]
    if len(prior) < GOLD_YIELD_MINIMUM_PRIOR_OBSERVATIONS:
        return _empty()
    baseline = sum(item[3] for item in prior) / len(prior)
    change = latest_value - baseline
    magnitude = abs(change)
    if magnitude <= GOLD_YIELD_DEADBAND_PERCENTAGE_POINTS:
        score = 0.0
    else:
        usable_range = max(
            1e-9,
            GOLD_YIELD_FULL_SIGNAL_PERCENTAGE_POINTS - GOLD_YIELD_DEADBAND_PERCENTAGE_POINTS,
        )
        score = clamp(
            (magnitude - GOLD_YIELD_DEADBAND_PERCENTAGE_POINTS) / usable_range * 100.0
        )
        if change < 0:
            score *= -1
    freshness = evidence_freshness(latest_event, now=now)
    status = freshness["status"]
    depth = min(1.0, len(prior) / GOLD_YIELD_BASELINE_OBSERVATIONS)
    confidence = min(90.0, 100.0 * (0.45 * latest_quality + 0.35 * depth + 0.20 * (status == "ACTIVE")))
    evidence = [{
        "event_id": latest_event.get("event_id"),
        "event_name": latest_event.get("event_name"),
        "indicator": latest_event.get("indicator"),
        "currency": latest_event.get("currency"),
        "release_time": latest_time,
        "provider": latest_event.get("provider"),
        "actual": latest_event.get("actual"),
        "previous": latest_event.get("previous"),
        "score": round(score, 2),
        "contribution": round(score, 2),
        "provider_quality": latest_quality,
        "freshness_status": freshness["status"],
        "freshness_reason": freshness["reason"],
        "market_days_elapsed": freshness.get("market_days_elapsed"),
        "reason": "Latest Treasury yield versus the preceding market-day baseline.",
        "latest_value": round(latest_value, 4),
        "baseline_value": round(baseline, 4),
        "change_percentage_points": round(change, 4),
        "change_basis_points": round(change * 100.0, 2),
        "baseline_observation_dates": [item[0].date().isoformat() for item in prior],
    }]
    return {
        "factor": "real_yields",
        "score": round(score, 2),
        "confidence": round(confidence, 2),
        "status": status,
        "coverage": min(1.0, len(prior) / GOLD_YIELD_BASELINE_OBSERVATIONS),
        "evidence_count": len(prior) + 1,
        "provisional_count": 0,
        "revision_stability": 1.0,
        "evidence": evidence,
        "updated_at": latest_time,
        "method": "LATEST_VS_PRIOR_MARKET_OBSERVATION_BASELINE",
        "reason": freshness["reason"],
    }


def calculate_real_yield_factor(observations, currency="USD", now=None):
    """Calculate USD yield pressure; positive values are bearish for gold.

    A current real 10-year yield trend is mandatory. A current nominal 10-year
    trend contributes 20%, but cannot replace the real-yield series.
    """
    real = _trend_result(observations, "us_10y_real_yield", currency, now)
    if real.get("status") != "ACTIVE" or real.get("score") is None:
        return real
    nominal = _trend_result(observations, "us_10y_treasury_yield", currency, now)
    if nominal.get("status") == "ACTIVE" and nominal.get("score") is not None:
        real["score"] = round(clamp(0.80 * real["score"] + 0.20 * nominal["score"]), 2)
        real["confidence"] = round(min(90.0, 0.80 * real["confidence"] + 0.20 * nominal["confidence"]), 2)
        real["evidence"] = list(real.get("evidence") or []) + list(nominal.get("evidence") or [])
        real["evidence_count"] += nominal["evidence_count"]
        real["nominal_component_status"] = "ACTIVE"
    else:
        real["nominal_component_status"] = nominal.get("status", "INSUFFICIENT_DATA")
    real["real_component_weight"] = 0.80
    real["nominal_component_weight"] = 0.20 if real["nominal_component_status"] == "ACTIVE" else 0.0
    return real
