"""Backend-authoritative configuration for the active production strategy.

The production execution profile keeps its stable V3B identity, while the small
set of owner-approved management parameters below can be overridden without
editing frontend constants. Overrides are namespaced by the active profile, so
promoting a future strategy version automatically exposes that version's own
fresh defaults instead of silently inheriting settings from an older strategy.

Strategy Lab research modules remain frozen and reproducible. This service is
for the production PAPER/LIVE bridge only.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone

from db import SessionLocal
from models import RuntimeSetting, StrategySettingAudit


ACTIVE_STRATEGY_PROFILE = "V3B_M5_FROZEN"
ACTIVE_STRATEGY_VERSION = "V3B"
SETTING_PREFIX = f"strategy_profile.{ACTIVE_STRATEGY_PROFILE}."
CACHE_TTL_SECONDS = 30.0
FAILURE_CACHE_TTL_SECONDS = 5.0

logger = logging.getLogger("flowsignal.active_strategy_config")
_LOCK = threading.RLock()
_CACHE_LOCK = threading.RLock()
_CACHE = {}

FIELD_DEFINITIONS = OrderedDict(
    (
        (
            "target_rr",
            {
                "default": 1.90,
                "type": "number",
                "min": 0.50,
                "max": 5.00,
                "step": 0.05,
                "unit": "R",
                "label": "TP2 Risk / Reward",
            },
        ),
        (
            "protection_trigger_percent",
            {
                "default": 70.0,
                "type": "number",
                "min": 1.0,
                "max": 100.0,
                "step": 1.0,
                "unit": "% of TP2 path",
                "label": "Protection Trigger",
            },
        ),
        (
            "protected_stop_percent",
            {
                "default": 60.0,
                "type": "number",
                "min": 0.0,
                "max": 100.0,
                "step": 1.0,
                "unit": "% of TP2 path",
                "label": "Protected SL",
            },
        ),
    )
)

FIXED_RULES = {
    "setup_timeframe": "5m",
    "bos_body_minimum_percent": 50.0,
    "immediate_next_5m_confirmation_required": True,
    "confirmation_must_remain_beyond_bos": True,
    "event_owned_5m_invalidation": True,
    "sl_buffer_points": {"EURUSD": 50, "XAUUSD": 50},
    "minimum_sl_distance_points": {"EURUSD": 100, "XAUUSD": 100},
    "partial_close_at_protection_trigger": False,
    "ema_filter": False,
    "m15_entry_dependency": False,
    "consolidation_filter": False,
}


class ActiveStrategyConfigError(ValueError):
    pass


def _utc_iso(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def defaults():
    return {key: definition["default"] for key, definition in FIELD_DEFINITIONS.items()}


def limits():
    return {
        key: {
            item: value
            for item, value in definition.items()
            if item in {"type", "min", "max", "step", "unit", "label"}
        }
        for key, definition in FIELD_DEFINITIONS.items()
    }


def _setting_name(key):
    return f"{SETTING_PREFIX}{key}"


def _cache_key(factory):
    return factory


def invalidate_cache(session_factory=None):
    factory = session_factory or SessionLocal
    with _CACHE_LOCK:
        _CACHE.pop(_cache_key(factory), None)


def _coerce(key, value):
    definition = FIELD_DEFINITIONS[key]
    if isinstance(value, bool):
        raise ActiveStrategyConfigError(f"{key} must be a number")
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ActiveStrategyConfigError(f"{key} must be a number") from exc
    if numeric < definition["min"] or numeric > definition["max"]:
        raise ActiveStrategyConfigError(
            f"{key} must be between {definition['min']} and {definition['max']}"
        )
    return float(numeric)


def validate(values, *, merge_defaults=False):
    if not isinstance(values, dict):
        raise ActiveStrategyConfigError("settings must be an object")
    unknown = sorted(set(values) - set(FIELD_DEFINITIONS))
    if unknown:
        raise ActiveStrategyConfigError(
            f"unknown active strategy setting(s): {', '.join(unknown)}"
        )
    result = defaults() if merge_defaults else {}
    for key, value in values.items():
        result[key] = _coerce(key, value)
    candidate = {**defaults(), **result}
    if candidate["protected_stop_percent"] > candidate["protection_trigger_percent"]:
        raise ActiveStrategyConfigError(
            "protected_stop_percent cannot be greater than protection_trigger_percent"
        )
    return result


def _read_values(factory):
    names = [_setting_name(key) for key in FIELD_DEFINITIONS]
    with factory() as session:
        rows = (
            session.query(RuntimeSetting)
            .filter(RuntimeSetting.setting_name.in_(names))
            .all()
        )
    current = defaults()
    latest = None
    for row in rows:
        key = row.setting_name[len(SETTING_PREFIX):]
        if key not in FIELD_DEFINITIONS:
            continue
        try:
            current[key] = _coerce(key, json.loads(row.setting_value))
        except (TypeError, ValueError, json.JSONDecodeError, ActiveStrategyConfigError) as exc:
            raise ActiveStrategyConfigError(f"malformed persisted {key}") from exc
        if latest is None or row.updated_at > latest:
            latest = row.updated_at
    validate(current, merge_defaults=True)
    return current, latest


def get_active_values(
    session_factory=None,
    *,
    force_refresh=False,
    fail_closed=False,
    monotonic_now=None,
):
    """Return the active profile values with a tiny read-through cache.

    PAPER/UI reads may fall back to profile defaults if the database is briefly
    unavailable. LIVE final handoff uses ``force_refresh=True, fail_closed=True``
    so an unavailable or malformed authoritative configuration blocks the order.
    """
    factory = session_factory or SessionLocal
    now = time.monotonic() if monotonic_now is None else float(monotonic_now)
    key = _cache_key(factory)
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if not force_refresh and cached and now < cached["expires_at"]:
            return dict(cached["values"])

    with _LOCK:
        with _CACHE_LOCK:
            cached = _CACHE.get(key)
            if not force_refresh and cached and now < cached["expires_at"]:
                return dict(cached["values"])
        try:
            values, _latest = _read_values(factory)
            ttl = CACHE_TTL_SECONDS
            source = "runtime_setting"
        except Exception as exc:
            if fail_closed:
                raise ActiveStrategyConfigError(
                    f"active strategy configuration unavailable: {exc}"
                ) from exc
            values = defaults()
            ttl = FAILURE_CACHE_TTL_SECONDS
            source = "safe_defaults"
            logger.warning(
                "ACTIVE_STRATEGY_CONFIG_FALLBACK %s",
                {"error_type": type(exc).__name__, "error": str(exc)},
            )
        with _CACHE_LOCK:
            _CACHE[key] = {
                "values": dict(values),
                "source": source,
                "expires_at": now + ttl,
            }
        return dict(values)


def get_active_strategy_settings(session_factory=None):
    factory = session_factory or SessionLocal
    try:
        current, latest = _read_values(factory)
        source = "runtime_setting"
        with _CACHE_LOCK:
            _CACHE[_cache_key(factory)] = {
                "values": dict(current),
                "source": source,
                "expires_at": time.monotonic() + CACHE_TTL_SECONDS,
            }
    except Exception as exc:
        current = defaults()
        latest = None
        source = "safe_defaults"
        logger.warning(
            "ACTIVE_STRATEGY_SETTINGS_READ_FALLBACK %s",
            {"error_type": type(exc).__name__, "error": str(exc)},
        )
    return {
        "profile": ACTIVE_STRATEGY_PROFILE,
        "version": ACTIVE_STRATEGY_VERSION,
        "phase": "V3B_ACTIVE",
        "current": current,
        "defaults": defaults(),
        "limits": limits(),
        "editable": list(FIELD_DEFINITIONS),
        "execution_wiring": {key: True for key in FIELD_DEFINITIONS},
        "fixed_rules": dict(FIXED_RULES),
        "last_updated": _utc_iso(latest),
        "source": source,
        "strategy_version_scoped": True,
    }


def save_active_strategy_settings(
    payload,
    updated_by,
    session_factory=None,
    now=None,
):
    validated = validate(payload)
    factory = session_factory or SessionLocal
    changed_at = now or datetime.now(timezone.utc)
    with _LOCK:
        try:
            current, _latest = _read_values(factory)
        except Exception as exc:
            raise ActiveStrategyConfigError(
                f"could not read active strategy configuration: {exc}"
            ) from exc
        merged = {**current, **validated}
        validate(merged, merge_defaults=True)
        with factory() as session:
            for key, new_value in validated.items():
                old_value = current[key]
                if float(old_value) == float(new_value):
                    continue
                name = _setting_name(key)
                row = session.get(RuntimeSetting, name)
                encoded = json.dumps(new_value, separators=(",", ":"))
                if row is None:
                    row = RuntimeSetting(
                        setting_name=name,
                        setting_value=encoded,
                        updated_at=changed_at,
                        updated_by=str(updated_by or "unknown"),
                    )
                    session.add(row)
                else:
                    row.setting_value = encoded
                    row.updated_at = changed_at
                    row.updated_by = str(updated_by or "unknown")
                session.add(
                    StrategySettingAudit(
                        setting_name=f"{ACTIVE_STRATEGY_PROFILE}.{key}",
                        previous_value=json.dumps(old_value, separators=(",", ":")),
                        new_value=encoded,
                        updated_at=changed_at,
                        updated_by=str(updated_by or "unknown"),
                    )
                )
            session.commit()
        with _CACHE_LOCK:
            _CACHE[_cache_key(factory)] = {
                "values": dict(merged),
                "source": "runtime_setting",
                "expires_at": time.monotonic() + CACHE_TTL_SECONDS,
            }
    return get_active_strategy_settings(factory)


def reset_active_strategy_settings(
    *,
    confirmed,
    updated_by,
    session_factory=None,
    now=None,
):
    if confirmed is not True:
        raise ActiveStrategyConfigError("reset requires explicit confirmation")
    factory = session_factory or SessionLocal
    changed_at = now or datetime.now(timezone.utc)
    target = defaults()
    with _LOCK:
        try:
            current, _latest = _read_values(factory)
        except Exception as exc:
            raise ActiveStrategyConfigError(
                f"could not read active strategy configuration: {exc}"
            ) from exc
        with factory() as session:
            for key, default_value in target.items():
                previous = current[key]
                name = _setting_name(key)
                row = session.get(RuntimeSetting, name)
                encoded = json.dumps(default_value, separators=(",", ":"))
                if row is None:
                    row = RuntimeSetting(
                        setting_name=name,
                        setting_value=encoded,
                        updated_at=changed_at,
                        updated_by=str(updated_by or "unknown"),
                    )
                    session.add(row)
                else:
                    row.setting_value = encoded
                    row.updated_at = changed_at
                    row.updated_by = str(updated_by or "unknown")
                if float(previous) != float(default_value):
                    session.add(
                        StrategySettingAudit(
                            setting_name=f"{ACTIVE_STRATEGY_PROFILE}.{key}",
                            previous_value=json.dumps(previous, separators=(",", ":")),
                            new_value=encoded,
                            updated_at=changed_at,
                            updated_by=str(updated_by or "unknown"),
                        )
                    )
            session.commit()
        with _CACHE_LOCK:
            _CACHE[_cache_key(factory)] = {
                "values": dict(target),
                "source": "runtime_setting",
                "expires_at": time.monotonic() + CACHE_TTL_SECONDS,
            }
    return get_active_strategy_settings(factory)
