"""Authenticated Strategy Studio CRUD and read-only parity diagnostics.

Strategy Studio parity/status endpoints are observation-only. They do not place
orders, alter LIVE Auto, switch accounts, or enable the LIVE handoff gate.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from ctrader_account_context import selected_identity
from services.customer_forex_guard import _bearer
from services.strategy_simulator_data_source import load_simulation_5m
from services.strategy_studio_live_state import get_studio_live_state
from services.strategy_studio_parity import compare_v3b_entry_decisions
from services.strategy_studio_schema import (
    normalize_definition,
    strategy_summary,
    validation_errors,
)
from services.strategy_studio_service import (
    StrategyStudioConflict,
    StrategyStudioError,
    StrategyStudioNotFound,
    activate_strategy,
    clone_strategy,
    create_strategy,
    deactivate_strategy,
    delete_strategy,
    get_strategy,
    list_strategies,
    update_strategy,
)
from services.user_auth_service import current_user, current_user_with_csrf


router = APIRouter(prefix="/strategy-studio", tags=["strategy-studio"])


class StrategyWriteRequest(BaseModel):
    name: str
    definition: dict


class StrategyCloneRequest(BaseModel):
    name: str


class ConfirmRequest(BaseModel):
    confirm: bool = False


class ParityRunRequest(BaseModel):
    symbol: str
    start: datetime
    end: datetime


def owner_key(actor):
    actor_id = getattr(actor, "id", None)
    if actor_id is not None:
        return f"user:{actor_id}"
    if isinstance(actor, dict):
        email = str(actor.get("email") or "legacy-admin").strip().lower()
    else:
        email = str(getattr(actor, "email", None) or "legacy-admin").strip().lower()
    return f"owner:{email}"


def _legacy_actor(request: Request, *, mutation: bool = False):
    del mutation
    try:
        import api
    except Exception:
        return None
    token = _bearer(request.headers)
    session = api.SESSIONS.get(token) if token else None
    if not isinstance(session, dict):
        return None
    if str(session.get("role") or "").lower() != "admin":
        return None
    return session


def _actor(request: Request, *, mutation: bool = False):
    resolver = current_user_with_csrf if mutation else current_user
    try:
        return resolver(request)
    except HTTPException as auth_error:
        legacy = _legacy_actor(request, mutation=mutation)
        if legacy is not None:
            return legacy
        raise auth_error


def _service_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, StrategyStudioNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, StrategyStudioConflict):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, (StrategyStudioError, ValueError)):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail="STRATEGY_STUDIO_ERROR")


@router.get("/strategies")
def strategies_list(request: Request):
    owner = owner_key(_actor(request))
    try:
        return {"ok": True, "strategies": list_strategies(owner)}
    except Exception as exc:
        raise _service_http_error(exc) from exc


@router.post("/validate")
def strategy_validate(payload: StrategyWriteRequest, request: Request):
    _actor(request, mutation=True)
    errors = {}
    name = str(payload.name or "").strip()
    if not name:
        errors["name"] = "Strategy name is required"
    elif len(name) > 120:
        errors["name"] = "Strategy name must be 120 characters or fewer"
    errors.update(validation_errors(payload.definition))

    normalized = None
    summary = None
    if not errors:
        normalized = normalize_definition(payload.definition)
        summary = strategy_summary(normalized)
    return {
        "ok": True,
        "valid": not errors,
        "errors": errors,
        "normalized_definition": normalized,
        "summary": summary,
        "live_handoff_enabled": False,
    }


@router.post("/strategies", status_code=status.HTTP_201_CREATED)
def strategy_create(payload: StrategyWriteRequest, request: Request):
    owner = owner_key(_actor(request, mutation=True))
    try:
        strategy = create_strategy(owner, payload.name, payload.definition)
        return {"ok": True, "strategy": strategy}
    except Exception as exc:
        raise _service_http_error(exc) from exc


@router.get("/strategies/{strategy_id}")
def strategy_get(strategy_id: str, request: Request):
    owner = owner_key(_actor(request))
    try:
        return {"ok": True, "strategy": get_strategy(owner, strategy_id)}
    except Exception as exc:
        raise _service_http_error(exc) from exc


@router.put("/strategies/{strategy_id}")
def strategy_update(strategy_id: str, payload: StrategyWriteRequest, request: Request):
    owner = owner_key(_actor(request, mutation=True))
    try:
        strategy = update_strategy(owner, strategy_id, payload.name, payload.definition)
        return {"ok": True, "strategy": strategy}
    except Exception as exc:
        raise _service_http_error(exc) from exc


@router.post("/strategies/{strategy_id}/clone", status_code=status.HTTP_201_CREATED)
def strategy_clone(strategy_id: str, payload: StrategyCloneRequest, request: Request):
    owner = owner_key(_actor(request, mutation=True))
    try:
        strategy = clone_strategy(owner, strategy_id, payload.name)
        return {"ok": True, "strategy": strategy}
    except Exception as exc:
        raise _service_http_error(exc) from exc


@router.post("/strategies/{strategy_id}/activate")
def strategy_activate(strategy_id: str, payload: ConfirmRequest, request: Request):
    owner = owner_key(_actor(request, mutation=True))
    try:
        strategy = activate_strategy(owner, strategy_id, payload.confirm)
        return {"ok": True, "strategy": strategy}
    except Exception as exc:
        raise _service_http_error(exc) from exc


@router.post("/strategies/{strategy_id}/deactivate")
def strategy_deactivate(strategy_id: str, payload: ConfirmRequest, request: Request):
    owner = owner_key(_actor(request, mutation=True))
    try:
        strategy = deactivate_strategy(owner, strategy_id, payload.confirm)
        return {"ok": True, "strategy": strategy}
    except Exception as exc:
        raise _service_http_error(exc) from exc


@router.delete("/strategies/{strategy_id}")
def strategy_delete(strategy_id: str, payload: ConfirmRequest, request: Request):
    owner = owner_key(_actor(request, mutation=True))
    try:
        deleted = delete_strategy(owner, strategy_id, payload.confirm)
        return {"ok": True, "deleted": bool(deleted), "strategy_id": strategy_id}
    except Exception as exc:
        raise _service_http_error(exc) from exc


@router.post("/parity/run")
def strategy_parity_run(payload: ParityRunRequest, request: Request):
    owner = owner_key(_actor(request))
    symbol = str(payload.symbol or "").upper().replace("/", "")
    if symbol not in {"EURUSD", "XAUUSD"}:
        raise HTTPException(status_code=400, detail="PARITY_SYMBOL_UNSUPPORTED")
    if payload.end <= payload.start:
        raise HTTPException(status_code=400, detail="PARITY_RANGE_INVALID")
    identity = selected_identity()
    if identity is None:
        raise HTTPException(status_code=409, detail="CTRADER_ACCOUNT_NOT_SELECTED")
    try:
        frame = load_simulation_5m(
            symbol,
            payload.start,
            payload.end,
            stream_scope=identity.scope,
        )
        report = compare_v3b_entry_decisions(
            symbol,
            frame,
            account_scope=identity.scope,
        )
        state = get_studio_live_state(owner)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "ok": True,
        **report,
        "live_handoff_enabled": bool(state.get("enabled")),
    }


@router.get("/live-status")
def strategy_live_status(request: Request):
    owner = owner_key(_actor(request))
    state = get_studio_live_state(owner)
    return {
        "ok": True,
        **state,
        "parity_status": "REQUIRES_VERIFICATION",
        "entry_parity_only": True,
        "post_entry_management_compared": False,
    }
