"""Owner-scoped Strategy Studio CRUD and Studio-only activation metadata."""
from __future__ import annotations

import copy
import uuid
from datetime import datetime, timezone

import ctrader_account_context as account_context
from db import SessionLocal
from models import StrategySetupLifecycle, StrategyStudioLiveState
from services.strategy_studio_models import SavedStrategy, StrategyStudioSelection
from services.strategy_studio_schema import normalize_definition, strategy_summary


class StrategyStudioError(ValueError):
    pass


class StrategyStudioNotFound(StrategyStudioError):
    pass


class StrategyStudioConflict(StrategyStudioError):
    pass


def _factory(session_factory=None):
    return session_factory or SessionLocal


def _utc_now(now=None):
    return now or datetime.now(timezone.utc)


def _clean_owner(owner_id):
    value = str(owner_id or "").strip()
    if not value:
        raise StrategyStudioError("owner_id is required")
    return value


def _clean_name(name):
    value = str(name or "").strip()
    if not value:
        raise StrategyStudioError("strategy name is required")
    if len(value) > 120:
        raise StrategyStudioError("strategy name must be 120 characters or fewer")
    return value


def _selection_for_update(session, owner_id):
    return (
        session.query(StrategyStudioSelection)
        .filter(StrategyStudioSelection.owner_id == owner_id)
        .with_for_update()
        .one_or_none()
    )


def _strategy_row(session, owner_id, strategy_id, *, for_update=False):
    query = session.query(SavedStrategy).filter(
        SavedStrategy.strategy_id == str(strategy_id),
        SavedStrategy.owner_id == owner_id,
    )
    if for_update:
        query = query.with_for_update()
    row = query.one_or_none()
    if row is None:
        raise StrategyStudioNotFound("strategy not found")
    return row


def _live_lock_state(session, owner_id, strategy_id):
    state = session.get(StrategyStudioLiveState, str(owner_id))
    handoff_enabled = bool(
        state
        and state.enabled
        and str(state.enabled_strategy_id or "") == str(strategy_id)
    )
    if not handoff_enabled:
        return {"locked": False, "live_handoff_enabled": False}

    identity = account_context.current_identity() or account_context.selected_identity()
    if identity is None:
        return {"locked": False, "live_handoff_enabled": True}

    lifecycle = session.query(StrategySetupLifecycle).filter(
        StrategySetupLifecycle.owner_id == str(owner_id),
        StrategySetupLifecycle.strategy_id == str(strategy_id),
        StrategySetupLifecycle.account_scope == str(identity.scope),
        StrategySetupLifecycle.broker_position_id.is_not(None),
        StrategySetupLifecycle.status.in_({"CONSUMED", "SUBMITTING", "RECONCILIATION_REQUIRED"}),
    ).first()
    return {
        "locked": lifecycle is not None,
        "live_handoff_enabled": True,
    }


def _serialize(row, active_strategy_id=None, *, locked=False, live_handoff_enabled=False):
    definition = copy.deepcopy(row.definition_json)
    return {
        "strategy_id": row.strategy_id,
        "name": row.name,
        "schema_version": row.schema_version,
        "definition": definition,
        "state": "ACTIVE" if row.strategy_id == active_strategy_id else "INACTIVE",
        "summary": strategy_summary(definition),
        "locked": bool(locked),
        "live_handoff_enabled": bool(live_handoff_enabled),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _serialize_with_state(session, row, active_strategy_id=None):
    lock = _live_lock_state(session, row.owner_id, row.strategy_id)
    return _serialize(row, active_strategy_id, **lock)


def _require_unlocked(session, owner_id, strategy_id):
    if _live_lock_state(session, owner_id, strategy_id)["locked"]:
        raise StrategyStudioConflict(
            "strategy is locked while its Studio-managed position is open"
        )


def list_strategies(owner_id, session_factory=None):
    owner = _clean_owner(owner_id)
    factory = _factory(session_factory)
    with factory() as session:
        selection = session.get(StrategyStudioSelection, owner)
        active_id = selection.strategy_id if selection else None
        rows = (
            session.query(SavedStrategy)
            .filter(SavedStrategy.owner_id == owner)
            .order_by(SavedStrategy.updated_at.desc(), SavedStrategy.strategy_id.desc())
            .all()
        )
        return [_serialize_with_state(session, row, active_id) for row in rows]


def get_strategy(owner_id, strategy_id, session_factory=None):
    owner = _clean_owner(owner_id)
    factory = _factory(session_factory)
    with factory() as session:
        row = _strategy_row(session, owner, strategy_id)
        selection = session.get(StrategyStudioSelection, owner)
        active_id = selection.strategy_id if selection else None
        return _serialize_with_state(session, row, active_id)


def create_strategy(owner_id, name, definition, session_factory=None, *, now=None):
    owner = _clean_owner(owner_id)
    clean_name = _clean_name(name)
    normalized = normalize_definition(copy.deepcopy(definition))
    created_at = _utc_now(now)
    factory = _factory(session_factory)
    strategy_id = "strat_" + uuid.uuid4().hex
    with factory() as session:
        row = SavedStrategy(
            strategy_id=strategy_id,
            owner_id=owner,
            name=clean_name,
            schema_version=int(normalized["schema_version"]),
            definition_json=normalized,
            created_at=created_at,
            updated_at=created_at,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return _serialize(row, None)


def update_strategy(owner_id, strategy_id, name, definition, session_factory=None, *, now=None):
    owner = _clean_owner(owner_id)
    clean_name = _clean_name(name)
    normalized = normalize_definition(copy.deepcopy(definition))
    changed_at = _utc_now(now)
    factory = _factory(session_factory)
    with factory() as session:
        row = _strategy_row(session, owner, strategy_id, for_update=True)
        _require_unlocked(session, owner, row.strategy_id)
        selection = _selection_for_update(session, owner)
        if selection and selection.strategy_id == row.strategy_id:
            raise StrategyStudioConflict("deactivate the strategy before editing it")
        row.name = clean_name
        row.schema_version = int(normalized["schema_version"])
        row.definition_json = normalized
        row.updated_at = changed_at
        session.commit()
        session.refresh(row)
        return _serialize(row, None)


def clone_strategy(owner_id, strategy_id, clone_name, session_factory=None, *, now=None):
    owner = _clean_owner(owner_id)
    clean_name = _clean_name(clone_name)
    factory = _factory(session_factory)
    with factory() as session:
        source = _strategy_row(session, owner, strategy_id)
        definition = copy.deepcopy(source.definition_json)
    return create_strategy(owner, clean_name, definition, factory, now=now)


def delete_strategy(owner_id, strategy_id, confirmed, session_factory=None):
    if confirmed is not True:
        raise StrategyStudioConflict("delete requires explicit confirmation")
    owner = _clean_owner(owner_id)
    factory = _factory(session_factory)
    with factory() as session:
        row = _strategy_row(session, owner, strategy_id, for_update=True)
        _require_unlocked(session, owner, row.strategy_id)
        selection = _selection_for_update(session, owner)
        if selection and selection.strategy_id == row.strategy_id:
            raise StrategyStudioConflict("deactivate the strategy before deleting it")
        session.delete(row)
        session.commit()
    return True


def activate_strategy(owner_id, strategy_id, confirmed, session_factory=None, *, now=None):
    if confirmed is not True:
        raise StrategyStudioConflict("activation requires explicit confirmation")
    owner = _clean_owner(owner_id)
    changed_at = _utc_now(now)
    factory = _factory(session_factory)
    with factory() as session:
        row = _strategy_row(session, owner, strategy_id)
        selection = _selection_for_update(session, owner)
        if selection is not None and selection.strategy_id != row.strategy_id:
            _require_unlocked(session, owner, selection.strategy_id)
        if selection is None:
            selection = StrategyStudioSelection(
                owner_id=owner,
                strategy_id=row.strategy_id,
                activated_at=changed_at,
                updated_at=changed_at,
            )
            session.add(selection)
        else:
            selection.strategy_id = row.strategy_id
            selection.activated_at = changed_at
            selection.updated_at = changed_at
        session.commit()
        session.refresh(row)
        return _serialize_with_state(session, row, row.strategy_id)


def deactivate_strategy(owner_id, strategy_id, confirmed, session_factory=None, *, now=None):
    if confirmed is not True:
        raise StrategyStudioConflict("deactivation requires explicit confirmation")
    owner = _clean_owner(owner_id)
    factory = _factory(session_factory)
    with factory() as session:
        row = _strategy_row(session, owner, strategy_id)
        _require_unlocked(session, owner, row.strategy_id)
        selection = _selection_for_update(session, owner)
        if selection is None or selection.strategy_id != row.strategy_id:
            raise StrategyStudioConflict("strategy is not the active Studio strategy")
        session.delete(selection)
        session.commit()
        session.refresh(row)
        return _serialize(row, None)
