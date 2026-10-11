"""Durable, backend-authoritative Strategy Settings and execution read cache."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone

from db import SessionLocal
from models import RuntimeSetting, StrategySettingAudit


SETTING_PREFIX = "strategy."
_LOCK = threading.RLock()
_CACHE_LOCK = threading.RLock()
_EXECUTION_CACHE = {}
EXECUTION_CACHE_TTL_SECONDS = 30.0
EXECUTION_FAILURE_CACHE_TTL_SECONDS = 5.0
WIRED_EXECUTION_SETTINGS = frozenset({
    "minimum_rr",
    "maximum_rr",
    "bos_buffer_points",
    "minimum_sl_distance_points",
    "post_trade_cooldown_minutes",
    "consolidation_filter_enabled",
})
EDITABLE_STRATEGY_SETTINGS = WIRED_EXECUTION_SETTINGS
LEGACY_SETTING_ALIASES = {
    "bos_buffer_points": "bos_buffer_min_points",
}
logger = logging.getLogger("flowsignal.strategy_settings")

FIELD_DEFINITIONS = OrderedDict(
    (
        ("minimum_rr", {"default": 1.20, "type": "number", "min": 1.0, "max": 5.0, "step": 0.05, "unit": "R"}),
        ("maximum_rr", {"default": 2.00, "type": "number", "min": 1.0, "max": 10.0, "step": 0.05, "unit": "R"}),
        ("risk_per_trade_percent", {"default": 1.0, "type": "number", "min": 0.05, "max": 1.0, "step": 0.05, "unit": "%"}),
        ("tp1_percent_of_tp2", {"default": 80.0, "type": "number", "min": 1.0, "max": 100.0, "step": 1.0, "unit": "%"}),
        ("protected_sl_percent_of_tp2", {"default": 50.0, "type": "number", "min": 0.0, "max": 100.0, "step": 1.0, "unit": "%"}),
        ("bos_buffer_points", {"default": 10, "type": "integer", "min": 0, "max": 500, "step": 1, "unit": "points"}),
        ("minimum_sl_distance_points", {"default": 100, "type": "integer", "min": 1, "max": 2000, "step": 1, "unit": "points"}),
        ("ema_filter_enabled", {"default": True, "type": "boolean"}),
        ("ema_fast_period", {"default": 9, "type": "integer", "min": 2, "max": 100, "step": 1, "unit": "candles"}),
        ("ema_slow_period", {"default": 21, "type": "integer", "min": 3, "max": 300, "step": 1, "unit": "candles"}),
        ("consolidation_filter_enabled", {"default": True, "type": "boolean"}),
        ("m15_close_required", {"default": True, "type": "boolean"}),
        ("m5_confirmation_required", {"default": True, "type": "boolean"}),
        ("fresh_bos_after_consolidation", {"default": True, "type": "boolean"}),
        ("post_trade_cooldown_minutes", {"default": 15, "type": "integer", "min": 0, "max": 1440, "step": 1, "unit": "minutes"}),
    )
)


class StrategySettingsValidationError(ValueError):
    pass


def _utc_iso(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def defaults():
    return {key: definition["default"] for key, definition in FIELD_DEFINITIONS.items()}


def validation_limits():
    return {
        key: {
            item: value
            for item, value in definition.items()
            if item in {"type", "min", "max", "step", "unit"}
        }
        for key, definition in FIELD_DEFINITIONS.items()
    }


def execution_defaults():
    current_defaults = defaults()
    return {key: current_defaults[key] for key in WIRED_EXECUTION_SETTINGS}


def _persisted_names(keys):
    names = [f"{SETTING_PREFIX}{key}" for key in keys]
    names.extend(
        f"{SETTING_PREFIX}{legacy}"
        for key, legacy in LEGACY_SETTING_ALIASES.items()
        if key in keys
    )
    return names


def _normalize_payload_aliases(payload):
    normalized = dict(payload or {})
    for canonical, legacy in LEGACY_SETTING_ALIASES.items():
        if legacy not in normalized:
            continue
        if canonical in normalized:
            raise StrategySettingsValidationError(
                f"use only {canonical}; {legacy} is a legacy alias"
            )
        normalized[canonical] = normalized.pop(legacy)
    return normalized


def _deserialize(value, definition):
    try:
        decoded = json.loads(value)
        return _coerce(decoded, definition)
    except (TypeError, ValueError, json.JSONDecodeError, StrategySettingsValidationError):
        return definition["default"]


def _coerce(value, definition):
    expected = definition["type"]
    if expected == "boolean":
        if not isinstance(value, bool):
            raise StrategySettingsValidationError("must be true or false")
        return value

    if isinstance(value, bool):
        raise StrategySettingsValidationError("must be a number")
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise StrategySettingsValidationError("must be a number") from exc

    if numeric < definition["min"] or numeric > definition["max"]:
        raise StrategySettingsValidationError(
            f"must be between {definition['min']} and {definition['max']}"
        )
    if expected == "integer":
        if not numeric.is_integer():
            raise StrategySettingsValidationError("must be a whole number")
        return int(numeric)
    return float(numeric)


def validate_settings(payload, *, require_all=False):
    if not isinstance(payload, dict):
        raise StrategySettingsValidationError("settings must be an object")
    payload = _normalize_payload_aliases(payload)
    unknown = sorted(set(payload) - set(FIELD_DEFINITIONS))
    if unknown:
        raise StrategySettingsValidationError(
            f"unknown strategy setting(s): {', '.join(unknown)}"
        )
    if require_all:
        missing = sorted(set(FIELD_DEFINITIONS) - set(payload))
        if missing:
            raise StrategySettingsValidationError(
                f"missing strategy setting(s): {', '.join(missing)}"
            )

    validated = {}
    for key, value in payload.items():
        try:
            validated[key] = _coerce(value, FIELD_DEFINITIONS[key])
        except StrategySettingsValidationError as exc:
            raise StrategySettingsValidationError(f"{key} {exc}") from exc

    if require_all:
        if validated["maximum_rr"] < validated["minimum_rr"]:
            raise StrategySettingsValidationError(
                "maximum_rr must be greater than or equal to minimum_rr"
            )
        if validated["ema_fast_period"] >= validated["ema_slow_period"]:
            raise StrategySettingsValidationError(
                "ema_fast_period must be less than ema_slow_period"
            )
    return validated


def _validate_execution_values(values):
    safe = execution_defaults()
    candidate = {**safe, **dict(values or {})}
    minimum_rr = _coerce(candidate["minimum_rr"], FIELD_DEFINITIONS["minimum_rr"])
    maximum_rr = _coerce(candidate["maximum_rr"], FIELD_DEFINITIONS["maximum_rr"])
    cooldown = _coerce(
        candidate["post_trade_cooldown_minutes"],
        FIELD_DEFINITIONS["post_trade_cooldown_minutes"],
    )
    bos_buffer_points = _coerce(
        candidate["bos_buffer_points"],
        FIELD_DEFINITIONS["bos_buffer_points"],
    )
    minimum_sl_distance_points = _coerce(
        candidate["minimum_sl_distance_points"],
        FIELD_DEFINITIONS["minimum_sl_distance_points"],
    )
    consolidation_filter_enabled = _coerce(
        candidate["consolidation_filter_enabled"],
        FIELD_DEFINITIONS["consolidation_filter_enabled"],
    )
    if maximum_rr < minimum_rr:
        raise StrategySettingsValidationError(
            "maximum_rr must be greater than or equal to minimum_rr"
        )
    return {
        "minimum_rr": minimum_rr,
        "maximum_rr": maximum_rr,
        "bos_buffer_points": bos_buffer_points,
        "minimum_sl_distance_points": minimum_sl_distance_points,
        "post_trade_cooldown_minutes": cooldown,
        "consolidation_filter_enabled": consolidation_filter_enabled,
    }


def _cache_key(factory):
    return factory


def invalidate_execution_settings_cache(session_factory=None):
    factory = session_factory or SessionLocal
    with _CACHE_LOCK:
        _EXECUTION_CACHE.pop(_cache_key(factory), None)


def _prime_execution_settings_cache(values, factory, *, source="runtime_setting", now=None):
    validated = _validate_execution_values(values)
    current_time = time.monotonic() if now is None else float(now)
    with _CACHE_LOCK:
        _EXECUTION_CACHE[_cache_key(factory)] = {
            "values": validated,
            "source": source,
            "expires_at": current_time + EXECUTION_CACHE_TTL_SECONDS,
        }
    return dict(validated)


def _read_execution_settings(factory):
    safe = execution_defaults()
    names = _persisted_names(WIRED_EXECUTION_SETTINGS)
    with factory() as session:
        rows = (
            session.query(RuntimeSetting)
            .filter(RuntimeSetting.setting_name.in_(names))
            .all()
        )
    by_name = {row.setting_name: row for row in rows}
    loaded = dict(safe)
    for key in WIRED_EXECUTION_SETTINGS:
        row = by_name.get(f"{SETTING_PREFIX}{key}")
        if row is None and key in LEGACY_SETTING_ALIASES:
            row = by_name.get(
                f"{SETTING_PREFIX}{LEGACY_SETTING_ALIASES[key]}"
            )
        if row is None:
            continue
        try:
            loaded[key] = _coerce(
                json.loads(row.setting_value),
                FIELD_DEFINITIONS[key],
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise StrategySettingsValidationError(
                f"malformed persisted {key}"
            ) from exc
    return _validate_execution_values(loaded)


def get_cached_execution_settings(
    session_factory=None,
    *,
    force_refresh=False,
    monotonic_now=None,
):
    """Return wired settings for normal strategy evaluation.

    Strategy evaluation may use the short-lived cache. LIVE RR validation does
    not use this path; get_configured_rr_window() always performs an
    authoritative database read and fails closed if that read cannot complete.
    """
    factory = session_factory or SessionLocal
    current_time = time.monotonic() if monotonic_now is None else float(monotonic_now)
    cache_key = _cache_key(factory)
    with _CACHE_LOCK:
        cached = _EXECUTION_CACHE.get(cache_key)
        if (
            not force_refresh
            and cached is not None
            and current_time < cached["expires_at"]
        ):
            return dict(cached["values"])

    with _LOCK:
        with _CACHE_LOCK:
            cached = _EXECUTION_CACHE.get(cache_key)
            if (
                not force_refresh
                and cached is not None
                and current_time < cached["expires_at"]
            ):
                return dict(cached["values"])
        try:
            values = _read_execution_settings(factory)
            source = "runtime_setting"
            ttl = EXECUTION_CACHE_TTL_SECONDS
        except Exception as exc:
            values = execution_defaults()
            source = "safe_defaults"
            ttl = EXECUTION_FAILURE_CACHE_TTL_SECONDS
            logger.warning("STRATEGY_SETTINGS_FALLBACK_TO_DEFAULTS %s", {
                "error_type": type(exc).__name__,
                "error": str(exc),
                "fallback": values,
            })

        with _CACHE_LOCK:
            _EXECUTION_CACHE[cache_key] = {
                "values": dict(values),
                "source": source,
                "expires_at": current_time + ttl,
            }
    return dict(values)


def get_configured_rr_window(session_factory=None):
    """Return the authoritative RR window used by LIVE execution.

    This deliberately bypasses the in-process cache. A LIVE order must never
    be allowed because one worker still has an older Minimum RR value cached.
    Database/read/validation failures are allowed to propagate so execution
    fails closed instead of silently falling back to the looser 1.20 default.
    """
    factory = session_factory or SessionLocal
    with _LOCK:
        configured = _read_execution_settings(factory)
        _prime_execution_settings_cache(configured, factory)
    minimum_rr = float(configured["minimum_rr"])
    maximum_rr = float(configured["maximum_rr"])
    logger.info("LIVE_RR_AUTHORITATIVE_SETTINGS %s", {
        "minimum_rr": minimum_rr,
        "maximum_rr": maximum_rr,
        "source": "runtime_setting",
    })
    return minimum_rr, maximum_rr


def get_configured_cooldown_seconds(session_factory=None):
    configured = get_cached_execution_settings(session_factory)
    return int(configured["post_trade_cooldown_minutes"]) * 60


def get_strategy_settings(session_factory=None):
    factory = session_factory or SessionLocal
    current = defaults()
    latest = None
    with factory() as session:
        rows = (
            session.query(RuntimeSetting)
            .filter(RuntimeSetting.setting_name.in_(
                _persisted_names(FIELD_DEFINITIONS)
            ))
            .all()
        )
        canonical_rows = {
            row.setting_name[len(SETTING_PREFIX):]: row
            for row in rows
            if row.setting_name[len(SETTING_PREFIX):] in FIELD_DEFINITIONS
        }
        for row in rows:
            key = row.setting_name[len(SETTING_PREFIX):]
            for canonical, legacy in LEGACY_SETTING_ALIASES.items():
                if key == legacy and canonical not in canonical_rows:
                    key = canonical
                    break
            if key not in WIRED_EXECUTION_SETTINGS:
                continue
            current[key] = _deserialize(row.setting_value, FIELD_DEFINITIONS[key])
            if latest is None or row.updated_at > latest:
                latest = row.updated_at

    try:
        validate_settings(current, require_all=True)
    except StrategySettingsValidationError:
        current = defaults()

    return {
        "current": current,
        "defaults": defaults(),
        "limits": validation_limits(),
        "last_updated": _utc_iso(latest),
        "execution_wiring": {
            key: key in WIRED_EXECUTION_SETTINGS for key in FIELD_DEFINITIONS
        },
        "fixed_rules": {
            "ema_trend_filter": {"enabled": True, "fast_period": 9, "slow_period": 21},
            "m15_closed_break_required": True,
            "later_m5_confirmation_required": True,
            "fresh_bos_after_consolidation_required": True,
            "atr_bos_buffer_fraction": 0.10,
            "atr_period": 14,
        },
        "phase": "V1_COMPLETE",
    }


def get_strategy_settings_history(*, limit=50, offset=0, session_factory=None):
    """Return a bounded, newest-first view of append-only strategy changes."""
    factory = session_factory or SessionLocal
    safe_limit = max(1, min(int(limit), 100))
    safe_offset = max(0, int(offset))
    with factory() as session:
        query = session.query(StrategySettingAudit)
        total = query.count()
        rows = (
            query.order_by(
                StrategySettingAudit.updated_at.desc(),
                StrategySettingAudit.id.desc(),
            )
            .offset(safe_offset)
            .limit(safe_limit)
            .all()
        )
    return {
        "items": [
            {
                "setting_name": row.setting_name,
                "previous_value": json.loads(row.previous_value),
                "new_value": json.loads(row.new_value),
                "changed_at": _utc_iso(row.updated_at),
                "changed_by": row.updated_by,
            }
            for row in rows
        ],
        "count": len(rows),
        "total": total,
        "limit": safe_limit,
        "offset": safe_offset,
        "read_only": True,
    }


def save_strategy_settings(payload, updated_by, session_factory=None, now=None):
    validated = validate_settings(payload)
    fixed = sorted(set(validated) - EDITABLE_STRATEGY_SETTINGS)
    if fixed:
        raise StrategySettingsValidationError(
            f"fixed strategy setting(s) are read-only: {', '.join(fixed)}"
        )
    factory = session_factory or SessionLocal
    updated_at = now or datetime.now(timezone.utc)
    with _LOCK:
        with factory() as session:
            rows = (
                session.query(RuntimeSetting)
                .filter(RuntimeSetting.setting_name.in_(
                    _persisted_names(FIELD_DEFINITIONS)
                ))
                .all()
            )
            by_name = {row.setting_name: row for row in rows}
            existing = {
                key: by_name.get(f"{SETTING_PREFIX}{key}")
                for key in FIELD_DEFINITIONS
            }
            merged = defaults()
            for key, row in existing.items():
                source_row = row
                if source_row is None and key in LEGACY_SETTING_ALIASES:
                    source_row = by_name.get(
                        f"{SETTING_PREFIX}{LEGACY_SETTING_ALIASES[key]}"
                    )
                if source_row is not None:
                    merged[key] = _deserialize(
                        source_row.setting_value,
                        FIELD_DEFINITIONS[key],
                    )
            previous = dict(merged)
            merged.update(validated)
            validate_settings(merged, require_all=True)

            for key in validated:
                value = merged[key]
                row = existing.get(key)
                if row is None:
                    row = RuntimeSetting(setting_name=f"{SETTING_PREFIX}{key}")
                    session.add(row)
                row.setting_value = json.dumps(value, separators=(",", ":"))
                row.updated_at = updated_at
                row.updated_by = str(updated_by or "user")
            for key in WIRED_EXECUTION_SETTINGS:
                if previous[key] == merged[key]:
                    continue
                session.add(StrategySettingAudit(
                    setting_name=key,
                    previous_value=json.dumps(previous[key], separators=(",", ":")),
                    new_value=json.dumps(merged[key], separators=(",", ":")),
                    updated_at=updated_at,
                    updated_by=str(updated_by or "user"),
                ))
            session.commit()
        _prime_execution_settings_cache(merged, factory)
    return get_strategy_settings(factory)


def reset_strategy_settings(*, confirmed, updated_by, session_factory=None, now=None):
    if confirmed is not True:
        raise StrategySettingsValidationError(
            "reset requires explicit confirmation"
        )
    return save_strategy_settings(
        execution_defaults(),
        updated_by=updated_by,
        session_factory=session_factory,
        now=now,
    )
