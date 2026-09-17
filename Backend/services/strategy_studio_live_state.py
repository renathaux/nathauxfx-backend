"""Read-only Strategy Studio LIVE handoff state.

The durable gate defaults OFF.  This module intentionally has no broker,
execution, or LIVE Auto imports; it only reads owner-scoped handoff metadata.
"""
from __future__ import annotations

from db import SessionLocal
from models import StrategyStudioLiveState


def _owner(owner_id) -> str:
    value = str(owner_id or "").strip()
    if not value:
        raise ValueError("owner_id is required")
    return value


def get_studio_live_state(owner_id, session_factory=None) -> dict:
    owner = _owner(owner_id)
    factory = session_factory or SessionLocal
    with factory() as session:
        row = session.get(StrategyStudioLiveState, owner)
        if row is None:
            return {
                "owner_id": owner,
                "enabled": False,
                "enabled_strategy_id": None,
                "enabled_at": None,
                "updated_at": None,
            }
        return {
            "owner_id": owner,
            "enabled": bool(row.enabled),
            "enabled_strategy_id": row.enabled_strategy_id,
            "enabled_at": row.enabled_at.isoformat() if row.enabled_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }


def studio_live_enabled(owner_id, session_factory=None) -> bool:
    return bool(get_studio_live_state(owner_id, session_factory).get("enabled"))


def get_enabled_studio_live_owner(session_factory=None) -> str | None:
    """Return the sole enabled owner, otherwise fail closed.

    The background execution loop has no request/user context.  It may use a
    Studio strategy only when durable state identifies exactly one enabled
    owner. Zero or multiple enabled owners returns None so V3B remains the
    authority rather than guessing which owner should trade.
    """
    factory = session_factory or SessionLocal
    with factory() as session:
        rows = (
            session.query(StrategyStudioLiveState)
            .filter(StrategyStudioLiveState.enabled.is_(True))
            .order_by(StrategyStudioLiveState.owner_id.asc())
            .limit(2)
            .all()
        )
    if len(rows) != 1:
        return None
    return str(rows[0].owner_id)
