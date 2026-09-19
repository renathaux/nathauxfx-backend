from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

from db import SessionLocal
from fundamentals.config import (
    FUNDAMENTAL_INGEST_INTERVAL_SECONDS,
    OFFICIAL_INGEST_INTERVAL_SECONDS,
    OFFICIAL_INGEST_LOOKBACK_DAYS,
)
from fundamentals.repositories.economic_events import (
    persist_calendar_batch,
    record_failed_fetch,
)
from fundamentals.repositories.observations import provider_health
from fundamentals.locks import LIVE_INGESTION_LOCK_KEY, advisory_lock
from models import EconomicProviderFetch
from services.market_hours import forex_weekend_closed


_INGEST_LOCK = threading.Lock()
_WORKER_LOCK = threading.Lock()
_WORKER_THREAD = None
_LAST_KICK_MONOTONIC = 0.0
_SCHEDULER_LOCK = threading.Lock()
_SCHEDULER_THREAD = None


def _official_last_attempt(provider, session_factory=None):
    factory = session_factory or SessionLocal
    with factory() as session:
        return session.query(EconomicProviderFetch.completed_at).filter_by(
            provider=provider
        ).order_by(EconomicProviderFetch.completed_at.desc()).limit(1).scalar()


def _official_provider_due(provider, current, *, session_factory=None):
    last = _official_last_attempt(provider, session_factory=session_factory)
    if last is None:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (current - last).total_seconds() >= OFFICIAL_INGEST_INTERVAL_SECONDS


def collect_official_provider_data(*, now=None, timeout=20, session_factory=None, fetchers=None):
    """Refresh recent official releases on a low-frequency independent cadence."""
    current = now or datetime.now(timezone.utc)
    if fetchers is None:
        from fundamentals.providers import bea, bls, ecb, eurostat, federal_reserve, treasury

        fetchers = {
            "bls": bls.fetch_range,
            "bea": bea.fetch_range,
            "eurostat": eurostat.fetch_range,
            "federal_reserve": federal_reserve.fetch_range,
            "ecb": ecb.fetch_range,
            "treasury": treasury.fetch_range,
        }
    date_from = (current - timedelta(days=OFFICIAL_INGEST_LOOKBACK_DAYS)).date().isoformat()
    date_to = current.date().isoformat()
    results = {"successful_providers": [], "failed_providers": [], "event_count": 0}
    for provider, fetch_provider in fetchers.items():
        if not _official_provider_due(provider, current, session_factory=session_factory):
            continue
        started_at = datetime.now(timezone.utc)
        try:
            payload = fetch_provider(date_from, date_to, ("EUR", "USD"), timeout=timeout)
            normalized = payload.get("normalized_events") or []
            persisted = persist_calendar_batch(
                provider,
                normalized,
                normalized,
                started_at=started_at,
                completed_at=datetime.now(timezone.utc),
                session_factory=session_factory,
            )
            results["event_count"] += int(persisted.get("events") or 0)
            results["successful_providers"].append(provider)
        except Exception as exc:
            results["failed_providers"].append({"provider": provider, "error": str(exc)})
            try:
                record_failed_fetch(
                    provider, exc, started_at=started_at, session_factory=session_factory
                )
            except Exception as persistence_exc:
                print("FUNDAMENTAL_OFFICIAL_FAILURE_PERSIST_ERROR =", {
                    "provider": provider, "error": str(persistence_exc),
                })
    return results


def collect_provider_data(*, now=None, timeout=8):
    """Collect trusted providers without changing the legacy News Mode path."""
    current = now or datetime.now(timezone.utc)
    if forex_weekend_closed(current):
        return {
            "status": "MARKET_CLOSED_WEEKEND",
            "event_count": 0,
            "successful_providers": [],
            "failed_providers": [],
        }

    from services.news_service import (
        fetch_finnhub_calendar_events,
        fetch_fmp_calendar_events,
        fetch_jblanked_calendar_events,
    )

    providers = (
        ("jblanked_mql5", lambda: (
            (events := fetch_jblanked_calendar_events(force=True, timeout=timeout)),
            events,
        )),
        ("fmp", lambda: fetch_fmp_calendar_events(timeout=timeout, now=current)),
        ("finnhub", lambda: fetch_finnhub_calendar_events(timeout=timeout, now=current)),
    )
    total_events = 0
    successful = []
    failed = []
    for provider, fetch_provider in providers:
        started_at = datetime.now(timezone.utc)
        try:
            raw_events, normalized_events = fetch_provider()
            result = persist_calendar_batch(
                provider,
                raw_events,
                normalized_events,
                started_at=started_at,
                completed_at=datetime.now(timezone.utc),
            )
            total_events += int(result.get("events") or 0)
            successful.append(provider)
        except Exception as exc:
            failed.append({"provider": provider, "error": str(exc)})
            try:
                record_failed_fetch(provider, exc, started_at=started_at)
            except Exception as persistence_exc:
                print("FUNDAMENTAL_PROVIDER_FAILURE_PERSIST_ERROR =", {
                    "provider": provider,
                    "error": str(persistence_exc),
                })
    official = collect_official_provider_data(now=current, timeout=max(timeout, 20))
    total_events += int(official.get("event_count") or 0)
    successful.extend(official.get("successful_providers") or [])
    failed.extend(official.get("failed_providers") or [])
    return {
        "status": "FETCHED" if successful else "FAILED",
        "event_count": total_events,
        "successful_providers": successful,
        "failed_providers": failed,
    }


def run_fundamental_ingestion_if_due(
    *, now=None, interval_seconds=FUNDAMENTAL_INGEST_INTERVAL_SECONDS, fetcher=None,
    health_reader=None,
):
    """Collect provider data without depending on a browser request.

    The latest durable successful fetch makes the due decision restart-safe.
    Failures are isolated from the trading loop and recorded by news_service.
    """
    current = now or datetime.now(timezone.utc)
    if forex_weekend_closed(current):
        return {"status": "MARKET_CLOSED_WEEKEND", "event_count": 0}
    read_health = health_reader or provider_health
    try:
        health = read_health(now=current)
    except Exception as exc:
        health = {"last_successful_provider_fetch": None, "health_error": str(exc)}
    last_attempt = health.get("last_provider_fetch_attempt") or health.get("last_successful_provider_fetch")
    if last_attempt is not None:
        if last_attempt.tzinfo is None:
            last_attempt = last_attempt.replace(tzinfo=timezone.utc)
        age = max(0.0, (current - last_attempt).total_seconds())
        if age < interval_seconds:
            return {"status": "NOT_DUE", "age_seconds": age}
    if not _INGEST_LOCK.acquire(blocking=False):
        return {"status": "ALREADY_RUNNING"}
    try:
        with advisory_lock(LIVE_INGESTION_LOCK_KEY) as acquired:
            if not acquired:
                return {"status": "ALREADY_RUNNING"}
            if fetcher is None:
                return collect_provider_data(now=current, timeout=8)
            events = fetcher(force=True, timeout=8, now=current)
            return {"status": "FETCHED", "event_count": len(events or [])}
    except Exception as exc:
        print("FUNDAMENTAL_BACKGROUND_INGEST_ERROR =", str(exc))
        return {"status": "FAILED", "error": str(exc)}
    finally:
        _INGEST_LOCK.release()


def kick_fundamental_ingestion():
    """Start a non-blocking provider check so trading-cycle timing is unaffected."""
    global _LAST_KICK_MONOTONIC, _WORKER_THREAD
    with _WORKER_LOCK:
        current_monotonic = time.monotonic()
        if current_monotonic - _LAST_KICK_MONOTONIC < 60.0:
            return {"status": "NOT_DUE"}
        if _WORKER_THREAD is not None and _WORKER_THREAD.is_alive():
            return {"status": "ALREADY_RUNNING"}
        _LAST_KICK_MONOTONIC = current_monotonic
        _WORKER_THREAD = threading.Thread(
            target=run_fundamental_ingestion_if_due,
            name="flowsignal-fundamental-ingestion",
            daemon=True,
        )
        _WORKER_THREAD.start()
        return {"status": "STARTED", "thread_id": _WORKER_THREAD.ident}


def _scheduler_loop():
    while True:
        try:
            kick_fundamental_ingestion()
        except Exception as exc:
            print("FUNDAMENTAL_SCHEDULER_ERROR =", str(exc))
        time.sleep(60)


def start_fundamental_ingestion_scheduler():
    """Start one restart-safe scheduler without touching the trading engine loop."""
    global _SCHEDULER_THREAD
    with _SCHEDULER_LOCK:
        if _SCHEDULER_THREAD is not None and _SCHEDULER_THREAD.is_alive():
            return {"status": "ALREADY_RUNNING", "thread_id": _SCHEDULER_THREAD.ident}
        _SCHEDULER_THREAD = threading.Thread(
            target=_scheduler_loop,
            name="flowsignal-fundamental-scheduler",
            daemon=True,
        )
        _SCHEDULER_THREAD.start()
        return {"status": "STARTED", "thread_id": _SCHEDULER_THREAD.ident}
