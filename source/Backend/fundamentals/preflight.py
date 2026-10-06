"""Read-only historical provider preflight. This module never imports the DB."""

from __future__ import annotations

import argparse
import json
from datetime import date

from fundamentals.provider_audit import analyze_events
from fundamentals.providers.jblanked import JBlankedAccessError, fetch_range


def parse_currencies(value):
    currencies = tuple(dict.fromkeys(
        part.strip().upper() for part in str(value).split(",") if part.strip()
    ))
    if not currencies or any(len(item) != 3 for item in currencies):
        raise ValueError("currencies must be comma-separated three-letter codes")
    return currencies


def run_preflight(*, provider, date_from, date_to, currencies, fetcher=None):
    start = date.fromisoformat(str(date_from))
    end = date.fromisoformat(str(date_to))
    if start > end:
        raise ValueError("--from must be on or before --to")
    if str(provider).lower() != "jblanked":
        raise ValueError("only provider 'jblanked' is supported")
    currency_list = parse_currencies(currencies) if isinstance(currencies, str) else tuple(currencies)
    result = (fetcher or fetch_range)(start, end, currency_list)
    audit = analyze_events(
        result["normalized_events"], provider_identity=result["provider_identity"]
    )
    audit.pop("canonical_events", None)
    return {
        "status": "OK",
        "read_only": True,
        "database_writes": 0,
        "trading_actions": 0,
        "provider": result["provider"],
        "dataset": result["dataset"],
        "provider_identity": result["provider_identity"],
        "source": result["source"],
        "date_from": start.isoformat(),
        "date_to": end.isoformat(),
        "currencies": list(currency_list),
        "request_count": result["request_count"],
        "estimated_credit_usage": result["estimated_credit_usage"],
        "provider_timezone": result["provider_timezone"],
        "normalized_timezone": result["normalized_timezone"],
        **audit,
    }


def build_parser():
    parser = argparse.ArgumentParser(description="Read-only Fundamental Engine provider preflight")
    parser.add_argument("--provider", required=True, choices=("jblanked",))
    parser.add_argument("--from", dest="date_from", required=True)
    parser.add_argument("--to", dest="date_to", required=True)
    parser.add_argument("--currencies", default="EUR,USD")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        report = run_preflight(
            provider=args.provider,
            date_from=args.date_from,
            date_to=args.date_to,
            currencies=args.currencies,
        )
    except (JBlankedAccessError, ValueError) as exc:
        print(json.dumps({
            "status": "FAILED",
            "read_only": True,
            "database_writes": 0,
            "error": str(exc),
        }, indent=2))
        return 2
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

