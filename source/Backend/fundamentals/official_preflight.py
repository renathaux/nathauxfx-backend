"""Read-only validation of free official macro sources; never imports the DB."""
from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import date

from fundamentals.normalization.indicators import normalize_indicator
from fundamentals.preflight import parse_currencies
from fundamentals.providers import bea, bls, ecb, eurostat, federal_reserve
from fundamentals.providers.official_common import OfficialProviderError


PROVIDERS = (bls, bea, federal_reserve, eurostat, ecb)
EXPECTED = {
    "us_cpi": {"cpi_y_y"},
    "us_core_cpi": {"core_cpi_y_y"},
    "us_ppi": {"ppi_y_y"},
    "ecb_decision": {"ecb_interest_rate"},
}


def _audit(events):
    indicators = [normalize_indicator(item.get("indicator") or item.get("event_name")) for item in events]
    return {
        "event_count": len(events),
        "eur_events": sum(str(item.get("currency")).upper() == "EUR" for item in events),
        "usd_events": sum(str(item.get("currency")).upper() == "USD" for item in events),
        "actual_count": sum(item.get("actual") not in (None, "") for item in events),
        "forecast_count": sum(item.get("forecast") not in (None, "") for item in events),
        "previous_count": sum(item.get("previous") not in (None, "") for item in events),
        "revision_count": sum(item.get("revised_previous") not in (None, "") for item in events),
        "timestamp_count": sum(item.get("release_time") is not None for item in events),
        "stable_id_count": sum(item.get("provider_event_id") not in (None, "") for item in events),
        "normalized_indicators": dict(sorted(Counter(indicators).items())),
    }


def run_official_preflight(*, date_from, date_to, currencies="EUR,USD", fetchers=None):
    start, end = date.fromisoformat(str(date_from)), date.fromisoformat(str(date_to))
    if start > end:
        raise ValueError("--from must be on or before --to")
    currency_list = parse_currencies(currencies) if isinstance(currencies, str) else tuple(currencies)
    configured = fetchers or {provider.PROVIDER: provider.fetch_range for provider in PROVIDERS}
    provider_reports, all_events, requests = {}, [], 0
    for provider in PROVIDERS:
        name = provider.PROVIDER
        fetcher = configured.get(name)
        if fetcher is None:
            continue
        try:
            result = fetcher(start, end, currency_list)
            events = list(result.get("normalized_events") or [])
            all_events.extend(events)
            requests += int(result.get("request_count") or 0)
            provider_reports[name] = {
                "status": "OK", "source": result.get("source"),
                "request_count": int(result.get("request_count") or 0),
                "forecast_available": bool(result.get("forecast_available")),
                "release_time_status": result.get("release_time_status", "official"),
                "period_observation_count": len(result.get("period_observations") or []),
                "calendar_event_count": int(result.get("calendar_event_count") or 0),
                "calendar_entries": result.get("calendar_entries") or [],
                "revision_method": result.get("revision_method"),
                **_audit(events),
            }
        except Exception as exc:
            failed_requests = int(getattr(exc, "request_count", 0) or 0)
            requests += failed_requests
            provider_reports[name] = {
                "status": "UNAVAILABLE", "error": str(exc),
                "request_count": failed_requests,
                "details": getattr(exc, "details", {}) or {},
            }
    audit = _audit(all_events)
    present = set(audit["normalized_indicators"])
    expected = {name: bool(values & present) for name, values in EXPECTED.items()}
    return {
        "status": "OK",
        "read_only": True,
        "database_writes": 0,
        "trading_actions": 0,
        "date_from": start.isoformat(), "date_to": end.isoformat(),
        "currencies": list(currency_list),
        "request_count": requests,
        "estimated_credit_usage": 0,
        "timezone": "all release timestamps normalized to UTC",
        "official_sources_provide_consensus_forecasts": False,
        "providers": provider_reports,
        "expected_event_presence": expected,
        "missing_expected_events": [name for name, found in expected.items() if not found],
        **audit,
    }


def build_parser():
    parser = argparse.ArgumentParser(description="Read-only official macro source validation")
    parser.add_argument("--from", dest="date_from", required=True)
    parser.add_argument("--to", dest="date_to", required=True)
    parser.add_argument("--currencies", default="EUR,USD")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        report = run_official_preflight(
            date_from=args.date_from, date_to=args.date_to, currencies=args.currencies
        )
    except (OfficialProviderError, ValueError) as exc:
        print(json.dumps({"status": "FAILED", "read_only": True, "database_writes": 0, "error": str(exc)}, indent=2))
        return 2
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
