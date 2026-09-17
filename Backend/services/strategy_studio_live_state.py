"""Durable Strategy Studio LIVE handoff state and fail-closed safety checks.

The gate defaults OFF. This module never places broker orders, changes the
selected cTrader account, or mutates the separate LIVE Auto preference.
"""
from __future__ import annotations

from datetime import datetime, timezone

from db import SessionLocal
from models import StrategySetupLifecycle, StrategyStudioLiveState, TradeSubmissionAttempt


UNRESOLVED_ATTEMPT_STATUSES = {
    "SUBMITTING",
    "RECONCILIATION_REQUIRED",
    "AMBIGUOUS",
    "ACCEPTED_PROTECTION_FAILED",
}
UNRESOLVED_RECONCILIATION_STATUSES = {
    "REQUIRED",
    "PENDING",
    "RECONCILIATION_REQUIRED",
    "AMBIGUOUS",
}


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


def set_studio_live_state(
    owner_id,
    strategy_id,
    enabled,
    confirmed,
    session_factory=None,
    *,
    now=None,
) -> dict:
    """Persist only the Studio LIVE handoff gate after explicit confirmation."""
    owner = _owner(owner_id)
    if confirmed is not True:
        raise ValueError("Strategy Studio LIVE handoff confirmation is required")
    enabled = bool(enabled)
    strategy = str(strategy_id or "").strip()
    if enabled and not strategy:
        raise ValueError("Active Strategy Studio strategy is required")

    factory = session_factory or SessionLocal
    changed_at = now or datetime.now(timezone.utc)
    session = factory()
    try:
        row = (
            session.query(StrategyStudioLiveState)
            .filter(StrategyStudioLiveState.owner_id == owner)
            .with_for_update()
            .one_or_none()
        )
        if row is None:
            row = StrategyStudioLiveState(
                owner_id=owner,
                enabled=False,
                enabled_strategy_id=None,
                enabled_at=None,
                updated_at=changed_at,
            )
            session.add(row)
            session.flush()

        row.enabled = enabled
        row.enabled_strategy_id = strategy if enabled else None
        row.enabled_at = changed_at if enabled else None
        row.updated_at = changed_at
        session.commit()
        return {
            "owner_id": owner,
            "enabled": bool(row.enabled),
            "enabled_strategy_id": row.enabled_strategy_id,
            "enabled_at": row.enabled_at.isoformat() if row.enabled_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def has_unresolved_studio_reconciliation(owner_id, session_factory=None) -> bool:
    """Fail closed while any Studio submission/lifecycle is unresolved."""
    owner = _owner(owner_id)
    factory = session_factory or SessionLocal
    with factory() as session:
        lifecycle_pending = session.query(StrategySetupLifecycle.setup_id).filter(
            StrategySetupLifecycle.owner_id == owner,
            StrategySetupLifecycle.status == "RECONCILIATION_REQUIRED",
        ).first()
        if lifecycle_pending is not None:
            return True

        attempts = session.query(TradeSubmissionAttempt).filter(
            TradeSubmissionAttempt.owner_id == owner,
            TradeSubmissionAttempt.lifecycle_kind == "STRATEGY_STUDIO",
        ).all()
        for attempt in attempts:
            if str(attempt.attempt_status or "").upper() in UNRESOLVED_ATTEMPT_STATUSES:
                return True
            if str(attempt.reconciliation_status or "").upper() in UNRESOLVED_RECONCILIATION_STATUSES:
                return True
    return False


def studio_live_enabled(owner_id, session_factory=None) -> bool:
    return bool(get_studio_live_state(owner_id, session_factory).get("enabled"))


def get_enabled_studio_live_owner(session_factory=None) -> str | None:
    """Return the sole enabled owner; reject ambiguous enabled state.

    Zero enabled owners means the Studio gate is OFF and V3B remains the LIVE
    authority. More than one enabled owner is an invalid durable state and must
    fail closed rather than silently falling back to another candidate source.
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
    if not rows:
        return None
    if len(rows) != 1:
        raise RuntimeError("STRATEGY_STUDIO_LIVE_OWNER_AMBIGUOUS")
    return str(rows[0].owner_id)
