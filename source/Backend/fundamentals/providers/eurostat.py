"""Eurostat dissemination API adapter for euro-area macro series."""
from __future__ import annotations

import re
from datetime import date, datetime
from xml.etree import ElementTree

from fundamentals.providers.official_common import get, official_event, safe_json


PROVIDER = "eurostat"
BASE_URL = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data"
CALENDAR_ICS_URL = "https://ec.europa.eu/eurostat/o/calendars/eventsIcal"
ATOM_URL = "https://ec.europa.eu/eurostat/news/euro-indicators"
SERIES = {
    # HICP exposes the evolving euro-area composition under GEO=EA.
    "hicp_y_y": ("prc_hicp_manr", {"coicop": "CP00", "unit": "RCH_A"}, ("EA",)),
    "unemployment_rate": ("une_rt_m", {"s_adj": "SA", "sex": "T", "age": "TOTAL", "unit": "PC_ACT"}, ("EA20", "EA21")),
    "gdp_q_q": ("namq_10_gdp", {"s_adj": "SCA", "na_item": "B1GQ", "unit": "CLV_PCH_PRE"}, ("EA20", "EA21")),
    "employment_change_q_q": ("namq_10_pe", {"s_adj": "SCA", "na_item": "EMP_DC", "unit": "PCH_PRE"}, ("EA20", "EA21")),
}


def _time_values(payload):
    dimension = payload.get("dimension", {}).get("time", {}).get("category", {})
    index = dimension.get("index", {})
    if isinstance(index, list):
        positions = {name: number for number, name in enumerate(index)}
    else:
        positions = index
    values = payload.get("value", {})
    output = []
    for period, position in positions.items():
        value = values.get(str(position), values.get(position))
        if value is not None:
            output.append((period, value))
    return output


def _unfold_ics(text):
    return re.sub(r"\r?\n[ \t]", "", str(text or ""))


def _calendar_entries(ics_text):
    """Return official date-level metadata without inventing a release time."""
    entries = []
    for block in re.findall(r"BEGIN:VEVENT\s*(.*?)\s*END:VEVENT", _unfold_ics(ics_text), flags=re.S):
        fields = {}
        for line in block.splitlines():
            if ":" not in line:
                continue
            name, value = line.split(":", 1)
            fields[name.split(";", 1)[0].upper()] = value.strip().replace("\\,", ",")
        raw_date = fields.get("DTSTART", "")[:8]
        try:
            release_date = datetime.strptime(raw_date, "%Y%m%d").date()
        except ValueError:
            continue
        entries.append({
            "provider_event_id": fields.get("UID"),
            "release_date": release_date,
            "title": fields.get("SUMMARY", ""),
            "categories": fields.get("CATEGORIES", ""),
            "release_categories": fields.get("X-CATEGORY", ""),
            "timestamp_precision": "DATE_ONLY",
            "timezone": "Europe/Luxembourg",
        })
    return entries


def _fetch_calendar(start, end, *, timeout=20, request_get=None):
    response = get(
        CALENDAR_ICS_URL,
        params={"theme": "", "category": ""},
        timeout=timeout,
        request_get=request_get,
        headers={"Accept": "text/calendar,text/plain"},
    )
    entries = _calendar_entries(response.text)
    return [item for item in entries if start <= item["release_date"] <= end]


def _valid_geo_period(geo, period):
    year_match = re.match(r"(\d{4})", str(period))
    if not year_match:
        return False
    year = int(year_match.group(1))
    return geo == "EA" or (geo == "EA20" and year <= 2025) or (geo == "EA21" and year >= 2026)


def _atom_entries(xml_text):
    try:
        root = ElementTree.fromstring(str(xml_text or ""))
    except ElementTree.ParseError:
        return []
    namespace = {"a": "http://www.w3.org/2005/Atom"}
    entries = []
    for node in root.findall("a:entry", namespace):
        title = node.findtext("a:title", default="", namespaces=namespace)
        summary = node.findtext("a:summary", default="", namespaces=namespace)
        published_text = node.findtext("a:published", default="", namespaces=namespace)
        identifier = node.findtext("a:id", default="", namespaces=namespace)
        try:
            published = datetime.fromisoformat(published_text.replace("Z", "+00:00"))
        except ValueError:
            continue
        entries.append({
            "provider_event_id": identifier,
            "title": title,
            "summary": summary,
            "published": published,
        })
    return entries


def _fetch_atom(start, end, *, timeout=20, request_get=None, max_pages=20):
    entries, request_count = [], 0
    for page in range(1, max_pages + 1):
        response = get(
            ATOM_URL,
            params={
                "p_p_id": "estatsearchportlet_WAR_estatsearchportlet_INSTANCE_OaTpFrwlabNK",
                "p_p_lifecycle": "2",
                "p_p_state": "normal",
                "p_p_mode": "view",
                "p_p_resource_id": "atom",
                "p_p_cacheability": "cacheLevelPage",
                "_estatsearchportlet_WAR_estatsearchportlet_INSTANCE_OaTpFrwlabNK_pageNumber": str(page),
                "_estatsearchportlet_WAR_estatsearchportlet_INSTANCE_OaTpFrwlabNK_pageSize": "250",
                "_estatsearchportlet_WAR_estatsearchportlet_INSTANCE_OaTpFrwlabNK_sort": "lastUpdateDate",
                "_estatsearchportlet_WAR_estatsearchportlet_INSTANCE_OaTpFrwlabNK_collection": "CAT_PREREL",
                "pageNumber": str(page),
            },
            timeout=timeout,
            request_get=request_get,
            headers={"Accept": "application/atom+xml,application/xml"},
        )
        request_count += 1
        page_entries = _atom_entries(response.text)
        if not page_entries:
            break
        entries.extend(page_entries)
        if min(item["published"].date() for item in page_entries) <= start:
            break
    return [item for item in entries if start <= item["published"].date() <= end], request_count


def _calendar_indicators(title):
    normalized = re.sub(r"\s+", " ", str(title or "").strip().lower())
    if normalized == "inflation (hicp)":
        return ("hicp_y_y",)
    if normalized == "unemployment":
        return ("unemployment_rate",)
    if "gdp" in normalized:
        return ("gdp_q_q", "employment_change_q_q") if "employment" in normalized else ("gdp_q_q",)
    return ()


def _atom_matches(indicator, title):
    lowered = str(title or "").lower()
    if indicator == "hicp_y_y":
        return "inflation" in lowered
    if indicator == "unemployment_rate":
        return "unemployment" in lowered
    if indicator in {"gdp_q_q", "employment_change_q_q"}:
        return "gdp" in lowered
    return False


def _reference_period(text):
    clean = re.sub(r"<[^>]+>", " ", str(text or ""))
    month = re.search(
        r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(20\d{2})\b",
        clean, flags=re.I,
    )
    if month:
        number = datetime.strptime(month.group(1).title(), "%B").month
        return f"{month.group(2)}-{number:02d}"
    quarter = re.search(r"\b(first|second|third|fourth) quarter(?: of)?\s+(20\d{2})\b", clean, flags=re.I)
    if quarter:
        number = {"first": 1, "second": 2, "third": 3, "fourth": 4}[quarter.group(1).lower()]
        return f"{quarter.group(2)}-Q{number}"
    return None


def _previous_period(period):
    monthly = re.fullmatch(r"(\d{4})-(\d{2})", str(period or ""))
    if monthly:
        year, month = int(monthly.group(1)), int(monthly.group(2))
        return f"{year - 1}-12" if month == 1 else f"{year}-{month - 1:02d}"
    quarterly = re.fullmatch(r"(\d{4})-Q([1-4])", str(period or ""))
    if quarterly:
        year, quarter = int(quarterly.group(1)), int(quarterly.group(2))
        return f"{year - 1}-Q4" if quarter == 1 else f"{year}-Q{quarter - 1}"
    return None


def _signed_change(text, subject):
    match = re.search(
        rf"\b{subject}\b[^.]*?\b(increased|decreased|grew|fell|rose|declined|up|down)\s+(?:by\s+)?([0-9]+(?:\.[0-9]+)?)%",
        str(text or ""), flags=re.I,
    )
    if not match:
        return None
    negative = match.group(1).lower() in {"decreased", "fell", "declined", "down"}
    value = float(match.group(2))
    return -value if negative else value


def _release_actual(indicator, title, summary):
    text = f"{title}. {summary}"
    if indicator == "hicp_y_y":
        match = re.search(r"annual inflation rate was\s+([0-9]+(?:\.[0-9]+)?)%", text, flags=re.I)
        return float(match.group(1)) if match else None
    if indicator == "unemployment_rate":
        match = re.search(r"unemployment rate was\s+([0-9]+(?:\.[0-9]+)?)%", text, flags=re.I)
        return float(match.group(1)) if match else None
    if indicator == "gdp_q_q":
        return _signed_change(text, "GDP")
    if indicator == "employment_change_q_q":
        return _signed_change(text, "employment")
    return None


def _release_previous(indicator, summary, actual):
    text = str(summary or "")
    if indicator == "hicp_y_y":
        match = re.search(r"(?:up|down) from\s+([0-9]+(?:\.[0-9]+)?)%", text, flags=re.I)
        return float(match.group(1)) if match else None
    if indicator == "unemployment_rate" and re.search(r"stable compared with", text, flags=re.I):
        return actual
    return None


def fetch_period_data(date_from, date_to, currencies=("EUR",), *, timeout=20, request_get=None):
    """Return official period values; release calendar matching remains explicit."""
    if "EUR" not in {str(item).upper() for item in currencies}:
        return {"provider": PROVIDER, "request_count": 0, "period_observations": []}
    start, end = date.fromisoformat(str(date_from)), date.fromisoformat(str(date_to))
    calendar_entries = _fetch_calendar(start, end, timeout=timeout, request_get=request_get)
    atom_entries, atom_requests = _fetch_atom(start, end, timeout=timeout, request_get=request_get)
    observations, requests = [], 1 + atom_requests
    for indicator, (dataset, dimensions, geos) in SERIES.items():
        quarterly = dataset.startswith("namq_")
        for geo in geos:
            params = {
                **dimensions,
                "geo": geo,
                "lang": "en",
                "sinceTimePeriod": f"{start.year - 1}-Q1" if quarterly else f"{start.year - 1}-01",
                "untilTimePeriod": f"{end.year}-Q4" if quarterly else f"{end.year}-12",
            }
            response = get(f"{BASE_URL}/{dataset}", params=params, timeout=timeout, request_get=request_get)
            payload = safe_json(response, "Eurostat")
            requests += 1
            for period, value in _time_values(payload):
                if not _valid_geo_period(geo, period):
                    continue
                observations.append({
                    "provider": PROVIDER, "provider_dataset": dataset,
                    "provider_event_id": f"eurostat:{dataset}:{geo}:{indicator}:{period}",
                    "indicator": indicator, "currency": "EUR", "country": "Euro Area",
                    "period": period, "actual": value, "release_time": None,
                    "updated": payload.get("updated"), "geo": geo,
                })
    relevant_calendar = []
    for entry in calendar_entries:
        matches = list(_calendar_indicators(entry["title"]))
        if matches:
            relevant_calendar.append({**entry, "candidate_indicators": matches})
    observations_by_key = {(item["indicator"], item["period"]): item for item in observations}
    events = []
    for calendar_entry in relevant_calendar:
        releases = [
            item for item in atom_entries
            if item["published"].date() == calendar_entry["release_date"]
        ]
        for indicator in calendar_entry["candidate_indicators"]:
            atom = next((item for item in releases if _atom_matches(indicator, item["title"])), None)
            if atom is None:
                continue
            period = _reference_period(atom["summary"])
            observation = observations_by_key.get((indicator, period))
            release_actual = _release_actual(indicator, atom["title"], atom["summary"])
            if release_actual is None:
                continue
            previous = observations_by_key.get((indicator, _previous_period(period)))
            release_previous = _release_previous(indicator, atom["summary"], release_actual)
            if release_previous is None and previous is not None:
                release_previous = previous["actual"]
            events.append(official_event(
                provider=PROVIDER,
                dataset="euro_indicators_atom",
                provider_event_id=atom["provider_event_id"],
                event_name={
                    "hicp_y_y": "Euro Area HICP y/y",
                    "unemployment_rate": "Euro Area Unemployment Rate",
                    "gdp_q_q": "Euro Area GDP q/q",
                    "employment_change_q_q": "Euro Area Employment Change q/q",
                }[indicator],
                indicator=indicator,
                currency="EUR",
                country="Euro Area",
                release_time=atom["published"],
                actual=release_actual,
                previous=release_previous,
                raw={
                    "reference_period": period,
                    "geo": observation.get("geo") if observation else None,
                    "calendar_uid": calendar_entry["provider_event_id"],
                    "release_title": atom["title"],
                    "release_actual_source": "Eurostat publication text",
                    "latest_revised_period_value": observation.get("actual") if observation else None,
                    "latest_previous_period_value": previous.get("actual") if previous else None,
                },
            ))
    return {
        "provider": PROVIDER, "dataset": "dissemination_api",
        "source": "Eurostat dissemination API", "request_count": requests,
        "estimated_credit_usage": 0, "normalized_events": events,
        "period_observations": observations, "forecast_available": False,
        "calendar_entries": relevant_calendar,
        "calendar_event_count": len(relevant_calendar),
        "release_time_status": (
            "official calendar date plus exact Eurostat Atom publication timestamp; "
            "unmatched date-only entries are not emitted"
        ),
        "aggregate_transition": "evolving EA for HICP; EA20 through 2025 and EA21 from 2026 for labour/GDP",
    }


fetch_range = fetch_period_data
