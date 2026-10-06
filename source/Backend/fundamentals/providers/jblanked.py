"""JBlanked MQL5 historical calendar adapter.

JBlanked documents calendar timestamps as GMT+3. This adapter converts those
timestamps to aware UTC datetimes before canonical normalization.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone

import requests


DATASET_ID = "jblanked_mql5"
SOURCE_LABEL = "JBlanked MQL5 calendar/range"
DEFAULT_BASE_URL = "https://www.jblanked.com/news/api"
RANGE_PATH = "/mql5/calendar/range/"
PROVIDER_TIMEZONE = timezone(timedelta(hours=3), name="GMT+3")


class JBlankedAccessError(RuntimeError):
    """Safe provider error that never includes credentials."""


def _value(raw, *keys):
    normalized = {
        str(key).replace("_", "").lower(): value
        for key, value in dict(raw or {}).items()
    }
    for key in keys:
        value = normalized.get(str(key).replace("_", "").lower())
        if value not in (None, "", "--", "N/A"):
            return value
    return None


def _extract_events(payload):
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("events", "data", "calendar", "results", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    if _value(payload, "name", "event", "event_name"):
        return [payload]
    return []


def parse_mql5_timestamp(value):
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip().replace("Z", "+00:00")
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            parsed = None
            for fmt in ("%Y.%m.%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y.%m.%d %H:%M"):
                try:
                    parsed = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
            if parsed is None:
                return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=PROVIDER_TIMEZONE)
    return parsed.astimezone(timezone.utc)


def normalize_mql5_event(raw, *, fetched_at=None):
    raw = dict(raw or {})
    event_name = _value(raw, "name", "event", "event_name", "title")
    release_time = parse_mql5_timestamp(
        _value(raw, "date", "release_time", "time", "timestamp")
    )
    currency = str(_value(raw, "currency", "ccy") or "").upper().strip()
    provider_event_id = _value(raw, "event_id", "eventid", "id")
    return {
        "event_name": event_name,
        "event": event_name,
        "indicator": event_name,
        "currency": currency,
        "country": _value(raw, "country"),
        "impact": _value(raw, "impact"),
        "release_time": release_time,
        "actual": _value(raw, "actual"),
        "forecast": _value(raw, "forecast"),
        "previous": _value(raw, "previous"),
        "revised_previous": _value(raw, "revised_previous", "revisedprevious"),
        "provider": DATASET_ID,
        "source": DATASET_ID,
        "provider_event_id": provider_event_id,
        "provider_timestamp": parse_mql5_timestamp(
            _value(raw, "updated_at", "provider_timestamp")
        ),
        "fetched_at": fetched_at or datetime.now(timezone.utc),
        "data_status": "RELEASED" if _value(raw, "actual") is not None else "SCHEDULED",
        "provider_dataset": "mql5",
        "provider_source": "jblanked",
        "provider_timezone": "GMT+3",
        "raw": raw,
    }


def fetch_range(date_from, date_to, currencies, *, timeout=20, request_get=None):
    """Fetch one inclusive date range with exactly one provider request."""
    start = date.fromisoformat(str(date_from))
    end = date.fromisoformat(str(date_to))
    if start > end:
        raise ValueError("date_from must be on or before date_to")
    api_key = os.getenv("JBLANKED_API_KEY", "").strip()
    if not api_key or api_key == "PUT_YOUR_KEY_HERE":
        raise JBlankedAccessError("JBlanked API access is not configured")
    base = os.getenv("JBLANKED_NEWS_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    url = f"{base}{RANGE_PATH}"
    getter = request_get or requests.get
    try:
        response = getter(
            url,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Api-Key {api_key}",
            },
            params={"from": start.isoformat(), "to": end.isoformat()},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise JBlankedAccessError(f"JBlanked request failed: {type(exc).__name__}") from exc
    status = int(getattr(response, "status_code", 0) or 0)
    if status != 200:
        reason = {
            401: "authentication rejected",
            403: "API access or credits unavailable",
            429: "rate limit or credits unavailable",
        }.get(status, "provider request failed")
        raise JBlankedAccessError(f"JBlanked {reason} (HTTP {status})")
    try:
        payload = response.json()
    except ValueError as exc:
        raise JBlankedAccessError("JBlanked returned invalid JSON") from exc
    raw_events = _extract_events(payload)
    requested = {str(value).upper().strip() for value in currencies}
    filtered = [
        item for item in raw_events
        if str(_value(item, "currency", "ccy") or "").upper().strip() in requested
    ]
    fetched_at = datetime.now(timezone.utc)
    normalized = [normalize_mql5_event(item, fetched_at=fetched_at) for item in filtered]
    return {
        "provider": "jblanked",
        "dataset": "mql5",
        "provider_identity": DATASET_ID,
        "source": SOURCE_LABEL,
        "provider_timezone": "GMT+3",
        "normalized_timezone": "UTC",
        "request_count": 1,
        "estimated_credit_usage": 1,
        "raw_events": filtered,
        "normalized_events": normalized,
    }
