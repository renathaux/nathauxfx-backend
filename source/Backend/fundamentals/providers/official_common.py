"""Shared, trading-independent helpers for free official macro providers."""
from __future__ import annotations

import html
import re
from datetime import date, datetime, timezone

import requests


USER_AGENT = "FlowSignal-FundamentalEngine/1.0 (official macro data validation)"


class OfficialProviderError(RuntimeError):
    """A safe provider failure with optional non-secret request diagnostics."""

    def __init__(self, message, *, request_count=0, details=None):
        super().__init__(message)
        self.request_count = int(request_count or 0)
        self.details = details or {}


def safe_json(response, provider):
    status = int(getattr(response, "status_code", 0) or 0)
    if status != 200:
        raise OfficialProviderError(f"{provider} request failed (HTTP {status})")
    try:
        return response.json()
    except ValueError as exc:
        raise OfficialProviderError(f"{provider} returned invalid JSON") from exc


def strip_html(value):
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def utc(value):
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def in_window(timestamp, date_from, date_to):
    current = utc(timestamp).date()
    return date.fromisoformat(str(date_from)) <= current <= date.fromisoformat(str(date_to))


def official_event(
    *, provider, dataset, provider_event_id, event_name, indicator, currency,
    country, release_time, actual, previous=None, revised_previous=None,
    raw=None, data_status="RELEASED",
):
    return {
        "event_name": event_name,
        "indicator": indicator,
        "currency": currency,
        "country": country,
        "impact": "UNKNOWN",
        "release_time": utc(release_time),
        "actual": actual,
        "forecast": None,
        "previous": previous,
        "revised_previous": revised_previous,
        "provider": provider,
        "source": provider,
        "provider_dataset": dataset,
        "provider_event_id": provider_event_id,
        "provider_timestamp": utc(release_time),
        "data_status": data_status,
        "authority": {
            "actual": True,
            "revised_previous": True,
            "release_time": True,
            "forecast": False,
        },
        "raw": raw or {},
    }


def get(url, *, params=None, timeout=20, request_get=None, headers=None):
    getter = request_get or requests.get
    request_headers = {"User-Agent": USER_AGENT, "Accept": "application/json, text/html;q=0.9, */*;q=0.8"}
    request_headers.update(headers or {})
    try:
        return getter(url, params=params, headers=request_headers, timeout=timeout)
    except requests.RequestException as exc:
        raise OfficialProviderError(f"official request failed: {type(exc).__name__}") from exc
