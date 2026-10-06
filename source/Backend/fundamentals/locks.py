"""Database-backed coordination for Fundamental Engine maintenance jobs."""

from __future__ import annotations

import threading
import os
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from db import normalize_database_url


# Fixed signed-bigint keys; changing them would break cross-process locking.
LIVE_INGESTION_LOCK_KEY = 2_026_080_701
HISTORICAL_BACKFILL_LOCK_KEY = 2_026_080_702

_LOCAL_GUARD = threading.Lock()
_LOCAL_LOCKS: dict[int, threading.Lock] = {}
_DIRECT_ENGINE = None
_DIRECT_ENGINE_URL = None


def _coordination_bind(bind):
    """Use Neon's direct endpoint for session locks, never its pooler.

    PgBouncer transaction pooling can run lock and unlock statements on
    different server sessions, leaking session-level advisory locks. Render's
    MIGRATION_DATABASE_URL is the direct Neon endpoint already used by
    Alembic. NullPool also ensures the lock connection is not reused locally.
    """
    if bind.dialect.name != "postgresql":
        return bind
    direct_url = os.getenv("MIGRATION_DATABASE_URL", "").strip()
    if not direct_url:
        return bind
    direct_url = normalize_database_url(direct_url)
    global _DIRECT_ENGINE, _DIRECT_ENGINE_URL
    with _LOCAL_GUARD:
        if _DIRECT_ENGINE is None or _DIRECT_ENGINE_URL != direct_url:
            if _DIRECT_ENGINE is not None:
                _DIRECT_ENGINE.dispose()
            _DIRECT_ENGINE = create_engine(
                direct_url,
                poolclass=NullPool,
                pool_pre_ping=True,
                connect_args={
                    "connect_timeout": int(os.getenv("DB_CONNECT_TIMEOUT_SECONDS", "10")),
                    "application_name": "flowsignal-fundamentals-lock",
                },
            )
            _DIRECT_ENGINE_URL = direct_url
    return _DIRECT_ENGINE


@contextmanager
def advisory_lock(lock_key, *, bind=None):
    """Yield whether a non-blocking advisory lock was acquired.

    PostgreSQL uses a session-level advisory lock, which works across Render
    workers. SQLite uses a process-local lock solely for local tests.
    """
    if bind is None:
        from db import engine as bind

    if bind.dialect.name != "postgresql":
        with _LOCAL_GUARD:
            local_lock = _LOCAL_LOCKS.setdefault(int(lock_key), threading.Lock())
        acquired = local_lock.acquire(blocking=False)
        try:
            yield acquired
        finally:
            if acquired:
                local_lock.release()
        return

    # Session advisory locks do not require a transaction. AUTOCOMMIT keeps
    # HTTP latency from holding an idle database transaction open.
    lock_bind = _coordination_bind(bind)
    connection = lock_bind.connect().execution_options(isolation_level="AUTOCOMMIT")
    acquired = False
    try:
        acquired = bool(connection.execute(
            text("SELECT pg_try_advisory_lock(:lock_key)"),
            {"lock_key": int(lock_key)},
        ).scalar())
        yield acquired
    finally:
        if acquired:
            connection.execute(
                text("SELECT pg_advisory_unlock(:lock_key)"),
                {"lock_key": int(lock_key)},
            )
        connection.close()
