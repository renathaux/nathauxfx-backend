"""US Bureau of Economic Analysis NIPA adapter (free API key required)."""
from __future__ import annotations

import os
import calendar
import re
from datetime import date, datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

from fundamentals.providers.official_common import (
    OfficialProviderError, get, official_event, safe_json, strip_html,
)


PROVIDER = "bea"
DATASET = "nipa"
API_URL = "https://apps.bea.gov/api/data/"
SCHEDULE_URL = "https://www.bea.gov/news/schedule"
ARCHIVE_URL = "https://www.bea.gov/news/archive"
BASE_URL = "https://www.bea.gov"
EASTERN = ZoneInfo("America/New_York")
TABLES = {
    "gdp": {"table": "T10101", "descriptions": ("gross domestic product",)},
    "pce": {"table": "T20804", "descriptions": ("personal consumption expenditures", "pce")},
    "core_pce": {"table": "T20804", "descriptions": ("excluding food and energy",)},
}


def _schedule_rows(html_text, year):
    """Parse only entries with a date and explicit time from BEA's schedule."""
    rows = []
    for block in re.findall(r"<(?:tr|article)\b[^>]*>(.*?)</(?:tr|article)>", str(html_text), flags=re.I | re.S):
        text = strip_html(block)
        date_match = re.search(
            r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})",
            text, flags=re.I,
        )
        time_match = re.search(r"(\d{1,2}:\d{2})\s*(AM|PM)", text, flags=re.I)
        if not date_match or not time_match:
            continue
        month = list(calendar.month_name).index(date_match.group(1).title())
        release = datetime.strptime(
            f"{year}-{month:02d}-{int(date_match.group(2)):02d} {time_match.group(1)} {time_match.group(2)}",
            "%Y-%m-%d %I:%M %p",
        ).replace(tzinfo=EASTERN)
        rows.append({"title": text, "release_time": release})
    return rows


def _archive_rows(html_text):
    """Parse BEA archive timestamps; these include the original UTC offset."""
    rows = []
    for block in re.findall(r"<tr\b[^>]*class=[\"'][^\"']*release-row[^\"']*[\"'][^>]*>(.*?)</tr>", str(html_text), flags=re.I | re.S):
        link = re.search(r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", block, flags=re.I | re.S)
        timestamp = re.search(r"<time\b[^>]*datetime=[\"']([^\"']+)[\"']", block, flags=re.I)
        if not link or not timestamp:
            continue
        try:
            release_time = datetime.fromisoformat(timestamp.group(1).replace("Z", "+00:00"))
        except ValueError:
            continue
        rows.append({
            "title": strip_html(link.group(2)),
            "release_time": release_time,
            "release_url": urljoin(BASE_URL, link.group(1)),
            "timestamp_source": "BEA news archive",
        })
    return rows


def _fetch_schedule(start, end, *, timeout=20, request_get=None):
    rows, request_count = [], 0
    for year in range(start.year, end.year + 1):
        response = get(
            SCHEDULE_URL, params={"year": year}, timeout=timeout, request_get=request_get,
            headers={"Accept": "text/html,application/xhtml+xml"},
        )
        request_count += 1
        if int(getattr(response, "status_code", 0) or 0) != 200:
            raise OfficialProviderError(
                f"BEA release schedule failed (HTTP {response.status_code})",
                request_count=request_count,
            )
        rows.extend(_schedule_rows(response.text, year))
    for query in ("GDP", "Personal Income and Outlays"):
        response = get(
            ARCHIVE_URL,
            params={
                "field_related_product_target_id": "All",
                "created_1": "All",
                "title": query,
            },
            timeout=timeout,
            request_get=request_get,
            headers={"Accept": "text/html,application/xhtml+xml"},
        )
        request_count += 1
        if int(getattr(response, "status_code", 0) or 0) == 200:
            rows.extend(_archive_rows(response.text))
    rows = [item for item in rows if start <= item["release_time"].date() <= end]
    unique = {(item["title"], item["release_time"]): item for item in rows}
    return list(unique.values()), request_count


def _reference_period(title):
    quarter = re.search(r"([1-4])(?:st|nd|rd|th)\s+Quarter(?:\s+and\s+Year)?\s+(20\d{2})", title, flags=re.I)
    if quarter:
        return f"{quarter.group(2)}Q{quarter.group(1)}"
    month = re.search(
        r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(20\d{2})",
        title, flags=re.I,
    )
    if month:
        number = list(calendar.month_name).index(month.group(1).title())
        return f"{month.group(2)}M{number:02d}"
    return None


def _previous_period(period):
    match = re.fullmatch(r"(\d{4})([QM])(\d{1,2})", str(period or ""))
    if not match:
        return None
    year, frequency, number = int(match.group(1)), match.group(2), int(match.group(3))
    if frequency == "M":
        return f"{year - 1}M12" if number == 1 else f"{year}M{number - 1:02d}"
    return f"{year - 1}Q4" if number == 1 else f"{year}Q{number - 1}"


def _event_indicators(title):
    lowered = title.lower()
    if "personal income and outlays" in lowered:
        return ("pce", "core_pce")
    if re.search(r"\bgdp\b", lowered) and "state gdp" not in lowered:
        return ("gdp",)
    return ()


def _signed_percent(action, value):
    number = float(value)
    return -number if str(action).lower() in {"decreased", "fell", "declined"} else number


def _release_actual(indicator, html_text):
    """Extract the originally published value, not today's revised API value."""
    text = strip_html(html_text)
    if indicator == "gdp":
        match = re.search(
            r"real gross domestic product\s*\(GDP\)\s*(increased|decreased) at an annual rate of\s*([0-9]+(?:\.[0-9]+)?)\s*percent",
            text, flags=re.I,
        )
        return _signed_percent(match.group(1), match.group(2)) if match else None
    if indicator == "pce":
        match = re.search(
            r"From the preceding month, the\s+PCE price index[^.]*?\s(increased|decreased)\s+([0-9]+(?:\.[0-9]+)?)\s+percent",
            text, flags=re.I,
        )
        return _signed_percent(match.group(1), match.group(2)) if match else None
    if indicator == "core_pce":
        match = re.search(
            r"(?:Excluding food and energy, the PCE price index|"
            r"The PCE price index excluding food and energy)\s+"
            r"(increased|decreased)\s+([0-9]+(?:\.[0-9]+)?)\s+percent",
            text, flags=re.I,
        )
        return _signed_percent(match.group(1), match.group(2)) if match else None
    return None


def _rows(payload):
    results = payload.get("BEAAPI", {}).get("Results", {})
    if results.get("Error"):
        raise OfficialProviderError("BEA rejected the NIPA request")
    return results.get("Data", []) or []


def _matches_indicator(indicator, description):
    normalized = re.sub(r"\s+", " ", str(description or "").strip().lower())
    if indicator == "gdp":
        return normalized == "gross domestic product"
    if indicator == "core_pce":
        return "personal consumption expenditures" in normalized and "excluding food and energy" in normalized
    if indicator == "pce":
        return (
            normalized in {"personal consumption expenditures", "personal consumption expenditures (pce)"}
            or normalized.startswith("personal consumption expenditures (pce),")
        ) and "excluding food and energy" not in normalized
    return False


def fetch_period_data(date_from, date_to, currencies=("USD",), *, timeout=20, request_get=None):
    """Return official values, emitting events only after schedule reconciliation."""
    if "USD" not in {str(item).upper() for item in currencies}:
        return {"provider": PROVIDER, "dataset": DATASET, "request_count": 0, "period_observations": []}
    key = os.getenv("BEA_API_KEY", "").strip()
    if not key:
        raise OfficialProviderError("BEA_API_KEY is not configured")
    start, end = date.fromisoformat(str(date_from)), date.fromisoformat(str(date_to))
    schedule, requests = _fetch_schedule(start, end, timeout=timeout, request_get=request_get)
    observations = []
    for indicator, config in TABLES.items():
        response = get(API_URL, params={
            "UserID": key, "method": "GetData", "datasetname": "NIPA",
            "TableName": config["table"], "Frequency": "M,Q,A",
            "Year": f"{start.year - 1},{start.year},{end.year}", "ResultFormat": "JSON",
        }, timeout=timeout, request_get=request_get)
        requests += 1
        for row in _rows(safe_json(response, "BEA")):
            description = str(row.get("LineDescription") or "").lower()
            if not _matches_indicator(indicator, description):
                continue
            observations.append({
                "provider": PROVIDER, "provider_dataset": config["table"],
                "provider_event_id": f"bea:{config['table']}:{row.get('LineNumber')}:{row.get('TimePeriod')}",
                "indicator": indicator, "currency": "USD", "country": "United States",
                "period": row.get("TimePeriod"), "actual": row.get("DataValue"),
                "unit": row.get("UNIT_MULT"), "line_description": row.get("LineDescription"),
                "release_time": None,
            })
    by_indicator_period = {
        (item["indicator"], str(item["period"])): item for item in observations
    }
    events = []
    for release in schedule:
        timestamp = release["release_time"]
        if not start <= timestamp.date() <= end:
            continue
        period = _reference_period(release["title"])
        if not period:
            continue
        release_page = None
        if release.get("release_url"):
            response = get(
                release["release_url"], timeout=timeout, request_get=request_get,
                headers={"Accept": "text/html,application/xhtml+xml"},
            )
            requests += 1
            if int(getattr(response, "status_code", 0) or 0) == 200:
                release_page = response.text
        for indicator in _event_indicators(release["title"]):
            item = by_indicator_period.get((indicator, period))
            if item is None:
                continue
            original_actual = _release_actual(indicator, release_page) if release_page else None
            if release.get("release_url") and original_actual is None:
                # Historical releases must never be populated with a later API
                # revision when the original archive value cannot be proved.
                continue
            previous_item = by_indicator_period.get((indicator, _previous_period(period)))
            events.append(official_event(
                provider=PROVIDER,
                dataset=item["provider_dataset"],
                provider_event_id=f"bea:{indicator}:{period}:{timestamp.date().isoformat()}",
                event_name={
                    "gdp": "US GDP q/q",
                    "pce": "US PCE Price Index",
                    "core_pce": "US Core PCE Price Index",
                }[indicator],
                indicator={"gdp": "gdp_q_q", "pce": "pce", "core_pce": "core_pce"}[indicator],
                currency="USD",
                country="United States",
                release_time=timestamp,
                actual=original_actual if original_actual is not None else item["actual"],
                # The archive actual is a release-period percentage while the
                # NIPA table value can be an index/level.  Leaving previous
                # unset lets evidence reconciliation derive it from the prior
                # comparable archived release instead of mixing units.
                previous=(
                    previous_item["actual"]
                    if original_actual is None and previous_item else None
                ),
                raw={
                    "reference_period": period,
                    "line_description": item.get("line_description"),
                    "release_schedule": release["title"],
                    "release_url": release.get("release_url"),
                    "release_actual_source": "BEA news archive" if original_actual is not None else "BEA NIPA API",
                    "latest_revised_period_value": item["actual"],
                },
            ))
    return {
        "provider": PROVIDER, "dataset": DATASET,
        "source": "BEA NIPA API", "request_count": requests,
        "estimated_credit_usage": 0, "normalized_events": events,
        "period_observations": observations, "forecast_available": False,
        "release_time_status": "official BEA release schedule, America/New_York",
        "revision_method": "append-only repeated API observations; historical release archives retain vintages",
    }


fetch_range = fetch_period_data
