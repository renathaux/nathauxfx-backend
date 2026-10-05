from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from fundamentals.normalization.currencies import normalize_country, normalize_currency
from fundamentals.normalization.indicators import normalize_indicator
from fundamentals.schemas import EconomicEventSchema


VALID_IMPACTS = {"HIGH", "MEDIUM", "LOW", "UNKNOWN"}


def utc_now():
    return datetime.now(timezone.utc)


def normalize_datetime(value):
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        parsed = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    else:
        text = str(value or "").strip().replace("Z", "+00:00")
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def normalize_impact(value):
    text = str(value or "").strip().upper()
    if "HIGH" in text or text in {"3", "RED"}:
        return "HIGH"
    if "MED" in text or text in {"2", "ORANGE"}:
        return "MEDIUM"
    if "LOW" in text or text in {"1", "YELLOW"}:
        return "LOW"
    return "UNKNOWN"


def canonical_event_id(currency, indicator, release_time, provider=None):
    # JBlanked datasets are independent observations. Including their dataset
    # identity prevents MQL5/Forex Factory/FxStreet values from being merged.
    provider_name = str(provider or "").lower()
    dataset = provider_name if provider_name.startswith("jblanked_") else "canonical"
    basis = (
        f"{dataset}|{currency}|{indicator}|"
        f"{release_time.astimezone(timezone.utc).isoformat()}"
    )
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


def normalize_economic_event(raw, provider=None, fetched_at=None):
    raw = dict(raw or {})
    event_name = str(
        raw.get("event_name")
        or raw.get("event")
        or raw.get("title")
        or raw.get("name")
        or raw.get("indicator")
        or ""
    ).strip()
    release_time = normalize_datetime(
        raw.get("release_time")
        or raw.get("time_utc")
        or raw.get("time")
        or raw.get("date")
        or raw.get("datetime")
        or raw.get("timestamp")
    )
    country = raw.get("country")
    currency = normalize_currency(
        raw.get("currency") or raw.get("country_currency") or raw.get("ccy"),
        country,
    )
    if not event_name or not release_time or currency == "UNKNOWN":
        return None
    indicator = normalize_indicator(raw.get("indicator") or event_name)
    provider_name = str(provider or raw.get("provider") or raw.get("source") or "unknown").lower()
    provider_event_id = raw.get("provider_event_id") or raw.get("id") or raw.get("event_id")
    actual = raw.get("actual", raw.get("actual_value", raw.get("value")))
    forecast = raw.get("forecast", raw.get("consensus", raw.get("expected")))
    previous = raw.get("previous", raw.get("prior", raw.get("prev")))
    revised_previous = raw.get("revised_previous", raw.get("revised"))
    impact = normalize_impact(raw.get("impact") or raw.get("importance") or raw.get("priority"))
    status = str(raw.get("data_status") or "").upper()
    if not status:
        status = "RELEASED" if actual not in (None, "", "--", "N/A") else "SCHEDULED"
    if provider_name.startswith("manual"):
        status = "UNRELIABLE_STATIC"
        impact = "UNKNOWN"
    fetched = normalize_datetime(fetched_at or raw.get("fetched_at")) or utc_now()
    provider_timestamp = normalize_datetime(raw.get("provider_timestamp") or raw.get("updated_at"))
    return EconomicEventSchema(
        event_id=canonical_event_id(currency, indicator, release_time, provider_name),
        event_name=event_name,
        indicator=indicator,
        country=normalize_country(country, currency),
        currency=currency,
        impact=impact if impact in VALID_IMPACTS else "UNKNOWN",
        release_time=release_time,
        actual=None if actual in ("", "--", "N/A") else actual,
        forecast=None if forecast in ("", "--", "N/A") else forecast,
        previous=None if previous in ("", "--", "N/A") else previous,
        revised_previous=None if revised_previous in ("", "--", "N/A") else revised_previous,
        provider=provider_name,
        provider_event_id=str(provider_event_id) if provider_event_id not in (None, "") else None,
        provider_timestamp=provider_timestamp,
        fetched_at=fetched,
        data_status=status,
        raw=raw,
    )
