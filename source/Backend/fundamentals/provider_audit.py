"""Pure analysis helpers shared by preflight and dry-run backfill."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import timezone

from fundamentals.normalization.events import normalize_economic_event


AUDITED_FIELDS = (
    "actual", "forecast", "previous", "impact", "release_time", "provider_event_id"
)


def _present(value, field):
    if field == "impact":
        return str(value or "").upper() not in ("", "UNKNOWN", "NONE")
    return value not in (None, "", "--", "N/A")


def analyze_events(normalized_events, *, provider_identity):
    canonical = []
    invalid = []
    for index, item in enumerate(normalized_events or []):
        item_provider = (
            item.get("provider") if isinstance(item, dict) else None
        ) or provider_identity
        event = normalize_economic_event(item, provider=item_provider)
        if event is None:
            invalid.append({"index": index, "reason": "missing name/currency/timestamp"})
        else:
            canonical.append(event)

    field_counts = {
        field: sum(_present(getattr(event, field), field) for event in canonical)
        for field in AUDITED_FIELDS
    }
    currency_counts = Counter(event.currency for event in canonical)
    impact_counts = Counter(event.impact for event in canonical)
    exact_groups = defaultdict(list)
    identity_groups = defaultdict(list)
    provider_id_meanings = defaultdict(set)
    for event in canonical:
        exact_key = (
            event.provider,
            event.provider_event_id,
            event.currency,
            event.indicator,
            event.release_time,
            str(event.actual),
            str(event.forecast),
            str(event.previous),
            event.impact,
        )
        exact_groups[exact_key].append(event)
        identity_key = (event.currency, event.indicator, event.release_time)
        identity_groups[identity_key].append(event)
        if event.provider_event_id:
            provider_id_meanings[event.provider_event_id].add(
                (event.currency, event.indicator)
            )

    duplicates = [
        {
            "currency": key[2],
            "indicator": key[3],
            "release_time": key[4].astimezone(timezone.utc).isoformat(),
            "provider_event_id": key[1],
            "count": len(items),
        }
        for key, items in exact_groups.items() if len(items) > 1
    ]
    conflicts = []
    for (currency, indicator, release_time), items in identity_groups.items():
        variants = {
            (
                item.provider, str(item.actual), str(item.forecast),
                str(item.previous), item.impact,
            )
            for item in items
        }
        if len(variants) > 1:
            conflicts.append({
                "currency": currency,
                "indicator": indicator,
                "release_time": release_time.astimezone(timezone.utc).isoformat(),
                "datasets": sorted({item.provider for item in items}),
                "variant_count": len(variants),
                "resolution": "PRESERVED_SEPARATELY_NOT_AVERAGED",
            })

    inconsistent_ids = [
        {"provider_event_id": event_id, "meanings": sorted(meanings)}
        for event_id, meanings in provider_id_meanings.items() if len(meanings) > 1
    ]
    timestamps_utc = sum(
        event.release_time.tzinfo is not None
        and event.release_time.utcoffset() == timezone.utc.utcoffset(event.release_time)
        for event in canonical
    )
    missing = {
        field: len(canonical) - count for field, count in field_counts.items()
    }
    normalized_indicators = sorted({
        f"{event.event_name} -> {event.indicator}" for event in canonical
    })
    return {
        "event_count": len(canonical),
        "currency_counts": dict(sorted(currency_counts.items())),
        "impact_counts": dict(sorted(impact_counts.items())),
        "high_impact_event_count": impact_counts.get("HIGH", 0),
        "field_counts": field_counts,
        "missing_field_counts": missing,
        "stable_provider_event_id_count": field_counts["provider_event_id"],
        "normalized_indicators": normalized_indicators,
        "invalid_events": invalid,
        "duplicate_count": sum(item["count"] - 1 for item in duplicates),
        "duplicates": duplicates,
        "conflict_count": len(conflicts),
        "conflicts": conflicts,
        "inconsistent_provider_ids": inconsistent_ids,
        "timezone": {
            "all_normalized_to_utc": timestamps_utc == len(canonical),
            "utc_timestamp_count": timestamps_utc,
            "timestamp_count": len(canonical),
        },
        "canonical_events": canonical,
    }
