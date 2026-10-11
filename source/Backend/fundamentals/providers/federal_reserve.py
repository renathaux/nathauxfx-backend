"""Federal Reserve official FOMC statement adapter."""
from __future__ import annotations

import re
from datetime import date, datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

from fundamentals.providers.official_common import OfficialProviderError, get, official_event, strip_html


PROVIDER = "federal_reserve"
DATASET = "fomc_statements"
CALENDAR_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
EASTERN = ZoneInfo("America/New_York")


def _statement_links(html_text):
    links = []
    for href, label in re.findall(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', str(html_text), flags=re.I | re.S):
        if "monetary" not in href.lower() or not re.search(r"monetary\d{8}a\.htm(?:$|[?#])", href, flags=re.I):
            continue
        match = re.search(r"monetary(\d{8})a\.htm", href, flags=re.I)
        if match:
            links.append((datetime.strptime(match.group(1), "%Y%m%d").date(), urljoin(CALENDAR_URL, href), strip_html(label)))
    return links


def _target_rate(text):
    clean = strip_html(text).lower()
    # Some responses omit a charset and requests decodes UTF-8 hyphens as
    # Windows-1252 mojibake. Normalize both representations explicitly.
    clean = clean.replace("â\x80\x91", "-").replace("â\x80\x93", "-").replace("â\x80\x94", "-")
    for character in ("–", "—", "‑", "−"):
        clean = clean.replace(character, "-")
    number = r"[0-9]+(?:\.[0-9]+)?(?:-[0-9]+/[0-9]+)?"
    match = re.search(rf"target range for the federal funds rate (?:at|to)\s+({number})\s+to\s+({number})\s+percent", clean)
    if match:
        return f"{_rate_number(match.group(1))}-{_rate_number(match.group(2))}%"
    match = re.search(rf"federal funds rate at\s+({number})\s+percent", clean)
    return f"{_rate_number(match.group(1))}%" if match else None


def _rate_number(value):
    mixed = re.fullmatch(r"([0-9]+)-([0-9]+)/([0-9]+)", str(value))
    if mixed:
        number = int(mixed.group(1)) + int(mixed.group(2)) / int(mixed.group(3))
        return f"{number:g}"
    return str(value)


def _statement_release_time(html_text, decision_date):
    text = strip_html(html_text).replace("a.m.", "AM").replace("p.m.", "PM")
    match = re.search(r"For release at\s+(\d{1,2}:\d{2})\s*(AM|PM)\s*(?:E[DS]T)?", text, flags=re.I)
    if not match:
        return None
    parsed = datetime.strptime(f"{match.group(1)} {match.group(2)}", "%I:%M %p").time()
    return datetime.combine(decision_date, parsed, tzinfo=EASTERN)


def fetch_range(date_from, date_to, currencies=("USD",), *, timeout=20, request_get=None):
    start, end = date.fromisoformat(str(date_from)), date.fromisoformat(str(date_to))
    if "USD" not in {str(item).upper() for item in currencies}:
        return {"provider": PROVIDER, "request_count": 0, "normalized_events": []}
    response = get(CALENDAR_URL, timeout=timeout, request_get=request_get)
    if response.status_code != 200:
        raise OfficialProviderError(f"Federal Reserve calendar failed (HTTP {response.status_code})")
    events, requests = [], 1
    for decision_date, url, _label in _statement_links(response.text):
        if not start <= decision_date <= end:
            continue
        statement = get(url, timeout=timeout, request_get=request_get)
        requests += 1
        actual = _target_rate(statement.text)
        release = _statement_release_time(statement.text, decision_date)
        if actual is None or release is None:
            continue
        events.append(official_event(
            provider=PROVIDER, dataset=DATASET,
            provider_event_id=f"fed:fomc:{decision_date.isoformat()}",
            event_name="Federal Reserve Interest Rate Decision",
            indicator="fed_interest_rate", currency="USD", country="United States",
            release_time=release, actual=actual,
            raw={
                "statement_url": url,
                "decision_date": decision_date.isoformat(),
                "release_time_source": "FOMC statement 'For release at' line",
            },
        ))
    return {
        "provider": PROVIDER, "dataset": DATASET,
        "source": "Federal Reserve FOMC calendar and statements",
        "request_count": requests, "estimated_credit_usage": 0,
        "normalized_events": events, "forecast_available": False,
    }
