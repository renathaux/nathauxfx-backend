"""Resumable historical macro-data backfill command.

This module is deliberately isolated from broker, strategy, and auto-trading
modules. Importing it never starts a job; execution requires an explicit CLI.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
from datetime import date, datetime, time as datetime_time, timedelta, timezone

from db import SessionLocal, engine
from fundamentals.locks import (
    HISTORICAL_BACKFILL_LOCK_KEY,
    LIVE_INGESTION_LOCK_KEY,
    advisory_lock,
)
from fundamentals.preflight import parse_currencies
from fundamentals.provider_audit import analyze_events
from fundamentals.providers.jblanked import (
    DATASET_ID,
    JBlankedAccessError,
    fetch_range,
)
from fundamentals.repositories.economic_events import (
    persist_calendar_batch_in_session,
    preview_calendar_batch,
)
from models import EconomicBackfillJob


def _utc_midnight(value):
    parsed = value if isinstance(value, date) else date.fromisoformat(str(value))
    return datetime.combine(parsed, datetime_time.min, tzinfo=timezone.utc)


def _as_date(value):
    return value.date() if isinstance(value, datetime) else value


def _job_id(provider, start, end, chunk_days, currencies):
    basis = (
        f"{provider}|{start.isoformat()}|{end.isoformat()}|{chunk_days}|"
        f"{','.join(sorted(currencies))}"
    )
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


def _chunks(start, end, chunk_days):
    cursor = start
    while cursor <= end:
        chunk_end = min(end, cursor + timedelta(days=chunk_days - 1))
        yield cursor, chunk_end
        cursor = chunk_end + timedelta(days=1)


def estimated_request_count(start, end, chunk_days):
    return int(math.ceil(((end - start).days + 1) / chunk_days))


def _load_or_create_job(
    session_factory, *, provider, start, end, chunk_days, currencies, resume
):
    identifier = _job_id(provider, start, end, chunk_days, currencies)
    now = datetime.now(timezone.utc)
    with session_factory() as session:
        job = session.get(EconomicBackfillJob, identifier)
        if job is not None:
            if not resume:
                raise RuntimeError(
                    "matching backfill job exists; pass --resume to continue safely"
                )
            return identifier, _as_date(job.current_cursor), job.status
        job = EconomicBackfillJob(
            job_id=identifier,
            provider=provider,
            date_from=_utc_midnight(start),
            date_to=_utc_midnight(end),
            current_cursor=_utc_midnight(start),
            chunk_days=chunk_days,
            status="PENDING",
            events_seen=0,
            observations_added=0,
            duplicates_skipped=0,
            attempt_count=0,
            started_at=now,
            updated_at=now,
        )
        session.add(job)
        session.commit()
        return identifier, start, "PENDING"


def _mark_paused(session_factory, job_id, error, *, increment_attempt=False):
    with session_factory() as session:
        with session.begin():
            job = session.get(EconomicBackfillJob, job_id)
            if job is not None:
                job.status = "PAUSED"
                job.last_error = str(error)
                job.updated_at = datetime.now(timezone.utc)
                if increment_attempt:
                    job.attempt_count += 1


def run_backfill(
    *,
    provider,
    date_from,
    date_to,
    chunk_days,
    currencies,
    resume=False,
    dry_run=False,
    fetcher=None,
    session_factory=None,
    bind=None,
    lock_manager=None,
    sleeper=None,
    rate_limit_seconds=1.0,
    max_chunks=None,
):
    """Run or preview a historical backfill; HTTP occurs outside transactions."""
    if str(provider).lower() != "jblanked":
        raise ValueError("only provider 'jblanked' is supported")
    start = date.fromisoformat(str(date_from))
    end = date.fromisoformat(str(date_to))
    if start > end:
        raise ValueError("--from must be on or before --to")
    if int(chunk_days) < 1:
        raise ValueError("--chunk-days must be at least 1")
    currency_list = parse_currencies(currencies) if isinstance(currencies, str) else tuple(currencies)
    factory = session_factory or SessionLocal
    database_bind = bind or engine
    fetch = fetcher or fetch_range
    sleep = sleeper or time.sleep
    locks = lock_manager or (lambda key: advisory_lock(key, bind=database_bind))
    total_requests = estimated_request_count(start, end, int(chunk_days))
    report = {
        "status": "DRY_RUN" if dry_run else "PENDING",
        "dry_run": bool(dry_run),
        "provider": "jblanked",
        "dataset": "mql5",
        "provider_identity": DATASET_ID,
        "date_from": start.isoformat(),
        "date_to": end.isoformat(),
        "currencies": list(currency_list),
        "estimated_request_count": total_requests,
        "estimated_credit_usage": total_requests,
        "requests_completed": 0,
        "events_seen": 0,
        "observations_added": 0,
        "observations_would_add": 0,
        "duplicates_skipped": 0,
        "conflicts": [],
        "chunks_completed": 0,
        "database_writes": 0 if dry_run else None,
    }

    job_id = None
    cursor = start
    if not dry_run:
        job_id, cursor, existing_status = _load_or_create_job(
            factory,
            provider=DATASET_ID,
            start=start,
            end=end,
            chunk_days=int(chunk_days),
            currencies=currency_list,
            resume=resume,
        )
        report["job_id"] = job_id
        if existing_status == "COMPLETED":
            report["status"] = "COMPLETED"
            return report

    with locks(HISTORICAL_BACKFILL_LOCK_KEY) as backfill_acquired:
        if not backfill_acquired:
            report.update(status="LOCKED", reason="another historical backfill is active")
            return report

        for chunk_index, (chunk_start, chunk_end) in enumerate(
            _chunks(cursor, end, int(chunk_days))
        ):
            if max_chunks is not None and chunk_index >= max_chunks:
                report["status"] = "PAUSED"
                report["reason"] = "max_chunks reached"
                break
            with locks(LIVE_INGESTION_LOCK_KEY) as ingestion_acquired:
                if not ingestion_acquired:
                    report.update(
                        status="PAUSED",
                        reason="live fundamental ingestion is active",
                    )
                    if job_id:
                        _mark_paused(factory, job_id, report["reason"])
                    return report

                started_at = datetime.now(timezone.utc)
                try:
                    # No database session or transaction is open during HTTP.
                    provider_result = fetch(chunk_start, chunk_end, currency_list)
                    report["requests_completed"] += int(
                        provider_result.get("request_count") or 1
                    )
                except Exception as exc:
                    safe_error = str(exc)
                    report.update(status="PAUSED", reason=safe_error)
                    if job_id:
                        with factory() as session:
                            with session.begin():
                                persist_calendar_batch_in_session(
                                    session,
                                    DATASET_ID,
                                    [],
                                    [],
                                    status="FAILED",
                                    error=safe_error,
                                    started_at=started_at,
                                )
                                job = session.get(EconomicBackfillJob, job_id)
                                job.status = "PAUSED"
                                job.last_error = safe_error
                                job.attempt_count += 1
                                job.updated_at = datetime.now(timezone.utc)
                    return report

                raw_events = provider_result["raw_events"]
                normalized_events = provider_result["normalized_events"]
                audit = analyze_events(
                    normalized_events, provider_identity=DATASET_ID
                )
                report["events_seen"] += audit["event_count"]
                report["conflicts"].extend(audit["conflicts"])

                if dry_run:
                    with factory() as session:
                        preview = preview_calendar_batch(
                            session, DATASET_ID, raw_events, normalized_events
                        )
                    report["observations_would_add"] += preview["observations_would_add"]
                    report["duplicates_skipped"] += preview["duplicates_skipped"]
                else:
                    # Fetch result, observations, counters, and cursor are one
                    # atomic chunk transaction.
                    with factory() as session:
                        with session.begin():
                            persisted = persist_calendar_batch_in_session(
                                session,
                                DATASET_ID,
                                raw_events,
                                normalized_events,
                                started_at=started_at,
                                completed_at=datetime.now(timezone.utc),
                            )
                            job = session.get(EconomicBackfillJob, job_id)
                            job.events_seen += persisted["events"]
                            job.observations_added += persisted["observations_added"]
                            job.duplicates_skipped += persisted["duplicates_skipped"]
                            job.attempt_count += 1
                            job.current_cursor = _utc_midnight(chunk_end + timedelta(days=1))
                            job.status = "RUNNING"
                            job.last_error = None
                            job.updated_at = datetime.now(timezone.utc)
                            report["observations_added"] += persisted["observations_added"]
                            report["duplicates_skipped"] += persisted["duplicates_skipped"]
                    report["database_writes"] = "ATOMIC_CHUNK_TRANSACTIONS"

                report["chunks_completed"] += 1
            if chunk_end < end and rate_limit_seconds > 0:
                sleep(rate_limit_seconds)
        else:
            report["status"] = "DRY_RUN" if dry_run else "COMPLETED"
            if job_id:
                with factory() as session:
                    with session.begin():
                        job = session.get(EconomicBackfillJob, job_id)
                        job.status = "COMPLETED"
                        job.current_cursor = _utc_midnight(end + timedelta(days=1))
                        job.updated_at = datetime.now(timezone.utc)
                        job.completed_at = job.updated_at
    return report


def build_parser():
    parser = argparse.ArgumentParser(description="Resumable Fundamental Engine historical backfill")
    parser.add_argument("--provider", required=True, choices=("jblanked",))
    parser.add_argument("--from", dest="date_from", required=True)
    parser.add_argument("--to", dest="date_to", required=True)
    parser.add_argument("--chunk-days", type=int, default=7)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--currencies", default="EUR,USD")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        result = run_backfill(
            provider=args.provider,
            date_from=args.date_from,
            date_to=args.date_to,
            chunk_days=args.chunk_days,
            resume=args.resume,
            currencies=args.currencies,
            dry_run=args.dry_run,
        )
    except (JBlankedAccessError, ValueError, RuntimeError) as exc:
        print(json.dumps({"status": "FAILED", "error": str(exc)}, indent=2))
        return 2
    print(json.dumps(result, indent=2, default=str))
    return 0 if result["status"] in ("COMPLETED", "DRY_RUN") else 2


if __name__ == "__main__":
    raise SystemExit(main())
