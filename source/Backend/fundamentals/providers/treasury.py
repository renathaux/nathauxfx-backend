"""Official U.S. Treasury daily 10-year nominal and real-yield adapter."""
from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from datetime import date, datetime, time, timezone

from fundamentals.market_calendar import missing_market_dates
from fundamentals.providers.official_common import OfficialProviderError, get, official_event


PROVIDER = "treasury"
BASE_URL = "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml"
SERIES = {
    "us_10y_treasury_yield": {
        "dataset": "daily_treasury_yield_curve",
        "field": "BC_10YEAR",
        "name": "US 10-Year Nominal Treasury Yield",
        "available_from": 1990,
    },
    "us_10y_real_yield": {
        "dataset": "daily_treasury_real_yield_curve",
        "field": "TC_10YEAR",
        "name": "US 10-Year Real Yield",
        "available_from": 2003,
    },
}
ATOM = "http://www.w3.org/2005/Atom"
DATA = "http://schemas.microsoft.com/ado/2007/08/dataservices"
METADATA = "http://schemas.microsoft.com/ado/2007/08/dataservices/metadata"


def source_url(dataset, year):
    return f"{BASE_URL}?data={dataset}&field_tdr_date_value={int(year)}"


def parse_yield_xml(xml_text, *, indicator, source_identity):
    config = SERIES[indicator]
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise OfficialProviderError("Treasury returned invalid XML") from exc
    records = []
    for entry in root.findall(f"{{{ATOM}}}entry"):
        properties = entry.find(f"{{{ATOM}}}content/{{{METADATA}}}properties")
        if properties is None:
            continue
        date_node = properties.find(f"{{{DATA}}}NEW_DATE")
        value_node = properties.find(f"{{{DATA}}}{config['field']}")
        if date_node is None or value_node is None or value_node.text in (None, ""):
            continue
        try:
            observation_date = datetime.fromisoformat(date_node.text.replace("Z", "+00:00")).date()
            value = float(value_node.text)
        except (TypeError, ValueError) as exc:
            raise OfficialProviderError("Treasury XML contains an invalid date or 10Y value") from exc
        records.append({
            "indicator": indicator,
            "observation_date": observation_date,
            "value": value,
            "source_url": source_identity,
            "content_hash": hashlib.sha256(
                f"{indicator}|{observation_date.isoformat()}|{value:g}|{source_identity}".encode()
            ).hexdigest(),
        })
    records.sort(key=lambda item: item["observation_date"])
    return records


def _fetch_dataset(dataset, year, *, timeout=20, request_get=None):
    url = source_url(dataset, year)
    response = get(
        url, timeout=timeout, request_get=request_get,
        headers={"Accept": "application/atom+xml, application/xml;q=0.9"},
    )
    if response.status_code != 200:
        raise OfficialProviderError(f"Treasury {dataset} failed (HTTP {response.status_code})")
    return response.text, url


def fetch_range(date_from, date_to, currencies=("USD",), *, timeout=20, request_get=None):
    start, end = date.fromisoformat(str(date_from)), date.fromisoformat(str(date_to))
    if start > end:
        raise ValueError("date_from must not be later than date_to")
    if "USD" not in {str(item).upper() for item in currencies}:
        return {"provider": PROVIDER, "request_count": 0, "normalized_events": []}

    fetched_records = {indicator: [] for indicator in SERIES}
    request_count = 0
    for year in range(start.year, end.year + 1):
        for indicator, config in SERIES.items():
            if year < config["available_from"]:
                continue
            xml_text, url = _fetch_dataset(
                config["dataset"], year, timeout=timeout, request_get=request_get
            )
            request_count += 1
            fetched_records[indicator].extend(
                parse_yield_xml(xml_text, indicator=indicator, source_identity=url)
            )

    events = []
    missing = {}
    for indicator, records in fetched_records.items():
        all_records = sorted(records, key=lambda item: item["observation_date"])
        prior_value = None
        selected_dates = []
        for record in all_records:
            observation_date = record["observation_date"]
            if start <= observation_date <= end:
                selected_dates.append(observation_date)
                observation_timestamp = datetime.combine(observation_date, time.min, tzinfo=timezone.utc)
                config = SERIES[indicator]
                events.append(official_event(
                    provider=PROVIDER,
                    dataset=config["dataset"],
                    provider_event_id=f"treasury:{config['dataset']}:10y:{observation_date.isoformat()}",
                    event_name=config["name"],
                    indicator=indicator,
                    currency="USD",
                    country="United States",
                    release_time=observation_timestamp,
                    actual=f"{record['value']:.2f}",
                    previous=None if prior_value is None else f"{prior_value:.2f}",
                    data_status="RELEASED",
                    raw={
                        "observation_date": observation_date.isoformat(),
                        "timestamp_precision": "DATE_ONLY",
                        "source_url": record["source_url"],
                        "source_identity": f"US_TREASURY:{config['dataset']}:{config['field']}",
                        "series_field": config["field"],
                        "content_hash": record["content_hash"],
                        "revision_policy": "Treasury feed exposes latest official daily value; changed values persist append-only",
                    },
                ))
            prior_value = record["value"]
        completed_through = min(end, max(selected_dates)) if selected_dates else start
        missing[indicator] = [
            item.isoformat() for item in missing_market_dates(start, completed_through, selected_dates)
        ]
    events.sort(key=lambda item: (item["release_time"], item["indicator"]))
    return {
        "provider": PROVIDER,
        "dataset": "daily_treasury_10y_nominal_and_real_yields",
        "source": "U.S. Department of the Treasury Daily Interest Rate XML Feed",
        "request_count": request_count,
        "estimated_credit_usage": 0,
        "normalized_events": events,
        "missing_market_dates": missing,
        "historical_coverage": {
            "us_10y_treasury_yield": "1990-present",
            "us_10y_real_yield": "2003-present",
        },
        "timestamp_precision": "DATE_ONLY",
        "latest_observation_dates": {
            indicator: max(
                (event["raw"]["observation_date"] for event in events if event["indicator"] == indicator),
                default=None,
            )
            for indicator in SERIES
        },
        "revision_support": "append-only when an official daily value changes",
    }
