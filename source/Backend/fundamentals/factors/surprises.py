from __future__ import annotations

import math
import re
import statistics
from datetime import datetime, timedelta, timezone

from fundamentals.normalization.indicators import indicator_metadata
from fundamentals.freshness import evidence_freshness


HISTORICAL_Z_MIN_SAMPLES = 8
Z_SCORE_CLIP = 3.0
RECENCY_HALF_LIFE_DAYS = 30.0
PROVISIONAL_REFERENCE_RATIO = 0.25

IMPORTANCE_WEIGHT = {
    "HIGH": 1.0,
    "MEDIUM": 0.65,
    "LOW": 0.35,
    "UNKNOWN": 0.0,
}
PROVIDER_QUALITY = {
    "jblanked_live": 0.90,
    "jblanked": 0.90,
    "jblanked_cache": 0.85,
    "jblanked_mql5": 0.90,
    "fmp": 0.85,
    "finnhub": 0.80,
    "bls": 1.0,
    "bea": 1.0,
    "eurostat": 1.0,
    "federal_reserve": 1.0,
    "ecb": 1.0,
    "treasury": 1.0,
    "fred": 0.95,
    "manual": 0.0,
    "unknown": 0.25,
}
OFFICIAL_PROVIDERS = {"bls", "bea", "eurostat", "federal_reserve", "ecb", "treasury", "fred"}
DENOMINATOR_FLOOR = {
    "nonfarm_payrolls": 50_000.0,
    "jobless_claims": 10_000.0,
    "interest_rate": 0.25,
}


def parse_numeric(value):
    if value in (None, "", "--", "N/A"):
        return None
    text = str(value).strip().replace(",", "")
    multiplier = 1.0
    if text.endswith("%"):
        text = text[:-1]
    elif text.upper().endswith("K"):
        text, multiplier = text[:-1], 1_000.0
    elif text.upper().endswith("M"):
        text, multiplier = text[:-1], 1_000_000.0
    elif text.upper().endswith("B"):
        text, multiplier = text[:-1], 1_000_000_000.0
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(match.group(0)) * multiplier if match else None


def _clamp(value, minimum=-100.0, maximum=100.0):
    return max(minimum, min(maximum, float(value)))


def recency_weight(release_time, now=None):
    current = now or datetime.now(timezone.utc)
    if release_time is None:
        return 0.0
    if release_time.tzinfo is None:
        release_time = release_time.replace(tzinfo=timezone.utc)
    age_days = max(0.0, (current - release_time).total_seconds() / 86400.0)
    return math.pow(0.5, age_days / RECENCY_HALF_LIFE_DAYS)


def raw_surprise(event):
    actual = parse_numeric(event.get("actual"))
    forecast = parse_numeric(event.get("forecast"))
    if actual is None or forecast is None:
        return None
    return actual - forecast


def score_event_surprise(event, historical_surprises=None, now=None):
    metadata = indicator_metadata(event.get("indicator") or event.get("event_name"))
    raw = raw_surprise(event)
    provider = str(event.get("provider") or "unknown").lower()
    impact = str(event.get("impact") or "UNKNOWN").upper()
    quality = PROVIDER_QUALITY.get(provider, PROVIDER_QUALITY["unknown"])
    importance = IMPORTANCE_WEIGHT.get(impact, 0.0)
    if importance <= 0 and provider in OFFICIAL_PROVIDERS and metadata["recognized"]:
        # This is an internal evidence weight, not an inferred event impact.
        # The externally reported impact remains UNKNOWN.
        importance = 1.0 if metadata["category"] == "policy" else 0.65
    if event.get("data_status") == "UNRELIABLE_STATIC" or quality <= 0:
        return {"status": "UNRELIABLE_SOURCE", "score": None, "event_id": event.get("event_id")}
    if raw is None:
        return {"status": "MISSING_ACTUAL_OR_FORECAST", "score": None, "event_id": event.get("event_id")}
    if not metadata["recognized"] or metadata["higher_is_currency_bullish"] is None:
        return {"status": "UNKNOWN_INDICATOR_DIRECTION", "score": None, "event_id": event.get("event_id")}
    if importance <= 0:
        return {"status": "UNKNOWN_IMPORTANCE", "score": None, "event_id": event.get("event_id")}

    history = [float(value) for value in (historical_surprises or []) if value is not None]
    deviation = statistics.stdev(history) if len(history) >= HISTORICAL_Z_MIN_SAMPLES else None
    if deviation is not None and deviation > 1e-12:
        standardized = raw / deviation
        base_score = _clamp(standardized / Z_SCORE_CLIP * 100.0)
        method = "HISTORICAL_Z_SCORE"
        provisional = False
    else:
        forecast = parse_numeric(event.get("forecast")) or 0.0
        floor = DENOMINATOR_FLOOR.get(metadata["base_indicator"], 0.10)
        normalized = raw / max(abs(forecast), floor)
        base_score = _clamp(normalized / PROVISIONAL_REFERENCE_RATIO * 100.0)
        standardized = None
        method = "PROVISIONAL_NORMALIZED_SURPRISE"
        provisional = True

    if metadata["higher_is_currency_bullish"] is False:
        base_score *= -1
    freshness = recency_weight(event.get("release_time"), now=now)
    freshness_state = evidence_freshness(event, now=now)
    contribution = base_score * importance * freshness * quality
    revision_stability = 1.0
    previous = parse_numeric(event.get("previous"))
    revised = parse_numeric(event.get("revised_previous"))
    if previous is not None and revised is not None:
        revision_stability = max(0.5, 1.0 - abs(revised - previous) / max(abs(previous), 0.1))
    return {
        "status": "PROVISIONAL" if provisional else "STANDARDIZED",
        "event_id": event.get("event_id"),
        "event_name": event.get("event_name"),
        "currency": event.get("currency"),
        "indicator": metadata["indicator"],
        "category": metadata["category"],
        "raw_surprise": raw,
        "standardized_surprise": standardized,
        "base_score": round(base_score, 4),
        "score": round(_clamp(contribution), 4),
        "method": method,
        "importance_weight": importance,
        "recency_weight": round(freshness, 6),
        "provider_quality": quality,
        "revision_stability": round(revision_stability, 6),
        "release_time": event.get("release_time"),
        "provider": provider,
        "freshness_status": freshness_state["status"],
        "freshness_reason": freshness_state["reason"],
        "expected_next_release": freshness_state["expected_next_release"],
        "valid_until": freshness_state["valid_until"],
    }


def score_currency_surprises(events, currency, history_lookup=None, now=None):
    evidence = []
    for event in events or []:
        if str(event.get("currency") or "").upper() != str(currency).upper():
            continue
        release_time = event.get("release_time")
        if release_time is None:
            continue
        current = now or datetime.now(timezone.utc)
        if release_time.tzinfo is None:
            release_time = release_time.replace(tzinfo=timezone.utc)
        freshness_state = evidence_freshness(event, now=current)
        if (
            current - release_time > timedelta(days=90)
            and freshness_state["status"] != "ACTIVE"
        ):
            continue
        history_rows = history_lookup(event) if history_lookup else []
        history = [raw_surprise(item) for item in history_rows]
        result = score_event_surprise(event, history, now=now)
        if result.get("score") is not None:
            evidence.append(result)
    if not evidence:
        return {
            "factor": "surprise_score",
            "score": None,
            "status": "INSUFFICIENT_DATA",
            "coverage": 0.0,
            "confidence": 0.0,
            "method": None,
            "evidence": [],
            "updated_at": None,
            "evidence_count": 0,
            "provisional_count": 0,
            "revision_stability": 0.0,
        }
    weights = [
        item["importance_weight"] * item["recency_weight"] * item["provider_quality"]
        for item in evidence
    ]
    denominator = sum(weights)
    score = sum(item["base_score"] * weight for item, weight in zip(evidence, weights)) / denominator
    standardized_count = sum(item["method"] == "HISTORICAL_Z_SCORE" for item in evidence)
    average_quality = sum(item["provider_quality"] for item in evidence) / len(evidence)
    average_revision = sum(item["revision_stability"] for item in evidence) / len(evidence)
    coverage = min(1.0, len(evidence) / 5.0)
    confidence = min(
        100.0,
        100.0 * average_quality * (0.4 + 0.6 * coverage) * (
            1.0 if standardized_count == len(evidence) else 0.70
        ),
    )
    return {
        "factor": "surprise_score",
        "score": round(_clamp(score), 2),
        "status": "ACTIVE" if any(
            item.get("freshness_status") == "ACTIVE" for item in evidence
        ) else "STALE",
        "coverage": round(coverage, 4),
        "confidence": round(confidence, 2),
        "method": (
            "HISTORICAL_Z_SCORE"
            if standardized_count == len(evidence)
            else "MIXED_OR_PROVISIONAL"
        ),
        "evidence": evidence,
        "updated_at": max(item["release_time"] for item in evidence),
        "evidence_count": len(evidence),
        "provisional_count": len(evidence) - standardized_count,
        "revision_stability": round(average_revision, 4),
    }
