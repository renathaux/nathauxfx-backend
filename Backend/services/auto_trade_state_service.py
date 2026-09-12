"""Durable, backend-authoritative PAPER/LIVE Auto Trade preferences."""

from __future__ import annotations

import copy
import json
import os
import threading
import time
from datetime import datetime, timezone

from db import SessionLocal, engine
from models import AutoTradeStateAudit, RuntimeSetting


PAPER_SETTING = "paper_auto_trade_enabled"
LIVE_SETTING = "live_auto_trade_enabled"
SETTING_NAMES = {"paper": PAPER_SETTING, "live": LIVE_SETTING}
_LOCK = threading.RLock()
_CACHE_LOCK = threading.RLock()
_STATE_CACHE = {"state": None, "loaded_monotonic": 0.0}


def _cache_ttl_seconds():
    """Keep UI/status reads cheap without making trading state meaningfully stale.

    The default one-second TTL collapses bursts of repeated dashboard/status reads.
    Production broker submission performs its own authoritative refresh before a
    V3B order can be sent, so this cache is never the final LIVE safety authority.
    """
    try:
        value = float(os.getenv("AUTO_TRADE_STATE_CACHE_SECONDS", "1.0"))
    except (TypeError, ValueError):
        value = 1.0
    return max(0.0, min(value, 5.0))


def _cache_allowed(session_factory):
    # Tests and dependency-injected callers must keep exact database semantics.
    return session_factory is None


def _read_cached_state():
    ttl = _cache_ttl_seconds()
    if ttl <= 0:
        return None
    with _CACHE_LOCK:
        state = _STATE_CACHE.get("state")
        loaded_at = float(_STATE_CACHE.get("loaded_monotonic") or 0.0)
        if state is None or (time.monotonic() - loaded_at) > ttl:
            return None
        return copy.deepcopy(state)


def _store_cached_state(state):
    if not isinstance(state, dict):
        return
    with _CACHE_LOCK:
        _STATE_CACHE["state"] = copy.deepcopy(state)
        _STATE_CACHE["loaded_monotonic"] = time.monotonic()


def clear_state_cache():
    """Invalidate the process-local read-through cache."""
    with _CACHE_LOCK:
        _STATE_CACHE["state"] = None
        _STATE_CACHE["loaded_monotonic"] = 0.0


def _as_bool(value, default=False):
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return bool(default)


def _utc_iso(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def persistence_info():
    database_url = str(os.getenv("DATABASE_URL") or "")
    backend = engine.url.get_backend_name()
    durable = backend not in {"sqlite"}
    return {
        "backend": backend,
        "durable_across_deployments": durable,
        "configured_by_database_url": bool(database_url),
        "warning": None if durable else (
            "SQLite is process-local on Render unless its directory is mounted "
            "to a persistent disk. Configure DATABASE_URL with durable Postgres."
        ),
    }


def _legacy_values(legacy_path):
    if not legacy_path or not os.path.exists(legacy_path):
        return None
    try:
        with open(legacy_path, "r", encoding="utf-8") as file:
            state = json.load(file)
        return {
            "paper": _as_bool(state.get("paper_auto_enabled")),
            "live": _as_bool(state.get("live_auto_enabled")),
        }
    except Exception:
        return None


def load_state(legacy_path=None, session_factory=None, force_refresh=False):
    use_cache = _cache_allowed(session_factory)
    if use_cache and not force_refresh:
        cached = _read_cached_state()
        if cached is not None:
            return cached

    factory = session_factory or SessionLocal
    with factory() as session:
        # Read both preferences in one round trip instead of two session.get()
        # calls. Runtime settings are tiny but this path is hit frequently by
        # dashboard/status polling, so collapsing the reads materially reduces
        # Neon traffic without changing trading semantics.
        found = (
            session.query(RuntimeSetting)
            .filter(RuntimeSetting.setting_name.in_(tuple(SETTING_NAMES.values())))
            .all()
        )
        by_name = {row.setting_name: row for row in found}
        rows = {
            mode: by_name.get(setting_name)
            for mode, setting_name in SETTING_NAMES.items()
        }
        if any(rows.values()):
            timestamps = [row.updated_at for row in rows.values() if row is not None]
            latest = max(timestamps) if timestamps else None
            state = {
                "paper_enabled": _as_bool(
                    rows["paper"].setting_value if rows["paper"] else False
                ),
                "live_enabled": _as_bool(
                    rows["live"].setting_value if rows["live"] else False
                ),
                "updated_at": _utc_iso(latest),
                "updated_by": next(
                    (row.updated_by for row in rows.values() if row is not None),
                    "unknown",
                ),
                "source": "runtime_setting",
                "persistence": persistence_info(),
            }
            if use_cache:
                _store_cached_state(state)
            return state

    legacy = _legacy_values(legacy_path)
    if legacy is not None:
        state = save_state(
            paper_enabled=legacy["paper"],
            live_enabled=legacy["live"],
            updated_by="legacy_migration",
            request_source="startup_migration",
            reason="Migrated auto_trade_state.json to RuntimeSetting",
            session_factory=factory,
        )
        if use_cache:
            _store_cached_state(state)
        return state

    state = {
        "paper_enabled": _as_bool(os.getenv("PAPER_AUTO_TRADE_ENABLED", False)),
        "live_enabled": _as_bool(os.getenv("LIVE_AUTO_TRADE_ENABLED", False)),
        "updated_at": None,
        "updated_by": "environment" if (
            os.getenv("PAPER_AUTO_TRADE_ENABLED") is not None
            or os.getenv("LIVE_AUTO_TRADE_ENABLED") is not None
        ) else "system",
        "source": "environment" if (
            os.getenv("PAPER_AUTO_TRADE_ENABLED") is not None
            or os.getenv("LIVE_AUTO_TRADE_ENABLED") is not None
        ) else "default",
        "persistence": persistence_info(),
    }
    if use_cache:
        _store_cached_state(state)
    return state


def save_state(
    *, paper_enabled, live_enabled, updated_by="user",
    request_source="api", reason=None, active_broker_account=None,
    broker_environment=None, session_factory=None, now=None,
):
    factory = session_factory or SessionLocal
    updated_at = now or datetime.now(timezone.utc)
    requested = {
        "paper": bool(paper_enabled),
        "live": bool(live_enabled),
    }

    with _LOCK:
        with factory() as session:
            previous = {}
            for mode, setting_name in SETTING_NAMES.items():
                row = session.get(RuntimeSetting, setting_name)
                previous[mode] = _as_bool(row.setting_value) if row else False
                if row is None:
                    row = RuntimeSetting(setting_name=setting_name)
                    session.add(row)
                row.setting_value = "true" if requested[mode] else "false"
                row.updated_at = updated_at
                row.updated_by = str(updated_by or "user")

            for mode in ["paper", "live"]:
                if previous[mode] == requested[mode]:
                    continue
                session.add(AutoTradeStateAudit(
                    trading_mode=mode.upper(),
                    previous_enabled=previous[mode],
                    new_enabled=requested[mode],
                    updated_by=str(updated_by or "user"),
                    active_broker_account=(
                        str(active_broker_account)
                        if active_broker_account not in (None, "") else None
                    ),
                    broker_environment=(
                        str(broker_environment)
                        if broker_environment not in (None, "") else None
                    ),
                    timestamp=updated_at,
                    request_source=str(request_source or "api"),
                    reason=str(reason) if reason else None,
                ))
            session.commit()

    state = {
        "paper_enabled": requested["paper"],
        "live_enabled": requested["live"],
        "updated_at": _utc_iso(updated_at),
        "updated_by": str(updated_by or "user"),
        "source": "runtime_setting",
        "request_source": str(request_source or "api"),
        "reason": reason,
        "persistence": persistence_info(),
    }
    if _cache_allowed(session_factory):
        _store_cached_state(state)
    return state


def save_mode(
    *, mode, enabled, updated_by="user", request_source="api", reason=None,
    active_broker_account=None, broker_environment=None,
    session_factory=None, now=None,
):
    """Persist one Auto Trade preference without rewriting the other mode.

    PAPER and LIVE are independent user preferences.  Updating one from a
    worker with stale in-memory state must never silently overwrite the other.
    """
    normalized_mode = str(mode or "").strip().lower()
    if normalized_mode not in SETTING_NAMES:
        raise ValueError("Auto Trade mode must be PAPER or LIVE")

    factory = session_factory or SessionLocal
    updated_at = now or datetime.now(timezone.utc)
    setting_name = SETTING_NAMES[normalized_mode]
    requested = bool(enabled)

    with _LOCK:
        with factory() as session:
            row = session.get(RuntimeSetting, setting_name)
            previous = _as_bool(row.setting_value) if row else False
            if row is None:
                row = RuntimeSetting(setting_name=setting_name)
                session.add(row)
            row.setting_value = "true" if requested else "false"
            row.updated_at = updated_at
            row.updated_by = str(updated_by or "user")

            if previous != requested:
                session.add(AutoTradeStateAudit(
                    trading_mode=normalized_mode.upper(),
                    previous_enabled=previous,
                    new_enabled=requested,
                    updated_by=str(updated_by or "user"),
                    active_broker_account=(
                        str(active_broker_account)
                        if active_broker_account not in (None, "") else None
                    ),
                    broker_environment=(
                        str(broker_environment)
                        if broker_environment not in (None, "") else None
                    ),
                    timestamp=updated_at,
                    request_source=str(request_source or "api"),
                    reason=str(reason) if reason else None,
                ))
            session.commit()

    # A mode update is rare and safety-sensitive. Refresh once authoritatively
    # so the returned state includes the untouched mode, then refresh the cache.
    state = load_state(session_factory=factory, force_refresh=True)
    if _cache_allowed(session_factory):
        _store_cached_state(state)
    return {
        **state,
        "request_source": str(request_source or "api"),
        "reason": reason,
        "changed_mode": normalized_mode.upper(),
    }


def latest_changes(limit=10, session_factory=None):
    factory = session_factory or SessionLocal
    with factory() as session:
        rows = (
            session.query(AutoTradeStateAudit)
            .order_by(AutoTradeStateAudit.timestamp.desc())
            .limit(max(1, min(int(limit), 100)))
            .all()
        )
        return [{
            "mode": row.trading_mode,
            "previous_enabled": bool(row.previous_enabled),
            "new_enabled": bool(row.new_enabled),
            "updated_by": row.updated_by,
            "active_broker_account": row.active_broker_account,
            "broker_environment": row.broker_environment,
            "timestamp": _utc_iso(row.timestamp),
            "request_source": row.request_source,
            "reason": row.reason,
        } for row in rows]
