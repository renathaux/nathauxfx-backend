"""Read-only lookup helpers for immutable indicator events."""
from __future__ import annotations

import copy
from datetime import timezone

from db import SessionLocal
from models import IndicatorEvent


def _iso_utc(value):
    if value is None:
        return None
    try:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        else:
            value = value.astimezone(timezone.utc)
        return value.isoformat()
    except Exception:
        return str(value)


def get_indicator_event(event_id, *, session_factory=None):
    """Return one immutable indicator event without mutating stream state."""
    if not event_id:
        return None
    factory = session_factory or SessionLocal
    session = factory()
    try:
        row = session.get(IndicatorEvent, str(event_id))
        if row is None:
            return None
        return {
            "event_id": row.event_id,
            "symbol": row.symbol,
            "timeframe": row.timeframe,
            "candle_timestamp": _iso_utc(row.candle_timestamp),
            "classification": row.classification,
            "direction": row.direction,
            "broken_level": row.broken_level,
            "identity": copy.deepcopy(row.identity or {}),
            "payload": copy.deepcopy(row.payload or {}),
            "configuration_version": row.configuration_version,
            "is_historical": bool(row.is_historical),
        }
    except Exception:
        return None
    finally:
        session.close()
