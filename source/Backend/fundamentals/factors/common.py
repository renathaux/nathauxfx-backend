from __future__ import annotations

import math
from datetime import datetime, timezone

from fundamentals.factors.surprises import (
    IMPORTANCE_WEIGHT,
    OFFICIAL_PROVIDERS,
    PROVIDER_QUALITY,
    parse_numeric,
)
from fundamentals.freshness import evidence_freshness
from fundamentals.normalization.indicators import indicator_metadata


def clamp(value, minimum=-100.0, maximum=100.0):
    return max(minimum, min(maximum, float(value)))


def utc(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def event_age_days(event, now=None):
    current = utc(now or datetime.now(timezone.utc))
    released = utc(event.get("release_time"))
    if released is None:
        return float("inf")
    return max(0.0, (current - released).total_seconds() / 86400.0)


def tiered_recency_weight(age_days, policy=False):
    """Strong 0-7d, moderate 8-30d, weak 31-90d; policy decays longer."""
    if policy:
        if age_days <= 30:
            return 1.0
        if age_days <= 90:
            return 0.70
        if age_days <= 180:
            return 0.40
        if age_days <= 365:
            return 0.15
        return 0.0
    if age_days <= 7:
        return 1.0
    if age_days <= 30:
        return 0.60
    if age_days <= 90:
        return 0.25
    return 0.0


def trusted_event(event):
    provider = str(event.get("provider") or "unknown").lower()
    status = str(event.get("data_status") or "").upper()
    return (
        status != "UNRELIABLE_STATIC"
        and not provider.startswith("manual")
        and PROVIDER_QUALITY.get(provider, PROVIDER_QUALITY["unknown"]) > 0
    )


def event_quality(event):
    provider = str(event.get("provider") or "unknown").lower()
    impact = str(event.get("impact") or "UNKNOWN").upper()
    importance = IMPORTANCE_WEIGHT.get(impact, 0.0)
    metadata = indicator_metadata(event.get("indicator") or event.get("event_name"))
    if importance <= 0 and provider in OFFICIAL_PROVIDERS and metadata["recognized"]:
        # Official releases may not publish commercial HIGH/MEDIUM labels.
        # Give recognized evidence a scoring weight without changing its
        # externally visible impact from UNKNOWN.
        importance = 1.0 if metadata["category"] == "policy" else 0.65
    return PROVIDER_QUALITY.get(provider, PROVIDER_QUALITY["unknown"]) * importance


def normalized_delta(current, reference, floor=0.10):
    current_value = parse_numeric(current)
    reference_value = parse_numeric(reference)
    if current_value is None or reference_value is None:
        return None
    denominator = max(abs(reference_value), float(floor))
    return clamp((current_value - reference_value) / denominator * 100.0)


def revision_stability(event):
    previous = parse_numeric(event.get("previous"))
    revised = parse_numeric(event.get("revised_previous"))
    if previous is None or revised is None:
        return 1.0, False, 0.0
    change = revised - previous
    stability = max(0.35, 1.0 - abs(change) / max(abs(previous), 0.1))
    return stability, abs(change) > 1e-12, change


def weighted_factor_result(
    factor,
    candidates,
    *,
    now=None,
    stale_after_days=45,
    horizon_days=90,
    minimum_events=1,
):
    current = utc(now or datetime.now(timezone.utc))
    usable = []
    for item in candidates:
        event = item["event"]
        if not trusted_event(event):
            continue
        age = event_age_days(event, current)
        freshness_state = evidence_freshness(event, now=current)
        if age > horizon_days and freshness_state["status"] != "ACTIVE":
            continue
        recency = tiered_recency_weight(age, policy=factor == "policy_score")
        if recency <= 0 and freshness_state["status"] == "ACTIVE":
            recency = 0.15
        quality = event_quality(event)
        if recency <= 0 or quality <= 0:
            continue
        stability, revised, revision_change = revision_stability(event)
        weight = recency * quality * stability
        if weight <= 0:
            continue
        score = clamp(item["score"])
        evidence = {
            "event_id": event.get("event_id"),
            "event_name": event.get("event_name"),
            "indicator": event.get("indicator"),
            "currency": event.get("currency"),
            "release_time": event.get("release_time"),
            "provider": event.get("provider"),
            "actual": event.get("actual"),
            "forecast": event.get("forecast"),
            "previous": event.get("previous"),
            "revised_previous": event.get("revised_previous"),
            "score": round(score, 2),
            "contribution": round(score * weight, 2),
            "recency_weight": recency,
            "provider_quality": round(quality, 4),
            "revision_stability": round(stability, 4),
            "revision_affected": revised,
            "revision_change": round(revision_change, 4),
            "reason": item.get("reason"),
            "freshness_status": freshness_state["status"],
            "freshness_reason": freshness_state["reason"],
            "expected_next_release": freshness_state["expected_next_release"],
            "valid_until": freshness_state["valid_until"],
        }
        usable.append((score, weight, age, evidence, freshness_state))

    if len(usable) < minimum_events:
        return {
            "factor": factor,
            "score": None,
            "confidence": 0.0,
            "status": "INSUFFICIENT_DATA",
            "coverage": 0.0,
            "evidence_count": 0,
            "provisional_count": 0,
            "revision_stability": 0.0,
            "evidence": [],
            "updated_at": None,
        }

    denominator = sum(weight for _score, weight, _age, _evidence, _freshness in usable)
    score = sum(score * weight for score, weight, _age, _evidence, _freshness in usable) / denominator
    newest_age = min(age for _score, _weight, age, _evidence, _freshness in usable)
    average_quality = sum(
        evidence["provider_quality"] for _score, _weight, _age, evidence, _freshness in usable
    ) / len(usable)
    average_revision = sum(
        evidence["revision_stability"] for _score, _weight, _age, evidence, _freshness in usable
    ) / len(usable)
    evidence_depth = min(1.0, len(usable) / 4.0)
    # One ordinary release cannot swing a full factor. A structured central-bank
    # decision is deliberately allowed more influence because it is itself a
    # primary policy action, but still receives a depth discount.
    depth_multiplier = (
        0.80 + 0.20 * evidence_depth
        if factor == "policy_score"
        else 0.55 + 0.45 * evidence_depth
    )
    score *= depth_multiplier
    freshness = tiered_recency_weight(newest_age, policy=factor == "policy_score")
    confidence = 100.0 * (
        0.35 * average_quality
        + 0.25 * freshness
        + 0.20 * evidence_depth
        + 0.20 * average_revision
    )
    newest = min(usable, key=lambda item: item[2])
    status = newest[4]["status"]
    newest_time = max(
        utc(evidence["release_time"])
        for _score, _weight, _age, evidence, _freshness in usable
        if evidence.get("release_time") is not None
    )
    return {
        "factor": factor,
        "score": round(clamp(score), 2),
        "confidence": round(min(confidence, 90.0), 2),
        "status": status,
        "coverage": min(1.0, len(usable) / 4.0),
        "evidence_count": len(usable),
        "provisional_count": 0,
        "revision_stability": round(average_revision, 4),
        "evidence": [item[3] for item in usable],
        "updated_at": newest_time,
    }
