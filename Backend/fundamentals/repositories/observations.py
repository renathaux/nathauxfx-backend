from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
import time

from sqlalchemy import and_, func, or_

from db import SessionLocal
from fundamentals.authority import choose_field
from fundamentals.normalization.events import normalize_datetime
from models import EconomicEvent, EconomicEventObservation, EconomicProviderFetch


TRUSTED_PROVIDERS = {
    "jblanked", "jblanked_live", "jblanked_cache", "jblanked_mql5",
    "jblanked_forex_factory", "jblanked_fxstreet", "fmp", "finnhub",
    "bls", "bea", "eurostat", "federal_reserve", "ecb", "treasury", "fred",
}

_JBLANKED_PROVIDERS = {item for item in TRUSTED_PROVIDERS if item.startswith("jblanked")}
_PLACEHOLDER_MARKERS = {
    "data not loaded", "not loaded", "no data", "missing", "unavailable",
    "n/a", "na", "null", "none", "--", "-",
}


def _utc(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _nested_values(payload):
    if isinstance(payload, dict):
        for key, value in payload.items():
            yield str(key).lower(), value
            yield from _nested_values(value)
    elif isinstance(payload, list):
        for value in payload:
            yield from _nested_values(value)


def _observation_validation_metadata(observation):
    """Derive raw-payload validation facts once for all authority fields."""
    reported_release_time = None
    placeholder_actual = False
    bea_release_actual_source = ""
    for key, value in _nested_values(observation.raw_payload or {}):
        if reported_release_time is None and key in {
            "release_time", "datetime", "date", "timestamp", "time_utc",
        }:
            reported_release_time = normalize_datetime(value)
        if key in {"outcome", "strength", "quality", "actual", "actual_value"}:
            text = str(value or "").strip().lower()
            if text in _PLACEHOLDER_MARKERS or any(
                marker in text for marker in ("data not loaded", "not available")
            ):
                placeholder_actual = True
        if not bea_release_actual_source and key == "release_actual_source":
            bea_release_actual_source = str(value or "").lower()
    return {
        "reported_release_time": reported_release_time,
        "jblanked_placeholder_actual": placeholder_actual,
        "bea_release_actual_source": bea_release_actual_source,
    }


def _bea_archive_unit_mismatch(observation, field, *, source=None):
    if field not in {"previous", "revised_previous"}:
        return False
    if source is None:
        source = _observation_validation_metadata(observation)[
            "bea_release_actual_source"
        ]
    if "bea news archive" not in source:
        return False
    try:
        actual_match = re.search(r"[-+]?\d+(?:\.\d+)?", str(observation.actual))
        value_match = re.search(r"[-+]?\d+(?:\.\d+)?", str(getattr(observation, field)))
        if not actual_match or not value_match:
            return False
        return abs(float(actual_match.group())) <= 25 and abs(float(value_match.group())) > 25
    except (TypeError, ValueError):
        return False


def _candidate_rejection(event, observation, field, *, validation_metadata=None):
    metadata = validation_metadata or _observation_validation_metadata(observation)
    provider = str(observation.provider or "").lower()
    reported = metadata["reported_release_time"]
    canonical = _utc(event.release_time)
    if reported is not None and canonical is not None:
        if abs((reported - canonical).total_seconds()) > 18 * 60 * 60:
            return "PROVIDER_RELEASE_MISMATCH"
    value = getattr(observation, field)
    if value in (None, ""):
        return "MISSING_VALUE"
    if (
        provider in _JBLANKED_PROVIDERS
        and field == "actual"
        and metadata["jblanked_placeholder_actual"]
    ):
        return "JBLANKED_PLACEHOLDER_ACTUAL"
    if provider == "bea" and _bea_archive_unit_mismatch(
        observation,
        field,
        source=metadata["bea_release_actual_source"],
    ):
        return "BEA_INCOMPATIBLE_PREVIOUS_UNIT"
    if field in {"actual", "forecast", "previous", "revised_previous"}:
        text = str(value).strip().lower()
        if text in _PLACEHOLDER_MARKERS:
            return "PLACEHOLDER_VALUE"
        if not re.search(r"[-+]?\d", text):
            return "MALFORMED_NUMERIC_VALUE"
    return None


def _serialize(event, observation):
    return {
        "event_id": event.event_id,
        "event_name": event.event_name,
        "indicator": event.indicator,
        "country": event.country,
        "currency": event.currency,
        "impact": event.impact,
        "release_time": event.release_time,
        "provider": observation.provider or event.provider,
        "provider_event_id": event.provider_event_id,
        "actual": observation.actual,
        "forecast": observation.forecast,
        "previous": observation.previous,
        "revised_previous": observation.revised_previous,
        "provider_timestamp": observation.provider_timestamp,
        "fetched_at": observation.fetched_at,
        "data_status": observation.data_status,
    }


def _serialize_authoritative(event, observations):
    """Merge fields by authority while preserving source attribution per field."""
    result = {
        "event_id": event.event_id,
        "event_name": event.event_name,
        "indicator": event.indicator,
        "country": event.country,
        "currency": event.currency,
        "impact": event.impact,
        "release_time": event.release_time,
        "provider_event_id": event.provider_event_id,
        "data_status": event.data_status,
        "field_sources": {},
        "data_quality_rejections": [],
    }
    validation_metadata = {
        id(observation): _observation_validation_metadata(observation)
        for observation in observations
    }
    for field in ("actual", "forecast", "previous", "revised_previous"):
        candidates = []
        for observation in observations:
            rejection = _candidate_rejection(
                event,
                observation,
                field,
                validation_metadata=validation_metadata[id(observation)],
            )
            if rejection:
                if rejection != "MISSING_VALUE":
                    result["data_quality_rejections"].append({
                        "field": field,
                        "provider": observation.provider,
                        "observation_id": observation.id,
                        "reason": rejection,
                    })
                continue
            candidates.append({
                "value": getattr(observation, field),
                "provider": observation.provider,
                "sequence": observation.id or 0,
                "observation": observation,
            })
        selected = choose_field(field, candidates, event.indicator, event.currency)
        result[field] = selected["value"] if selected else None
        result["field_sources"][field] = selected["provider"] if selected else None
    actual_source = result["field_sources"].get("actual")
    forecast_source = result["field_sources"].get("forecast")
    selected_observation = next((
        item for item in observations if item.provider == actual_source
    ), observations[0] if observations else None)
    result.update({
        "provider": actual_source or forecast_source or event.provider,
        "provider_timestamp": selected_observation.provider_timestamp if selected_observation else None,
        "fetched_at": max((item.fetched_at for item in observations), default=event.last_seen_at),
    })
    return result


def _group_authoritative(rows):
    grouped = {}
    for event, observation in rows:
        grouped.setdefault(event.id, [event, []])[1].append(observation)
    effective = [_serialize_authoritative(event, observations) for event, observations in grouped.values()]
    effective.sort(key=lambda item: _utc(item["release_time"]) or datetime.min.replace(tzinfo=timezone.utc))
    prior_by_series = {}
    for item in effective:
        key = (item.get("currency"), item.get("indicator"))
        prior = prior_by_series.get(key)
        if item.get("previous") is None and prior and prior.get("actual") is not None:
            item["previous"] = prior["actual"]
            item["field_sources"]["previous"] = prior["field_sources"].get("actual")
            item["previous_derived_from_event_id"] = prior.get("event_id")
        if item.get("actual") is not None:
            prior_by_series[key] = item
    latest_seen = set()
    for item in reversed(effective):
        key = (item.get("currency"), item.get("indicator"))
        item["is_latest_release_for_indicator"] = key not in latest_seen
        latest_seen.add(key)
    return list(reversed(effective))


def latest_released_observations(
    currencies,
    *,
    now=None,
    lookback_days=365,
    session_factory=None,
    timing=None,
    indicators=None,
):
    factory = session_factory or SessionLocal
    current = now or datetime.now(timezone.utc)
    cutoff = current - timedelta(days=lookback_days)
    with factory() as session:
        connection_started = time.perf_counter()
        session.connection()
        if timing is not None:
            timing["observation_connection_acquisition_ms"] = round(
                (time.perf_counter() - connection_started) * 1000, 2
            )
        query_started = time.perf_counter()
        query = (
            session.query(EconomicEvent, EconomicEventObservation)
            .join(
                EconomicEventObservation,
                EconomicEventObservation.economic_event_id == EconomicEvent.id,
            )
            .filter(
                EconomicEvent.currency.in_([str(value).upper() for value in currencies]),
                EconomicEvent.release_time <= current,
                EconomicEvent.release_time >= cutoff,
            )
        )
        if indicators:
            bases = sorted({str(value) for value in indicators if value})
            query = query.filter(or_(*[
                or_(
                    EconomicEvent.indicator == base,
                    EconomicEvent.indicator.like(f"{base}_%"),
                )
                for base in bases
            ]))
        rows = (
            query.order_by(
                EconomicEvent.release_time.desc(),
                EconomicEventObservation.fetched_at.desc(),
            )
            .all()
        )
    if timing is not None:
        timing["economic_observation_query_ms"] = round(
            (time.perf_counter() - query_started) * 1000, 2
        )
    reconciliation_started = time.perf_counter()
    result = [item for item in _group_authoritative(rows) if item.get("actual") is not None]
    if timing is not None:
        timing["reconciliation_ms"] = round(
            (time.perf_counter() - reconciliation_started) * 1000, 2
        )
        timing["economic_observation_rows"] = len(rows)
        timing["canonical_observations"] = len(result)
    return result


def relevant_reconciled_observation_history(
    currencies,
    indicator_bases,
    *,
    now=None,
    lookback_days=365,
    session_factory=None,
    timing=None,
):
    """Load and reconcile the complete relevant history exactly once.

    The returned current observations retain the existing 365-day factor
    horizon. Surprise history remains complete for every allowed series.
    """
    normalized_currencies = sorted({
        str(value).upper() for value in currencies or () if value
    })
    normalized_bases = sorted({
        str(value) for value in indicator_bases or () if value
    })
    if not normalized_currencies or not normalized_bases:
        return [], []
    factory = session_factory or SessionLocal
    current = now or datetime.now(timezone.utc)
    cutoff = current - timedelta(days=lookback_days)
    indicator_filter = or_(*[
        or_(
            EconomicEvent.indicator == base,
            EconomicEvent.indicator.startswith(f"{base}_", autoescape=True),
        )
        for base in normalized_bases
    ])
    with factory() as session:
        connection_started = time.perf_counter()
        session.connection()
        if timing is not None:
            timing["observation_connection_acquisition_ms"] = round(
                (time.perf_counter() - connection_started) * 1000, 2
            )
            # The former second history connection/query is deliberately gone.
            timing["history_connection_acquisition_ms"] = 0.0
        query_started = time.perf_counter()
        rows = (
            session.query(EconomicEvent, EconomicEventObservation)
            .join(
                EconomicEventObservation,
                EconomicEventObservation.economic_event_id == EconomicEvent.id,
            )
            .filter(
                EconomicEvent.currency.in_(normalized_currencies),
                indicator_filter,
                EconomicEvent.release_time <= current,
            )
            .order_by(
                EconomicEvent.release_time.desc(),
                EconomicEventObservation.fetched_at.desc(),
            )
            .all()
        )
    if timing is not None:
        timing["economic_observation_query_ms"] = round(
            (time.perf_counter() - query_started) * 1000, 2
        )
        timing["historical_surprise_query_ms"] = 0.0
    reconciliation_started = time.perf_counter()
    reconciled = _group_authoritative(rows)
    release_by_event_id = {
        item.get("event_id"): _utc(item.get("release_time"))
        for item in reconciled
    }
    current_observations = [
        item for item in reconciled
        if item.get("actual") is not None
        and cutoff <= _utc(item.get("release_time")) <= current
    ]
    # The former current-window query could only derive a missing `previous`
    # value from another event inside that same window. Preserve that boundary
    # while still reconciling the complete history just once.
    for item in current_observations:
        derived_event_id = item.get("previous_derived_from_event_id")
        derived_release = release_by_event_id.get(derived_event_id)
        if derived_event_id and (derived_release is None or derived_release < cutoff):
            item["previous"] = None
            item["field_sources"]["previous"] = None
            item.pop("previous_derived_from_event_id", None)
    history_observations = [
        item for item in reconciled
        if item.get("actual") is not None and item.get("forecast") is not None
    ]
    if timing is not None:
        timing["reconciliation_ms"] = round(
            (time.perf_counter() - reconciliation_started) * 1000, 2
        )
        timing["historical_surprise_reconciliation_ms"] = 0.0
        timing["economic_observation_rows"] = len(rows)
        timing["canonical_observations"] = len(current_observations)
        timing["historical_surprise_rows"] = len(history_observations)
    return current_observations, history_observations


def historical_surprises(indicator, currency, *, before=None, session_factory=None):
    factory = session_factory or SessionLocal
    with factory() as session:
        query = (
            session.query(EconomicEvent, EconomicEventObservation)
            .join(
                EconomicEventObservation,
                EconomicEventObservation.economic_event_id == EconomicEvent.id,
            )
            .filter(
                EconomicEvent.indicator == indicator,
                EconomicEvent.currency == currency,
            )
        )
        if before is not None:
            query = query.filter(EconomicEvent.release_time < before)
        rows = query.order_by(
            EconomicEvent.release_time.desc(),
            EconomicEventObservation.fetched_at.desc(),
        ).all()
        return [item for item in _group_authoritative(rows)
                if item.get("actual") is not None and item.get("forecast") is not None]


def historical_surprises_for_series(series, *, session_factory=None, timing=None):
    """Load all surprise history for a bounded set of series in one query."""
    normalized = sorted({
        (str(currency or "").upper(), str(indicator or ""))
        for currency, indicator in series or ()
        if currency and indicator
    })
    if not normalized:
        return []
    factory = session_factory or SessionLocal
    with factory() as session:
        connection_started = time.perf_counter()
        session.connection()
        if timing is not None:
            timing["history_connection_acquisition_ms"] = round(
                (time.perf_counter() - connection_started) * 1000, 2
            )
        query_started = time.perf_counter()
        rows = (
            session.query(EconomicEvent, EconomicEventObservation)
            .join(
                EconomicEventObservation,
                EconomicEventObservation.economic_event_id == EconomicEvent.id,
            )
            .filter(or_(*[
                and_(
                    EconomicEvent.currency == currency,
                    EconomicEvent.indicator == indicator,
                )
                for currency, indicator in normalized
            ]))
            .order_by(
                EconomicEvent.release_time.desc(),
                EconomicEventObservation.fetched_at.desc(),
            )
            .all()
        )
    if timing is not None:
        timing["historical_surprise_query_ms"] = round(
            (time.perf_counter() - query_started) * 1000, 2
        )
    reconciliation_started = time.perf_counter()
    result = [
        item for item in _group_authoritative(rows)
        if item.get("actual") is not None and item.get("forecast") is not None
    ]
    if timing is not None:
        timing["historical_surprise_reconciliation_ms"] = round(
            (time.perf_counter() - reconciliation_started) * 1000, 2
        )
        timing["historical_surprise_rows"] = len(rows)
    return result


def next_high_impact_event(
    currencies, *, now=None, session_factory=None, indicators=None, timing=None
):
    factory = session_factory or SessionLocal
    current = now or datetime.now(timezone.utc)
    with factory() as session:
        connection_started = time.perf_counter()
        session.connection()
        if timing is not None:
            timing["next_event_connection_acquisition_ms"] = round(
                (time.perf_counter() - connection_started) * 1000, 2
            )
        query_started = time.perf_counter()
        query = (
            session.query(EconomicEvent, EconomicEventObservation)
            .join(
                EconomicEventObservation,
                EconomicEventObservation.economic_event_id == EconomicEvent.id,
            )
            .filter(
                EconomicEvent.currency.in_([str(value).upper() for value in currencies]),
                EconomicEvent.release_time >= current,
                EconomicEvent.impact == "HIGH",
                EconomicEvent.data_status != "UNRELIABLE_STATIC",
                EconomicEvent.provider.in_(sorted(TRUSTED_PROVIDERS)),
            )
        )
        if indicators:
            bases = sorted(set(indicators))
            query = query.filter(or_(*[
                or_(
                    EconomicEvent.indicator == base,
                    EconomicEvent.indicator.like(f"{base}_%"),
                )
                for base in bases
            ]))
        rows = (
            query.order_by(
                EconomicEvent.release_time.asc(),
                EconomicEventObservation.fetched_at.desc(),
            )
            .all()
        )
    if timing is not None:
        timing["next_event_query_ms"] = round(
            (time.perf_counter() - query_started) * 1000, 2
        )
    reconciliation_started = time.perf_counter()
    grouped = _group_authoritative(rows)
    if timing is not None:
        timing["next_event_reconciliation_ms"] = round(
            (time.perf_counter() - reconciliation_started) * 1000, 2
        )
    return grouped[0] if grouped else None


def provider_health(*, now=None, session_factory=None, timing=None):
    factory = session_factory or SessionLocal
    current = now or datetime.now(timezone.utc)
    with factory() as session:
        connection_started = time.perf_counter()
        session.connection()
        if timing is not None:
            timing["provider_health_connection_acquisition_ms"] = round(
                (time.perf_counter() - connection_started) * 1000, 2
            )
        query_started = time.perf_counter()
        latest_completed = (
            session.query(
                EconomicProviderFetch.provider.label("provider"),
                func.max(EconomicProviderFetch.completed_at).label("completed_at"),
            )
            .group_by(EconomicProviderFetch.provider)
            .subquery()
        )
        fetches = (
            session.query(EconomicProviderFetch)
            .join(
                latest_completed,
                and_(
                    EconomicProviderFetch.provider == latest_completed.c.provider,
                    EconomicProviderFetch.completed_at == latest_completed.c.completed_at,
                ),
            )
            .all()
        )
        latest_by_provider = {row.provider: row for row in fetches}
        last_attempt = max((row.completed_at for row in fetches), default=None)
        last_success_row = (
            session.query(EconomicProviderFetch)
            .filter(
                EconomicProviderFetch.status == "SUCCESS",
                EconomicProviderFetch.provider.in_(sorted(TRUSTED_PROVIDERS)),
            )
            .order_by(EconomicProviderFetch.completed_at.desc())
            .limit(1)
            .one_or_none()
        )
        last_success = last_success_row.completed_at if last_success_row else None
        authoritative = last_success_row.provider if last_success_row else None
        failures = (
            session.query(func.count(EconomicProviderFetch.id))
            .filter(
                EconomicProviderFetch.status == "FAILED",
                EconomicProviderFetch.completed_at >= current - timedelta(hours=24),
            )
            .scalar()
            or 0
        )
        observation_count = session.query(func.count(EconomicEventObservation.id)).scalar() or 0
        event_count = session.query(func.count(EconomicEvent.id)).scalar() or 0
    if timing is not None:
        timing["provider_health_query_ms"] = round(
            (time.perf_counter() - query_started) * 1000, 2
        )
    if last_success and last_success.tzinfo is None:
        last_success = last_success.replace(tzinfo=timezone.utc)
    if last_attempt and last_attempt.tzinfo is None:
        last_attempt = last_attempt.replace(tzinfo=timezone.utc)
    stale_age = (
        max(0.0, (current - last_success).total_seconds())
        if last_success
        else None
    )
    return {
        "last_successful_provider_fetch": last_success,
        "last_provider_fetch_attempt": last_attempt,
        "authoritative_provider": authoritative,
        "stale_age_seconds": stale_age,
        "observation_count": int(observation_count),
        "event_count": int(event_count),
        "provider_failures_recent": int(failures),
        "providers": {
            provider: {
                "status": row.status,
                "completed_at": row.completed_at,
                "error": row.error,
                "event_count": row.normalized_event_count,
            }
            for provider, row in latest_by_provider.items()
        },
    }
