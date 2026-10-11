"""US Bureau of Labor Statistics historical macro adapter."""
from __future__ import annotations

import calendar
import os
import re
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

from fundamentals.providers.official_common import (
    OfficialProviderError, USER_AGENT, official_event, safe_json, strip_html,
)


PROVIDER = "bls"
DATASET = "public_data_api_v2"
API_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
SCHEDULE_YEAR_URL = "https://www.bls.gov/schedule/{year}/"
SCHEDULE_MONTH_URL = "https://www.bls.gov/schedule/{year}/{month_name}_sched.htm"
EASTERN = ZoneInfo("America/New_York")
SERIES = {
    "cpi": "CUSR0000SA0",
    "core_cpi": "CUSR0000SA0L1E",
    "ppi": "WPSFD4",
    "nonfarm_payrolls": "CES0000000001",
    "unemployment_rate": "LNS14000000",
    "average_hourly_earnings": "CES0500000003",
}
RELEASE_TITLES = {
    "cpi": "Consumer Price Index",
    "core_cpi": "Consumer Price Index",
    "ppi": "Producer Price Index",
    "nonfarm_payrolls": "Employment Situation",
    "unemployment_rate": "Employment Situation",
    "average_hourly_earnings": "Employment Situation",
}


def _post_series(start_year, end_year, *, timeout=20, request_post=None, sleep=None, max_attempts=3):
    poster = request_post or requests.post
    payload = {
        "seriesid": list(SERIES.values()),
        "startyear": str(start_year),
        "endyear": str(end_year),
        "catalog": False,
        "calculations": False,
        "annualaverage": False,
    }
    registration_key = os.getenv("BLS_API_KEY", "").strip()
    if registration_key:
        payload["registrationkey"] = registration_key
    sleeper = sleep or time.sleep
    response = None
    for attempt in range(max_attempts):
        try:
            response = poster(
                API_URL, json=payload,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                timeout=timeout,
            )
        except requests.RequestException as exc:
            if attempt + 1 >= max_attempts:
                raise OfficialProviderError(
                    f"BLS API request failed: {type(exc).__name__}", request_count=attempt + 1
                ) from exc
            sleeper(min(2 ** attempt, 4))
            continue
        status = int(getattr(response, "status_code", 0) or 0)
        if status not in {429, 500, 502, 503, 504} or attempt + 1 >= max_attempts:
            break
        retry_after = getattr(response, "headers", {}).get("Retry-After") if hasattr(response, "headers") else None
        try:
            delay = min(float(retry_after), 10) if retry_after else min(2 ** attempt, 4)
        except (TypeError, ValueError):
            delay = min(2 ** attempt, 4)
        sleeper(delay)
    try:
        body = safe_json(response, "BLS")
    except OfficialProviderError as exc:
        raise OfficialProviderError(f"BLS API unavailable: {exc}", request_count=max_attempts) from exc
    if str(body.get("status", "")).upper() != "REQUEST_SUCCEEDED":
        messages = [str(item) for item in body.get("message", []) if item]
        suffix = f": {'; '.join(messages)}" if messages else ""
        raise OfficialProviderError(f"BLS API rejected the series request{suffix}", request_count=1)
    return body


def _schedule_rows(html_text, year):
    rows = []
    for block in re.findall(r"<tr\b[^>]*>(.*?)</tr>", str(html_text), flags=re.I | re.S):
        cells = [strip_html(item) for item in re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", block, flags=re.I | re.S)]
        joined = " | ".join(cells)
        date_match = re.search(
            r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})",
            joined, flags=re.I,
        )
        time_match = re.search(r"(\d{1,2}:\d{2})\s*(AM|PM)", joined, flags=re.I)
        if not date_match or not time_match:
            continue
        month = list(calendar.month_name).index(date_match.group(1).title())
        local = datetime.strptime(
            f"{year}-{month:02d}-{int(date_match.group(2)):02d} {time_match.group(1)} {time_match.group(2)}",
            "%Y-%m-%d %I:%M %p",
        ).replace(tzinfo=EASTERN)
        rows.append({"text": joined, "release_time": local})
    return rows


def _fetch_schedule(start, end, *, timeout=20, request_get=None):
    getter = request_get or requests.get
    rows = []
    requests_made = 0
    for year in range(start.year, end.year + 1):
        url = SCHEDULE_YEAR_URL.format(year=year)
        try:
            response = getter(
                url,
                headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
                timeout=timeout,
            )
            requests_made += 1
        except requests.RequestException as exc:
            raise OfficialProviderError(
                f"BLS release calendar failed: {type(exc).__name__}", request_count=requests_made + 1
            ) from exc
        if int(getattr(response, "status_code", 0) or 0) != 200:
            # The documented calendar has historically exposed both yearly and
            # monthly views. Use the latter only as an official-site fallback.
            for month in range(1, 13):
                month_start = date(year, month, 1)
                month_end = date(year, month, calendar.monthrange(year, month)[1])
                if month_end < start or month_start > end:
                    continue
                month_url = SCHEDULE_MONTH_URL.format(
                    year=year, month_name=calendar.month_name[month].lower()
                )
                try:
                    month_response = getter(
                        month_url,
                        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
                        timeout=timeout,
                    )
                    requests_made += 1
                except requests.RequestException:
                    continue
                if int(getattr(month_response, "status_code", 0) or 0) == 200:
                    rows.extend(_schedule_rows(month_response.text, year))
            if not rows:
                raise OfficialProviderError(
                    f"BLS release calendar blocked the runtime (HTTP {response.status_code})",
                    request_count=requests_made,
                )
            continue
        rows.extend(_schedule_rows(response.text, year))
    unique = {(row["text"], row["release_time"]): row for row in rows}
    return list(unique.values()), requests_made


def _series_values(payload):
    output = {}
    for series in payload.get("Results", {}).get("series", []):
        values = {}
        for item in series.get("data", []):
            period = str(item.get("period", ""))
            if not period.startswith("M") or period == "M13":
                continue
            key = (int(item["year"]), int(period[1:]))
            try:
                values[key] = float(str(item.get("value", "")).replace(",", ""))
            except (TypeError, ValueError):
                continue
        output[series.get("seriesID")] = values
    return output


def _prior_month(release_time):
    first = release_time.date().replace(day=1)
    prior = first - timedelta(days=1)
    return prior.year, prior.month


def _pct(current, previous):
    return None if current is None or previous in (None, 0) else round((current / previous - 1) * 100, 3)


def _derived_actual(indicator, values, period):
    current = values.get(period)
    year, month = period
    previous_month_date = date(year, month, 1) - timedelta(days=1)
    prior = values.get((previous_month_date.year, previous_month_date.month))
    year_ago = values.get((year - 1, month))
    if indicator in {"cpi", "core_cpi", "ppi"}:
        return _pct(current, year_ago), _pct(
            prior, values.get((previous_month_date.year - 1, previous_month_date.month))
        )
    if indicator == "nonfarm_payrolls":
        prior_month_date = previous_month_date.replace(day=1) - timedelta(days=1)
        before_prior = values.get((prior_month_date.year, prior_month_date.month))
        actual = None if current is None or prior is None else round(current - prior, 1)
        previous = None if prior is None or before_prior is None else round(prior - before_prior, 1)
        return actual, previous
    if indicator == "average_hourly_earnings":
        prior_month_date = previous_month_date.replace(day=1) - timedelta(days=1)
        return _pct(current, prior), _pct(prior, values.get((prior_month_date.year, prior_month_date.month)))
    return current, prior


def fetch_range(
    date_from, date_to, currencies=("USD",), *, timeout=20,
    request_post=None, request_get=None, sleep=None,
):
    start, end = date.fromisoformat(str(date_from)), date.fromisoformat(str(date_to))
    if "USD" not in {str(item).upper() for item in currencies}:
        return {"provider": PROVIDER, "dataset": DATASET, "request_count": 0, "normalized_events": []}
    failures = []
    schedule, schedule_requests = [], 0
    series_payload, api_requests = None, 0
    try:
        schedule, schedule_requests = _fetch_schedule(start, end, timeout=timeout, request_get=request_get)
    except OfficialProviderError as exc:
        schedule_requests = exc.request_count
        failures.append(str(exc))
    try:
        series_payload = _post_series(
            start.year - 2, end.year, timeout=timeout, request_post=request_post, sleep=sleep
        )
        api_requests = 1
    except OfficialProviderError as exc:
        api_requests = exc.request_count
        failures.append(str(exc))
    if failures:
        raise OfficialProviderError(
            "BLS validation unavailable; " + " | ".join(failures),
            request_count=schedule_requests + api_requests,
            details={"calendar_ready": bool(schedule), "series_api_ready": series_payload is not None},
        )
    values_by_series = _series_values(series_payload)
    events = []
    for indicator, title in RELEASE_TITLES.items():
        for release in schedule:
            if title.lower() not in release["text"].lower():
                continue
            timestamp = release["release_time"]
            if not start <= timestamp.date() <= end:
                continue
            period = _prior_month(timestamp)
            actual, previous = _derived_actual(indicator, values_by_series.get(SERIES[indicator], {}), period)
            if actual is None:
                continue
            suffix = "_y_y" if indicator in {"cpi", "core_cpi", "ppi"} else ("_m_m" if indicator == "average_hourly_earnings" else "")
            events.append(official_event(
                provider=PROVIDER, dataset=DATASET,
                provider_event_id=f"bls:{SERIES[indicator]}:{period[0]}:M{period[1]:02d}",
                event_name={
                    "cpi": "US CPI y/y", "core_cpi": "US Core CPI y/y", "ppi": "US PPI y/y",
                    "nonfarm_payrolls": "US Non-Farm Payrolls", "unemployment_rate": "US Unemployment Rate",
                    "average_hourly_earnings": "US Average Hourly Earnings m/m",
                }[indicator],
                indicator=f"{indicator}{suffix}", currency="USD", country="United States",
                release_time=timestamp, actual=actual, previous=previous,
                raw={"series_id": SERIES[indicator], "reference_period": f"{period[0]}-M{period[1]:02d}", "schedule": release["text"]},
            ))
    return {
        "provider": PROVIDER, "dataset": DATASET, "provider_identity": PROVIDER,
        "source": "BLS Public Data API and official release calendar",
        "request_count": schedule_requests + api_requests,
        "estimated_credit_usage": 0, "normalized_events": events,
        "forecast_available": False, "revision_method": "append-only repeated official observations",
        "rate_limit_policy": "registered 500/day or unregistered 25/day; max 50 requests/10 seconds",
        "release_time_status": "official BLS release calendar, America/New_York",
    }
