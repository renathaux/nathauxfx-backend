from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone

from db import SessionLocal
from fundamentals.authority import (
    RULE_VERSION,
    choose_field,
    values_disagree,
)
from fundamentals.normalization.events import normalize_economic_event
from fundamentals.reconciliation import (
    find_canonical_event,
    provider_dataset,
    provider_fingerprint,
    provider_neutral_event_id,
)
from models import (
    EconomicEvent,
    EconomicEventDisagreement,
    EconomicEventObservation,
    EconomicEventProviderLink,
    EconomicProviderFetch,
)


LOGGER = logging.getLogger(__name__)


def _json_safe(value):
    return json.loads(json.dumps(value, default=str))


def _observation_hash(event):
    payload = {
        "event_id": event.event_id,
        "provider": event.provider,
        "actual": event.actual,
        "forecast": event.forecast,
        "previous": event.previous,
        "revised_previous": event.revised_previous,
        "provider_timestamp": (
            event.provider_timestamp.isoformat() if event.provider_timestamp else None
        ),
        "data_status": event.data_status,
    }
    encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _record_provider_link(session, row, event):
    fingerprint = provider_fingerprint(event)
    link = session.query(EconomicEventProviderLink).filter_by(
        provider_fingerprint=fingerprint
    ).one_or_none()
    if link is None:
        link = EconomicEventProviderLink(
            economic_event_id=row.id,
            provider=event.provider,
            provider_dataset=provider_dataset(event),
            provider_event_id=event.provider_event_id,
            provider_fingerprint=fingerprint,
            reported_event_name=event.event_name,
            reported_indicator=event.indicator,
            reported_release_time=event.release_time,
            reported_impact=event.impact,
            first_seen_at=event.fetched_at,
            last_seen_at=event.fetched_at,
        )
        session.add(link)
    else:
        link.last_seen_at = event.fetched_at
        link.reported_event_name = event.event_name
        link.reported_indicator = event.indicator
        link.reported_release_time = event.release_time
        link.reported_impact = event.impact


def _select_event_metadata(row, event):
    release = choose_field("release_time", [
        {"value": row.release_time, "provider": row.provider, "sequence": 0},
        {"value": event.release_time, "provider": event.provider, "sequence": 1},
    ], event.indicator, event.currency)
    impact = choose_field("impact", [
        {"value": None if row.impact == "UNKNOWN" else row.impact, "provider": row.provider, "sequence": 0},
        {"value": None if event.impact == "UNKNOWN" else event.impact, "provider": event.provider, "sequence": 1},
    ], event.indicator, event.currency)
    if release and release["provider"] == event.provider:
        row.release_time = event.release_time
        row.provider = event.provider
        row.provider_event_id = event.provider_event_id
        row.event_name = event.event_name
    if impact:
        row.impact = impact["value"]
    row.country = event.country or row.country
    row.data_status = "RELEASED" if "RELEASED" in {row.data_status, event.data_status} else event.data_status
    row.last_seen_at = event.fetched_at


def _disagreement_hash(event_id, field, authoritative, conflicting):
    basis = "|".join((
        str(event_id), field, authoritative["provider"], str(authoritative["value"]),
        conflicting["provider"], str(conflicting["value"]), RULE_VERSION,
    ))
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def _record_disagreements(session, row, event):
    previous_rows = session.query(EconomicEventObservation).filter_by(
        economic_event_id=row.id
    ).all()
    incoming = {
        "actual": event.actual,
        "forecast": event.forecast,
        "previous": event.previous,
        "revised_previous": event.revised_previous,
    }
    for field, new_value in incoming.items():
        if new_value in (None, ""):
            continue
        candidates = [{
            "value": getattr(old, field), "provider": old.provider, "sequence": old.id or 0,
        } for old in previous_rows if getattr(old, field) not in (None, "")]
        candidates.append({
            "value": new_value, "provider": event.provider,
            "sequence": max([item["sequence"] for item in candidates] or [0]) + 1,
        })
        authoritative = choose_field(field, candidates, event.indicator, event.currency)
        for candidate in candidates:
            if (
                candidate is authoritative
                or candidate["provider"] == authoritative["provider"]
                or not values_disagree(authoritative["value"], candidate["value"])
            ):
                continue
            digest = _disagreement_hash(row.id, field, authoritative, candidate)
            if session.query(EconomicEventDisagreement.id).filter_by(
                disagreement_hash=digest
            ).first():
                continue
            session.add(EconomicEventDisagreement(
                disagreement_hash=digest,
                economic_event_id=row.id,
                field_name=field,
                authoritative_provider=authoritative["provider"],
                authoritative_value=str(authoritative["value"]),
                conflicting_provider=candidate["provider"],
                conflicting_value=str(candidate["value"]),
                rule_version=RULE_VERSION,
                detected_at=event.fetched_at,
            ))


def prepare_calendar_batch(provider, raw_events, normalized_events=None, *, completed_at=None):
    completed = completed_at or datetime.now(timezone.utc)
    canonical = []
    for item in normalized_events if normalized_events is not None else raw_events:
        event = normalize_economic_event(item, provider=provider, fetched_at=completed)
        if event is not None:
            canonical.append(event)
    return canonical


def preview_calendar_batch(session, provider, raw_events, normalized_events=None, *, completed_at=None):
    """Read-only insert preview used by --dry-run."""
    canonical = prepare_calendar_batch(
        provider, raw_events, normalized_events, completed_at=completed_at
    )
    hashes = [_observation_hash(event) for event in canonical]
    existing = set()
    if hashes:
        existing = {
            value for (value,) in session.query(EconomicEventObservation.observation_hash)
            .filter(EconomicEventObservation.observation_hash.in_(hashes)).all()
        }
    duplicates_in_payload = len(hashes) - len(set(hashes))
    duplicates_existing = sum(value in existing for value in set(hashes))
    duplicates = duplicates_in_payload + duplicates_existing
    return {
        "provider": str(provider).lower(),
        "events": len(canonical),
        "observations_would_add": max(0, len(hashes) - duplicates),
        "duplicates_skipped": duplicates,
        "canonical": canonical,
    }


def persist_calendar_batch_in_session(
    session,
    provider,
    raw_events,
    normalized_events=None,
    *,
    status="SUCCESS",
    error=None,
    started_at=None,
    completed_at=None,
):
    """Stage one provider batch in the caller's current transaction."""
    completed = completed_at or datetime.now(timezone.utc)
    started = started_at or completed
    canonical = prepare_calendar_batch(
        provider, raw_events, normalized_events, completed_at=completed
    )
    fetch = EconomicProviderFetch(
        provider=str(provider).lower(),
        started_at=started,
        completed_at=completed,
        status=str(status).upper(),
        raw_event_count=len(raw_events or []),
        normalized_event_count=len(canonical),
        error=str(error) if error else None,
    )
    session.add(fetch)
    observations_added = 0
    duplicates_skipped = 0
    for event in canonical:
        row = find_canonical_event(session, event)
        if row is None:
            event.event_id = provider_neutral_event_id(event)
            row = EconomicEvent(
                event_id=event.event_id,
                event_name=event.event_name,
                indicator=event.indicator,
                country=event.country,
                currency=event.currency,
                impact=event.impact,
                release_time=event.release_time,
                provider=event.provider,
                provider_event_id=event.provider_event_id,
                data_status=event.data_status,
                first_seen_at=event.fetched_at,
                last_seen_at=event.fetched_at,
            )
            session.add(row)
            session.flush()
        else:
            event.event_id = row.event_id
            _select_event_metadata(row, event)

        _record_provider_link(session, row, event)
        _record_disagreements(session, row, event)

        observation_hash = _observation_hash(event)
        exists = session.query(EconomicEventObservation.id).filter_by(
            observation_hash=observation_hash
        ).first()
        if exists:
            duplicates_skipped += 1
            continue
        session.add(EconomicEventObservation(
            observation_hash=observation_hash,
            economic_event_id=row.id,
            actual=None if event.actual is None else str(event.actual),
            forecast=None if event.forecast is None else str(event.forecast),
            previous=None if event.previous is None else str(event.previous),
            revised_previous=(
                None if event.revised_previous is None else str(event.revised_previous)
            ),
            provider=event.provider,
            provider_timestamp=event.provider_timestamp,
            fetched_at=event.fetched_at,
            data_status=event.data_status,
            raw_payload=_json_safe(event.raw),
        ))
        observations_added += 1
    session.flush()
    return {
        "provider": str(provider).lower(),
        "events": len(canonical),
        "observations_added": observations_added,
        "duplicates_skipped": duplicates_skipped,
        "fetch_id": fetch.id,
    }


def persist_calendar_batch(
    provider,
    raw_events,
    normalized_events=None,
    *,
    status="SUCCESS",
    error=None,
    started_at=None,
    completed_at=None,
    session_factory=None,
):
    """Persist one provider result without overwriting observation history."""
    factory = session_factory or SessionLocal
    with factory() as session:
        try:
            result = persist_calendar_batch_in_session(
                session,
                provider,
                raw_events,
                normalized_events,
                status=status,
                error=error,
                started_at=started_at,
                completed_at=completed_at,
            )
            session.commit()
            if int(result.get("observations_added") or 0) > 0:
                # Import lazily so persistence remains independent of the API
                # route while every successful observation change invalidates
                # any completed read-only insight derived from older data.
                try:
                    from fundamentals.insight_cache import invalidate

                    invalidate()
                except Exception:
                    # The committed ingestion remains authoritative. A cache
                    # maintenance failure must not make the provider cycle look
                    # uncommitted; the short TTL remains the safety fallback.
                    LOGGER.exception("FUNDAMENTAL_INSIGHT_CACHE_INVALIDATION_FAILED")
            return result
        except Exception:
            session.rollback()
            raise


def record_failed_fetch(provider, error, *, started_at=None, session_factory=None):
    return persist_calendar_batch(
        provider,
        [],
        [],
        status="FAILED",
        error=error,
        started_at=started_at,
        session_factory=session_factory,
    )
