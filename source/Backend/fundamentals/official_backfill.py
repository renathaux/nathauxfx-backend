"""Resumable, trading-isolated historical backfill for official macro sources.

HTTP is completed before opening the per-chunk database transaction.  The
module imports no strategy, broker, order-management, News Mode, or LIVE/PAPER
control code.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from db import SessionLocal, engine
from fundamentals.locks import HISTORICAL_BACKFILL_LOCK_KEY, LIVE_INGESTION_LOCK_KEY, advisory_lock
from fundamentals.preflight import parse_currencies
from fundamentals.providers import bea, ecb, eurostat, federal_reserve, treasury
from fundamentals.providers import bls as bls_provider
from fundamentals.providers.official_common import official_event
from fundamentals.repositories.economic_events import persist_calendar_batch_in_session, preview_calendar_batch
from models import EconomicBackfillJob


PROVIDERS = {
    "bea": bea.fetch_range,
    "eurostat": eurostat.fetch_range,
    "federal_reserve": federal_reserve.fetch_range,
    "ecb": ecb.fetch_range,
    "treasury": treasury.fetch_range,
}
DEFAULT_MANIFEST = Path(__file__).with_name("bls_timestamp_manifest_12m.json")


def _utc_midnight(value):
    parsed = value if isinstance(value, date) else date.fromisoformat(str(value))
    return datetime.combine(parsed, datetime_time.min, tzinfo=timezone.utc)


def _as_date(value):
    return value.date() if isinstance(value, datetime) else value


def _job_id(provider, start, end, chunk_days, currencies):
    basis = f"{provider}|{start}|{end}|{chunk_days}|{','.join(sorted(currencies))}"
    return hashlib.sha256(basis.encode()).hexdigest()[:32]


def _validate_manifest(path):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema_version") != "1.0":
        raise ValueError("unsupported BLS manifest schema_version")
    rows = list(payload.get("manifests") or [])
    required = {
        "release_family", "reference_period", "release_date", "release_time",
        "timezone", "release_timestamp_utc", "official_url", "content_hash",
        "stable_manifest_id", "source",
    }
    identities, timestamps = {}, {}
    for row in rows:
        missing = required - set(row)
        if missing:
            raise ValueError(f"BLS manifest row missing: {', '.join(sorted(missing))}")
        expected = f"bls_manifest:{row['release_family']}:{row['reference_period']}"
        if row["stable_manifest_id"] != expected:
            raise ValueError(f"invalid BLS stable manifest ID: {row['stable_manifest_id']}")
        if row["source"] != "BLS" or not str(row["official_url"]).startswith("https://www.bls.gov/news.release/archives/"):
            raise ValueError("BLS manifest contains a non-official source")
        if len(str(row["content_hash"])) != 64 or any(c not in "0123456789abcdef" for c in str(row["content_hash"]).lower()):
            raise ValueError(f"invalid content hash for {expected}")
        published_utc = datetime.fromisoformat(
            str(row["release_timestamp_utc"]).replace("Z", "+00:00")
        ).astimezone(timezone.utc)
        published_local = datetime.fromisoformat(
            f"{row['release_date']}T{row['release_time']}"
        ).replace(tzinfo=ZoneInfo(str(row["timezone"])))
        if published_local.astimezone(timezone.utc) != published_utc:
            raise ValueError(f"BLS timezone conversion conflict: {expected}")
        prior = identities.get(expected)
        if prior and prior != row:
            raise ValueError(f"BLS manifest identity conflict: {expected}")
        key = (row["release_family"], row["reference_period"])
        timestamp = row["release_timestamp_utc"]
        if key in timestamps and timestamps[key] != timestamp:
            raise ValueError(f"BLS timestamp conflict: {expected}")
        identities[expected] = row
        timestamps[key] = timestamp
    if not rows:
        raise ValueError("BLS manifest is empty")
    return rows


def _bls_status(series_payload, series_id, period):
    year, month = period
    for series in series_payload.get("Results", {}).get("series", []):
        if series.get("seriesID") != series_id:
            continue
        for item in series.get("data", []):
            if str(item.get("year")) != str(year) or str(item.get("period")) != f"M{month:02d}":
                continue
            notes = " ".join(str(x.get("text") or "") for x in item.get("footnotes") or [])
            lowered = notes.lower()
            if "corrected" in lowered:
                return "CORRECTED", notes
            if "preliminary" in lowered:
                return "PRELIMINARY", notes
            return "RELEASED", notes
    return "RELEASED", ""


def fetch_bls_manifest_range(date_from, date_to, currencies=("USD",), *, manifest_path=DEFAULT_MANIFEST):
    start, end = date.fromisoformat(str(date_from)), date.fromisoformat(str(date_to))
    if "USD" not in {str(item).upper() for item in currencies}:
        return {"provider": "bls", "request_count": 0, "normalized_events": []}
    rows = [
        row for row in _validate_manifest(manifest_path)
        if start <= date.fromisoformat(row["release_date"]) <= end
    ]
    if not rows:
        return {"provider": "bls", "request_count": 0, "normalized_events": []}
    payload = bls_provider._post_series(start.year - 2, end.year)
    values = bls_provider._series_values(payload)
    family_indicators = {
        "cpi": ("cpi", "core_cpi"),
        "ppi": ("ppi",),
        "employment_situation": ("nonfarm_payrolls", "unemployment_rate", "average_hourly_earnings"),
    }
    names = {
        "cpi": ("US CPI y/y", "cpi_y_y"),
        "core_cpi": ("US Core CPI y/y", "core_cpi_y_y"),
        "ppi": ("US PPI y/y", "ppi_y_y"),
        "nonfarm_payrolls": ("US Non-Farm Payrolls", "nonfarm_payrolls"),
        "unemployment_rate": ("US Unemployment Rate", "unemployment_rate"),
        "average_hourly_earnings": ("US Average Hourly Earnings m/m", "average_hourly_earnings_m_m"),
    }
    events, missing = [], []
    for row in rows:
        year, month = map(int, row["reference_period"].split("-"))
        period = (year, month)
        release = datetime.fromisoformat(row["release_timestamp_utc"].replace("Z", "+00:00"))
        for indicator in family_indicators[row["release_family"]]:
            actual, previous = bls_provider._derived_actual(
                indicator, values.get(bls_provider.SERIES[indicator], {}), period
            )
            if actual is None:
                missing.append(f"{bls_provider.SERIES[indicator]}:{row['reference_period']}")
                continue
            status, notes = _bls_status(payload, bls_provider.SERIES[indicator], period)
            event_name, normalized_indicator = names[indicator]
            events.append(official_event(
                provider="bls", dataset="public_data_api_v2_manifest_v1",
                provider_event_id=f"bls:{bls_provider.SERIES[indicator]}:{year}:M{month:02d}",
                event_name=event_name, indicator=normalized_indicator,
                currency="USD", country="United States", release_time=release,
                actual=actual, previous=previous, data_status=status,
                raw={
                    "series_id": bls_provider.SERIES[indicator],
                    "reference_period": row["reference_period"],
                    "bls_manifest_id": row["stable_manifest_id"],
                    "bls_manifest_content_hash": row["content_hash"],
                    "bls_release_official_url": row["official_url"],
                    "bls_status_notes": notes,
                },
            ))
    if missing:
        raise RuntimeError("missing required BLS manifest value matches: " + ", ".join(missing))
    return {
        "provider": "bls", "dataset": "public_data_api_v2_manifest_v1",
        "request_count": 1, "normalized_events": events,
        "manifest_rows": len(rows), "matched_values": len(events),
        "missing_manifest_matches": [], "duplicate_manifest_ids": 0,
        "timestamp_conflicts": 0, "parsing_errors": [],
    }


def _load_or_create_job(factory, provider, start, end, chunk_days, currencies, resume):
    identifier = _job_id(provider, start, end, chunk_days, currencies)
    now = datetime.now(timezone.utc)
    with factory() as session:
        job = session.get(EconomicBackfillJob, identifier)
        if job:
            if not resume and job.status != "COMPLETED":
                raise RuntimeError("matching backfill job exists; pass --resume")
            return identifier, _as_date(job.current_cursor), job.status
        session.add(EconomicBackfillJob(
            job_id=identifier, provider=provider, date_from=_utc_midnight(start),
            date_to=_utc_midnight(end), current_cursor=_utc_midnight(start),
            chunk_days=chunk_days, status="PENDING", events_seen=0,
            observations_added=0, duplicates_skipped=0, attempt_count=0,
            started_at=now, updated_at=now,
        ))
        session.commit()
    return identifier, start, "PENDING"


def run_official_backfill(*, provider, date_from, date_to, chunk_days=31, currencies="EUR,USD",
                          resume=False, dry_run=False, fetcher=None, manifest_path=DEFAULT_MANIFEST,
                          session_factory=None, bind=None, lock_manager=None, sleeper=None,
                          rate_limit_seconds=0.5):
    provider = str(provider).lower()
    if provider not in {"bls", *PROVIDERS}:
        raise ValueError(f"unsupported official provider: {provider}")
    start, end = date.fromisoformat(str(date_from)), date.fromisoformat(str(date_to))
    if start > end or chunk_days < 1:
        raise ValueError("invalid date range or chunk size")
    currency_list = parse_currencies(currencies) if isinstance(currencies, str) else tuple(currencies)
    factory, database_bind = session_factory or SessionLocal, bind or engine
    fetch = fetcher or (lambda a, b, c: fetch_bls_manifest_range(a, b, c, manifest_path=manifest_path)) if provider == "bls" else (fetcher or PROVIDERS[provider])
    locks = lock_manager or (lambda key: advisory_lock(key, bind=database_bind))
    sleep = sleeper or time.sleep
    report = {"provider": provider, "date_from": str(start), "date_to": str(end), "status": "DRY_RUN" if dry_run else "PENDING",
              "dry_run": dry_run, "events_seen": 0, "observations_added": 0,
              "observations_would_add": 0, "duplicates_skipped": 0, "chunks_completed": 0,
              "requests_completed": 0, "database_writes": 0 if dry_run else "ATOMIC_CHUNK_TRANSACTIONS",
              "parsed_releases": 0, "matched_values": 0, "unmatched_values": [],
              "duplicate_manifest_ids": 0, "timestamp_conflicts": 0,
              "parsing_errors": [], "proposed_inserts": 0}
    cursor, job_id = start, None
    if not dry_run:
        job_id, cursor, status = _load_or_create_job(factory, provider, start, end, chunk_days, currency_list, resume)
        report["job_id"] = job_id
        if status == "COMPLETED":
            report["status"] = "COMPLETED"
            return report
    with locks(HISTORICAL_BACKFILL_LOCK_KEY) as acquired:
        if not acquired:
            return {**report, "status": "LOCKED"}
        while cursor <= end:
            chunk_end = min(end, cursor + timedelta(days=chunk_days - 1))
            with locks(LIVE_INGESTION_LOCK_KEY) as ingestion_clear:
                if not ingestion_clear:
                    return {**report, "status": "PAUSED", "reason": "live fundamental ingestion is active"}
                started_at = datetime.now(timezone.utc)
                try:
                    result = fetch(cursor, chunk_end, currency_list)
                except Exception as exc:
                    if job_id:
                        with factory() as session, session.begin():
                            job = session.get(EconomicBackfillJob, job_id)
                            job.status, job.last_error = "PAUSED", str(exc)
                            job.attempt_count += 1
                            job.updated_at = datetime.now(timezone.utc)
                    return {**report, "status": "PAUSED", "reason": str(exc)}
                normalized = list(result.get("normalized_events") or [])
                report["requests_completed"] += int(result.get("request_count") or 0)
                report["events_seen"] += len(normalized)
                report["parsed_releases"] += int(result.get("manifest_rows") or 0)
                report["matched_values"] += int(result.get("matched_values") or len(normalized))
                report["unmatched_values"].extend(result.get("missing_manifest_matches") or [])
                report["duplicate_manifest_ids"] += int(result.get("duplicate_manifest_ids") or 0)
                report["timestamp_conflicts"] += int(result.get("timestamp_conflicts") or 0)
                report["parsing_errors"].extend(result.get("parsing_errors") or [])
                if dry_run:
                    with factory() as session:
                        preview = preview_calendar_batch(session, provider, normalized, normalized)
                    report["observations_would_add"] += preview["observations_would_add"]
                    report["proposed_inserts"] += preview["observations_would_add"]
                    report["duplicates_skipped"] += preview["duplicates_skipped"]
                else:
                    with factory() as session, session.begin():
                        persisted = persist_calendar_batch_in_session(
                            session, provider, normalized, normalized,
                            started_at=started_at, completed_at=datetime.now(timezone.utc),
                        )
                        job = session.get(EconomicBackfillJob, job_id)
                        job.events_seen += persisted["events"]
                        job.observations_added += persisted["observations_added"]
                        job.duplicates_skipped += persisted["duplicates_skipped"]
                        job.attempt_count += 1
                        job.current_cursor = _utc_midnight(chunk_end + timedelta(days=1))
                        job.status, job.last_error, job.updated_at = "RUNNING", None, datetime.now(timezone.utc)
                        report["observations_added"] += persisted["observations_added"]
                        report["duplicates_skipped"] += persisted["duplicates_skipped"]
                report["chunks_completed"] += 1
            cursor = chunk_end + timedelta(days=1)
            if cursor <= end and rate_limit_seconds:
                sleep(rate_limit_seconds)
        report["status"] = "DRY_RUN" if dry_run else "COMPLETED"
        if job_id:
            with factory() as session, session.begin():
                job = session.get(EconomicBackfillJob, job_id)
                job.status, job.current_cursor = "COMPLETED", _utc_midnight(end + timedelta(days=1))
                job.updated_at = job.completed_at = datetime.now(timezone.utc)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="Official-source Fundamental Engine backfill")
    parser.add_argument("--provider", required=True, choices=("bls", *PROVIDERS))
    parser.add_argument("--from", dest="date_from", required=True)
    parser.add_argument("--to", dest="date_to", required=True)
    parser.add_argument("--chunk-days", type=int, default=31)
    parser.add_argument("--currencies", default="EUR,USD")
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    result = run_official_backfill(provider=args.provider, date_from=args.date_from,
        date_to=args.date_to, chunk_days=args.chunk_days, currencies=args.currencies,
        resume=args.resume, dry_run=args.dry_run, manifest_path=args.manifest)
    print(json.dumps(result, indent=2, default=str))
    return 0 if result["status"] in {"COMPLETED", "DRY_RUN"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
