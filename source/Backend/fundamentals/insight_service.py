from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timezone

from fundamentals.engine import calculate_fundamental_state
from fundamentals.normalization.indicators import EURUSD_ENGINE_INDICATOR_BASES
from fundamentals.pair_bias import SUPPORTED_PAIRS
from fundamentals.repositories.observations import (
    next_high_impact_event,
    provider_health,
    relevant_reconciled_observation_history,
)
from fundamentals.repositories.snapshots import persist_insight


# Uvicorn owns the production log handlers. Using its error logger makes the
# existing private timing record visible in Render without exposing it via API.
logger = logging.getLogger("uvicorn.error")


def _iso(value):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return value


def _serialize_factor(factor):
    return {
        **factor,
        "evidence": [
            {key: _iso(value) for key, value in evidence.items()}
            for evidence in factor.get("evidence") or []
        ],
    }


def _serialize_currency(result):
    return {
        **result,
        "factors": {
            name: _serialize_factor(factor)
            for name, factor in result.get("factors", {}).items()
        },
        "evidence": [
            {key: _iso(value) for key, value in evidence.items()}
            for evidence in result.get("evidence") or []
        ],
    }


def _next_event_payload(event, now):
    if not event:
        return None
    release_time = event.get("release_time")
    countdown = max(0, int((release_time - now).total_seconds())) if release_time else None
    return {
        "event_name": event.get("event_name"),
        "currency": event.get("currency"),
        "release_time": _iso(release_time),
        "countdown": countdown,
        "previous": event.get("previous"),
        "forecast": event.get("forecast"),
        "actual": event.get("actual"),
        "impact": event.get("impact", "UNKNOWN"),
        "source": event.get("provider"),
        "data_status": event.get("data_status"),
    }


def _ingest_current_calendar_safely(now):
    try:
        from services.news_service import fetch_calendar_events

        fetch_calendar_events(now=now)
    except Exception as exc:
        print("FUNDAMENTAL_CALENDAR_INGEST_WARNING =", str(exc))


def _history_lookup_from_observations(observations):
    """Build surprise history from the already reconciled request dataset.

    The previous implementation opened a new database session and repeated a
    full historical query for every candidate event. Reusing one reconciled
    history collection eliminates that read-path N+1 query pattern.
    """
    def released(item):
        value = item.get("release_time")
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    by_series = {}
    for item in observations or []:
        if item.get("actual") is None or item.get("forecast") is None:
            continue
        key = (str(item.get("currency") or "").upper(), item.get("indicator"))
        by_series.setdefault(key, []).append(item)
    for rows in by_series.values():
        rows.sort(
            key=lambda item: released(item)
            or datetime.min.replace(tzinfo=timezone.utc)
        )

    def lookup(event):
        key = (str(event.get("currency") or "").upper(), event.get("indicator"))
        before = released(event)
        if before is None:
            return []
        return [
            item for item in by_series.get(key, ())
            if released(item) is not None and released(item) < before
        ]

    return lookup


def get_fundamental_insight(
    symbol="EURUSD",
    *,
    now=None,
    observations=None,
    next_event=None,
    session_factory=None,
    persist=True,
    ingest=False,
    history_lookup_override=None,
):
    request_started = time.perf_counter()
    timings = {"snapshot_persistence_ms": 0.0, "external_provider_calls": 0}
    normalized = str(symbol or "").upper().replace("/", "")
    if normalized not in SUPPORTED_PAIRS:
        raise ValueError("Phase 1 supports EURUSD only")
    current = now or datetime.now(timezone.utc)
    currencies = SUPPORTED_PAIRS[normalized]
    repository_observations = observations is None
    history_rows = None
    if repository_observations:
        if ingest:
            _ingest_current_calendar_safely(current)
        observations, history_rows = relevant_reconciled_observation_history(
            currencies,
            EURUSD_ENGINE_INDICATOR_BASES,
            now=current,
            session_factory=session_factory,
            timing=timings,
        )
    if next_event is None:
        next_event = next_high_impact_event(
            currencies,
            now=current,
            session_factory=session_factory,
            timing=timings,
        )

    if history_lookup_override is not None:
        history_lookup = history_lookup_override
    elif repository_observations:
        history_lookup = _history_lookup_from_observations(history_rows)
    else:
        history_lookup = _history_lookup_from_observations(observations)

    calculation_started = time.perf_counter()
    state = calculate_fundamental_state(
        normalized,
        observations,
        history_lookup=history_lookup,
        now=current,
    )
    timings["factor_calculation_and_evidence_ms"] = round(
        (time.perf_counter() - calculation_started) * 1000, 2
    )
    serialization_started = time.perf_counter()
    serialized_currencies = {
        currency: _serialize_currency(result)
        for currency, result in state["currency_strength"].items()
    }
    pair = state["pair"]
    confidence = state["confidence"]
    status = pair["status"]
    direction = pair["direction"] if status == "ACTIVE" else "NEUTRAL"
    active_factors = sorted({
        name
        for result in serialized_currencies.values()
        for name in result.get("active_factors") or []
    })
    missing_factors = sorted({
        name
        for result in serialized_currencies.values()
        for name in result.get("missing_factors") or []
    })
    provisional_count = sum(
        int(factor.get("provisional_count") or 0)
        for result in serialized_currencies.values()
        for factor in result["factors"].values()
    )
    sources = sorted({str(item.get("provider") or "unknown") for item in observations})
    fallback_active = bool(sources) and all(source.startswith("manual") for source in sources)
    try:
        health = provider_health(
            now=current, session_factory=session_factory, timing=timings
        )
    except Exception as exc:
        health = {
            "last_successful_provider_fetch": None,
            "authoritative_provider": None,
            "stale_age_seconds": None,
            "observation_count": len(observations),
            "provider_failures_recent": 0,
            "providers": {},
            "health_error": str(exc),
        }
    calculation_id = str(uuid.uuid4())
    insight_id = str(uuid.uuid4())
    response = {
        "schema_version": 1,
        "insight_id": insight_id,
        "calculation_id": calculation_id,
        "symbol": normalized,
        "generated_at": _iso(current),
        "overall_bias": {
            "direction": direction,
            "pair_score": pair.get("pair_score"),
            "confidence": confidence,
            "status": status,
        },
        "currency_strength": serialized_currencies,
        "top_reasons": state["top_reasons"],
        "next_high_impact_event": _next_event_payload(next_event, current),
        "trading_guidance": {
            "preference": (
                "INSUFFICIENT_DATA"
                if status != "ACTIVE"
                else f"PREFER_{direction}" if direction in {"BUY", "SELL"} else "NEUTRAL"
            ),
            "message": (
                "Fundamental evidence is currently insufficient."
                if status != "ACTIVE"
                else (
                    f"Prefer {direction} setups. Opposing setups require stronger technical confirmation."
                    if direction in {"BUY", "SELL"}
                    else "No strong fundamental directional advantage."
                )
            ),
            "execution_connected": False,
        },
        "data_quality": {
            "coverage_percent": round(float(pair.get("coverage") or 0) * 100, 2),
            "observation_count": health.get("observation_count", len(observations)),
            "providers": sources,
            "fallback_active": fallback_active,
            "active_factors": active_factors,
            "missing_factors": missing_factors,
            "provisional_factor_count": provisional_count,
            "last_successful_provider_fetch": _iso(health.get("last_successful_provider_fetch")),
            "last_provider_fetch_attempt": _iso(health.get("last_provider_fetch_attempt")),
            "authoritative_provider": health.get("authoritative_provider"),
            "stale_age_seconds": health.get("stale_age_seconds"),
            "provider_failures_recent": health.get("provider_failures_recent", 0),
            "provider_health": {
                provider: {key: _iso(value) for key, value in detail.items()}
                for provider, detail in (health.get("providers") or {}).items()
            },
            "engine_readiness": "READY" if status == "ACTIVE" else "NOT_READY",
            "status": "ACTIVE" if status == "ACTIVE" else "INSUFFICIENT_DATA",
        },
        "read_only": True,
    }
    timings["response_construction_ms"] = round(
        (time.perf_counter() - serialization_started) * 1000, 2
    )
    serialization_started = time.perf_counter()
    json.dumps(response, default=str)
    timings["response_serialization_estimate_ms"] = round(
        (time.perf_counter() - serialization_started) * 1000, 2
    )
    if persist:
        persistence_started = time.perf_counter()
        try:
            persist_insight(
                calculation_id,
                insight_id,
                response,
                serialized_currencies,
                session_factory=session_factory,
            )
        except Exception as exc:
            print("FUNDAMENTAL_SNAPSHOT_PERSIST_WARNING =", str(exc))
        timings["snapshot_persistence_ms"] = round(
            (time.perf_counter() - persistence_started) * 1000, 2
        )
    timings["total_service_ms"] = round((time.perf_counter() - request_started) * 1000, 2)
    logger.info(
        "FUNDAMENTAL_INSIGHT_TIMING %s",
        json.dumps({"symbol": normalized, **timings}, sort_keys=True),
    )
    return response
