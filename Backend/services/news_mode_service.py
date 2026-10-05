"""Durable, backend-authoritative News Trading Mode settings."""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timezone

from db import SessionLocal
from models import NewsTradingModeAudit, RuntimeSetting


SETTING_NAME = "news_trading_mode"
ALLOWED_MODES = frozenset({"OFF", "BLOCK_ONLY", "TRADE_CONFIRMED"})
_LOCK = threading.RLock()
logger = logging.getLogger("flowsignal.news_mode")

class InvalidNewsTradingMode(ValueError):
    pass


def normalize_mode(value):
    mode = str(value or "").strip().upper()
    if mode not in ALLOWED_MODES:
        raise InvalidNewsTradingMode(
            "News trading mode must be OFF, BLOCK_ONLY, or TRADE_CONFIRMED."
        )
    return mode


def _utc_iso(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def get_effective_mode(session_factory=None):
    factory = session_factory or SessionLocal
    with factory() as session:
        row = session.get(RuntimeSetting, SETTING_NAME)
        if row is not None:
            try:
                mode = normalize_mode(row.setting_value)
            except InvalidNewsTradingMode:
                logger.error("Invalid persisted news mode ignored: %r", row.setting_value)
            else:
                return {
                    "mode": mode,
                    "updated_at": _utc_iso(row.updated_at),
                    "updated_by": row.updated_by,
                    "source": "runtime_setting",
                }

    environment = str(os.getenv("NEWS_TRADING_MODE", "")).strip().upper()
    if environment in ALLOWED_MODES:
        return {
            "mode": environment,
            "updated_at": None,
            "updated_by": "environment",
            "source": "environment",
        }
    return {
        "mode": "OFF",
        "updated_at": None,
        "updated_by": "system",
        "source": "default",
    }


def save_mode(mode, updated_by, session_factory=None, now=None):
    normalized = normalize_mode(mode)
    updated_at = now or datetime.now(timezone.utc)
    factory = session_factory or SessionLocal
    with _LOCK:
        with factory() as session:
            row = session.get(RuntimeSetting, SETTING_NAME)
            if row is None:
                row = RuntimeSetting(setting_name=SETTING_NAME)
                session.add(row)
            row.setting_value = normalized
            row.updated_at = updated_at
            row.updated_by = str(updated_by or "user")
            session.commit()
    return {
        "mode": normalized,
        "updated_at": _utc_iso(updated_at),
        "updated_by": str(updated_by or "user"),
        "source": "runtime_setting",
    }


def record_audit(
    *, previous_mode, new_mode, user_id, active_broker_account,
    broker_environment, request_source, success, failure_reason=None,
    session_factory=None, now=None,
):
    factory = session_factory or SessionLocal
    timestamp = now or datetime.now(timezone.utc)
    with _LOCK:
        with factory() as session:
            session.add(NewsTradingModeAudit(
                previous_mode=str(previous_mode or "UNKNOWN"),
                new_mode=str(new_mode or "UNKNOWN"),
                user_id=str(user_id or "unknown"),
                active_broker_account=(
                    str(active_broker_account)
                    if active_broker_account not in (None, "") else None
                ),
                broker_environment=str(broker_environment or "unknown").lower(),
                timestamp=timestamp,
                request_source=str(request_source or "api"),
                success=bool(success),
                failure_reason=str(failure_reason) if failure_reason else None,
            ))
            session.commit()
    logger.info("NEWS_TRADING_MODE_CHANGE %s", {
        "previous_mode": previous_mode,
        "new_mode": new_mode,
        "user_id": user_id,
        "active_broker_account": active_broker_account,
        "broker_environment": broker_environment,
        "timestamp": _utc_iso(timestamp),
        "request_source": request_source,
        "success": bool(success),
        "failure_reason": failure_reason,
    })
