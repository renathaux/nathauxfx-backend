"""European Central Bank official monetary-policy decision adapter."""
from __future__ import annotations

import re
from datetime import date, datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

from fundamentals.providers.official_common import OfficialProviderError, get, official_event, strip_html


PROVIDER = "ecb"
DATASET = "monetary_policy_decisions"
ARCHIVE_URL = "https://www.ecb.europa.eu/press/govcdec/mopo/{year}/html/index_include.en.html"
BASE_URL = "https://www.ecb.europa.eu"
EUROPE = ZoneInfo("Europe/Berlin")


def _decision_links(html_text):
    output = []
    for href in re.findall(r'href=["\']([^"\']*ecb\.mp\d{6}[^"\']*\.en\.html)["\']', str(html_text), flags=re.I):
        match = re.search(r"ecb\.mp(\d{6})", href, flags=re.I)
        if match:
            output.append((datetime.strptime(match.group(1), "%y%m%d").date(), urljoin(BASE_URL, href)))
    return list(dict.fromkeys(output))


def _rates(html_text):
    text = strip_html(html_text).lower().replace("per cent", "percent")
    result = {}
    combined = re.search(
        r"deposit facility,?\s+(?:the\s+)?main refinancing operations(?:\s+and|,)\s+"
        r"(?:the\s+)?marginal lending facility.{0,220}?"
        r"(?:to|at)\s+([0-9.]+)%?\s*,\s*([0-9.]+)%?\s+and\s+([0-9.]+)%?",
        text,
    )
    if combined:
        return {
            "deposit_facility": f"{combined.group(1)}%",
            "main_refinancing_operations": f"{combined.group(2)}%",
            "marginal_lending_facility": f"{combined.group(3)}%",
        }
    patterns = {
        "deposit_facility": r"deposit facility[^.]{0,180}?([0-9]+(?:\.[0-9]+)?)\s*percent",
        "main_refinancing_operations": r"main refinancing operations[^.]{0,180}?([0-9]+(?:\.[0-9]+)?)\s*percent",
        "marginal_lending_facility": r"marginal lending facility[^.]{0,180}?([0-9]+(?:\.[0-9]+)?)\s*percent",
    }
    for name, pattern in patterns.items():
        match = re.search(pattern, text)
        if match:
            result[name] = f"{match.group(1)}%"
    return result


def fetch_range(date_from, date_to, currencies=("EUR",), *, timeout=20, request_get=None):
    start, end = date.fromisoformat(str(date_from)), date.fromisoformat(str(date_to))
    if "EUR" not in {str(item).upper() for item in currencies}:
        return {"provider": PROVIDER, "request_count": 0, "normalized_events": []}
    events, requests, links = [], 0, []
    for year in range(start.year, end.year + 1):
        response = get(ARCHIVE_URL.format(year=year), timeout=timeout, request_get=request_get)
        requests += 1
        if response.status_code != 200:
            raise OfficialProviderError(f"ECB archive failed (HTTP {response.status_code})")
        links.extend(_decision_links(response.text))
    for decision_date, url in links:
        if not start <= decision_date <= end:
            continue
        statement = get(url, timeout=timeout, request_get=request_get)
        requests += 1
        rates = _rates(statement.text)
        if not rates:
            continue
        release = datetime.combine(decision_date, datetime.strptime("14:15", "%H:%M").time(), tzinfo=EUROPE)
        actual = rates.get("main_refinancing_operations") or rates.get("deposit_facility")
        events.append(official_event(
            provider=PROVIDER, dataset=DATASET,
            provider_event_id=f"ecb:decision:{decision_date.isoformat()}",
            event_name="ECB Interest Rate Decision", indicator="ecb_interest_rate",
            currency="EUR", country="Euro Area", release_time=release, actual=actual,
            raw={"statement_url": url, "rates": rates, "decision_date": decision_date.isoformat()},
        ))
    return {
        "provider": PROVIDER, "dataset": DATASET,
        "source": "ECB monetary-policy decision archive",
        "request_count": requests, "estimated_credit_usage": 0,
        "normalized_events": events, "forecast_available": False,
    }
