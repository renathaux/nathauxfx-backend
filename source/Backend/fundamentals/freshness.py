"""Indicator-aware evidence freshness.

Freshness is based on when the next observation should normally be published,
not on one global age cutoff.  This module changes evidence readiness only; it
does not change release values or trading behavior.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fundamentals.normalization.indicators import indicator_metadata
from fundamentals.gold_config import GOLD_YIELD_MAX_MISSING_MARKET_DAYS
from fundamentals.market_calendar import market_days_elapsed


def _utc(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def freshness_policy(indicator):
    """Return expected publication cadence and ingestion grace in days."""
    metadata = indicator_metadata(indicator)
    normalized = metadata["indicator"]
    base = metadata["base_indicator"]
    if metadata["category"] == "policy":
        # Policy evidence remains active between scheduled meetings.  The
        # scoring horizon still independently caps it at 365 days.
        return {"cadence_days": 180, "grace_days": 30, "frequency": "POLICY"}
    if metadata["category"] == "yields":
        return {"cadence_days": 1, "grace_days": 3, "frequency": "DAILY"}
    if metadata["category"] == "risk_sentiment":
        return {"cadence_days": 7, "grace_days": 7, "frequency": "WEEKLY"}
    if base == "jobless_claims":
        return {"cadence_days": 7, "grace_days": 7, "frequency": "WEEKLY"}
    if "_q_q" in normalized or base in {"gdp", "employment_change"}:
        return {"cadence_days": 100, "grace_days": 30, "frequency": "QUARTERLY"}
    if metadata["category"] in {"inflation", "employment", "growth"}:
        return {"cadence_days": 32, "grace_days": 14, "frequency": "MONTHLY"}
    return {"cadence_days": 45, "grace_days": 14, "frequency": "UNSPECIFIED"}


def evidence_freshness(event, *, now=None):
    current = _utc(now or datetime.now(timezone.utc))
    released = _utc(event.get("release_time"))
    policy = freshness_policy(event.get("indicator") or event.get("event_name"))
    if released is None:
        return {
            **policy,
            "age_days": None,
            "expected_next_release": None,
            "valid_until": None,
            "status": "STALE",
            "reason": "MISSING_RELEASE_TIMESTAMP",
        }
    if policy["frequency"] == "DAILY":
        elapsed = market_days_elapsed(released.date(), current.date())
        stale = elapsed > GOLD_YIELD_MAX_MISSING_MARKET_DAYS
        return {
            **policy,
            "age_days": max(0.0, (current - released).total_seconds() / 86400.0),
            "market_days_elapsed": elapsed,
            "expected_next_release": None,
            "valid_until": None,
            "status": "STALE" if stale else "ACTIVE",
            "reason": "TOO_MANY_MISSING_MARKET_DAYS" if stale else "LATEST_DAILY_MARKET_OBSERVATION_CURRENT",
        }
    expected = released + timedelta(days=policy["cadence_days"])
    valid_until = expected + timedelta(days=policy["grace_days"])
    stale = current > valid_until
    return {
        **policy,
        "age_days": max(0.0, (current - released).total_seconds() / 86400.0),
        "expected_next_release": expected,
        "valid_until": valid_until,
        "status": "STALE" if stale else "ACTIVE",
        "reason": "EXPECTED_NEWER_RELEASE_MISSING" if stale else "LATEST_RELEASE_WITHIN_EXPECTED_WINDOW",
    }
