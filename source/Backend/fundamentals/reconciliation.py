"""Provider-neutral release matching helpers."""
from __future__ import annotations

import hashlib
from datetime import timedelta, timezone

from fundamentals.normalization.events import canonical_event_id
from models import EconomicEvent, EconomicEventProviderLink


MATCH_TOLERANCE = timedelta(hours=18)


def provider_dataset(event):
    raw = event.raw or {}
    return str(
        raw.get("provider_dataset")
        or raw.get("dataset")
        or ("mql5" if event.provider == "jblanked_mql5" else "default")
    ).lower()


def provider_fingerprint(event):
    identity = event.provider_event_id or f"{event.indicator}|{event.currency}"
    # MQL5 Event_ID identifies the economic series, not one dated release.  A
    # timestamp is therefore part of the provider-release identity; otherwise
    # every monthly release is permanently linked to the first month ingested.
    if str(event.provider or "").lower().startswith("jblanked"):
        identity = f"{identity}|{event.release_time.astimezone(timezone.utc).isoformat()}"
    basis = f"{event.provider}|{provider_dataset(event)}|{identity}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def provider_neutral_event_id(event):
    return canonical_event_id(event.currency, event.indicator, event.release_time, "canonical")


def find_canonical_event(session, event):
    fingerprint = provider_fingerprint(event)
    linked = (
        session.query(EconomicEvent)
        .join(
            EconomicEventProviderLink,
            EconomicEventProviderLink.economic_event_id == EconomicEvent.id,
        )
        .filter(EconomicEventProviderLink.provider_fingerprint == fingerprint)
        .one_or_none()
    )
    if linked is not None:
        return linked
    earliest = event.release_time - MATCH_TOLERANCE
    latest = event.release_time + MATCH_TOLERANCE
    candidates = (
        session.query(EconomicEvent)
        .filter(
            EconomicEvent.currency == event.currency,
            EconomicEvent.indicator == event.indicator,
            EconomicEvent.release_time >= earliest,
            EconomicEvent.release_time <= latest,
        )
        .all()
    )
    if not candidates:
        return None
    target = event.release_time.astimezone(timezone.utc)
    return min(
        candidates,
        key=lambda row: abs(
            ((row.release_time.replace(tzinfo=timezone.utc) if row.release_time.tzinfo is None else row.release_time)
             .astimezone(timezone.utc) - target).total_seconds()
        ),
    )
