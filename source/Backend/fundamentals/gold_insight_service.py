from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fundamentals.gold_config import (
    GOLD_RELEVANT_EVENT_INDICATORS,
    XAUUSD_ENGINE_INDICATOR_BASES,
)
from fundamentals.gold_engine import calculate_xauusd_state
from fundamentals.insight_service import _iso, _next_event_payload, _serialize_factor
from fundamentals.repositories.observations import latest_released_observations, next_high_impact_event, provider_health


def get_xauusd_fundamental_insight(
    *, now=None, observations=None, next_event=None, session_factory=None
):
    """Build a read-only XAUUSD insight; it has no execution or snapshot writes."""
    current = now or datetime.now(timezone.utc)
    if observations is None:
        observations = latest_released_observations(
            ("USD",),
            now=current,
            session_factory=session_factory,
            indicators=XAUUSD_ENGINE_INDICATOR_BASES,
        )
    if next_event is None:
        next_event = next_high_impact_event(
            ("USD",), now=current, session_factory=session_factory,
            indicators=GOLD_RELEVANT_EVENT_INDICATORS,
        )
    state = calculate_xauusd_state(observations, now=current)
    try:
        health = provider_health(now=current, session_factory=session_factory)
    except Exception as exc:
        health = {"observation_count": len(observations), "providers": {}, "health_error": str(exc)}
    drivers = {name: _serialize_factor(item) for name, item in state["drivers"].items()}
    sources = sorted({str(item.get("provider") or "unknown") for item in observations})
    status = state["status"]
    preference = "INSUFFICIENT_DATA" if status != "ACTIVE" else (
        f"PREFER_{state['direction']}" if state["direction"] in {"BUY", "SELL"} else "NEUTRAL"
    )
    return {
        "schema_version": 1,
        "insight_id": str(uuid.uuid4()),
        "calculation_id": str(uuid.uuid4()),
        "symbol": "XAUUSD",
        "generated_at": _iso(current),
        "overall_bias": {
            "direction": state["direction"],
            "score": state["score"],
            "pair_score": state["score"],
            "confidence": state["confidence"],
            "status": status,
        },
        "gold_support_score": state["gold_support_score"],
        "usd_macro_score": state["usd_macro_score"],
        "drivers": drivers,
        "top_reasons": state["top_reasons"],
        "next_high_impact_event": _next_event_payload(next_event, current),
        "trading_guidance": {
            "preference": preference,
            "message": (
                (
                    "Fundamental evidence is insufficient because verified real-yield coverage is missing."
                    if not state["required_factors_present"]
                    else "Verified yields are available, but overall fundamental factor coverage is insufficient."
                )
                if status != "ACTIVE" else
                f"Prefer {state['direction']} XAUUSD setups; fundamentals remain informational only."
                if state["direction"] in {"BUY", "SELL"} else
                "No strong fundamental directional advantage for gold."
            ),
            "execution_connected": False,
        },
        "data_quality": {
            "coverage_percent": round(state["coverage"] * 100, 2),
            "observation_count": health.get("observation_count", len(observations)),
            "providers": sources,
            "active_factors": state["active_factors"],
            "missing_factors": state["missing_factors"],
            "required_factors_present": state["required_factors_present"],
            "confidence_components": state["confidence_components"],
            "authoritative_provider": health.get("authoritative_provider"),
            "last_successful_provider_fetch": _iso(health.get("last_successful_provider_fetch")),
            "engine_readiness": "READY" if status == "ACTIVE" else "NOT_READY",
            "status": status,
        },
        "read_only": True,
    }
