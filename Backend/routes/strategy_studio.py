"""Authenticated Strategy Studio CRUD, parity diagnostics, and gated LIVE handoff.

The LIVE handoff endpoint only mutates StrategyStudioLiveState after explicit
confirmation and fresh safety checks. It never places orders, alters LIVE Auto,
or switches broker accounts.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from ctrader_account_context import selected_identity
from services.customer_forex_guard import _bearer
from services.strategy_simulator_data_source import load_simulation_5m
from services.strategy_studio_live_state import (
    get_studio_live_state,
    has_unresolved_studio_reconciliation,
    set_studio_live_state,
)
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
PARITY_LOOKBACK_DAYS = 7


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


class LiveHandoffRequest(BaseModel):
    enabled: bool
    confirm: bool = False
    strategy_id: str | None = None


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


def _active_strategy(owner: str):
    strategies = list_strategies(owner)
    return next(
        (item for item in strategies if str(item.get("state") or "").upper() == "ACTIVE"),
        None,
    )


def _parity_summary(report: dict) -> dict:
    mismatches = list(report.get("mismatches") or [])
    return {
        "match": bool(report.get("match")),
        "compared_setups": int(report.get("compared_setups") or 0),
        "mismatch_count": len(mismatches),
        "mismatches": mismatches[:20],
        "account_scope": report.get("account_scope"),
        "parity_scope": report.get("parity_scope"),
        "post_entry_management_compared": bool(
            report.get("post_entry_management_compared", False)
        ),
    }


def _unavailable_readiness(reason: str) -> dict:
    return {
        "ready": False,
        "parity_verified": False,
        "parity_status": "REQUIRES_VERIFICATION",
        "active_strategy_id": None,
        "configured_symbols": [],
        "account_scope": None,
        "unresolved_reconciliation": False,
        "reports": {},
        "reason": reason,
    }


def evaluate_live_handoff_readiness(owner: str) -> dict:
    # Status polling also computes historical parity. Share admission with FAST
    # without locking broker execution or the handoff-disable action.
    from services.heavy_replay_admission import heavy_replay_lease, HeavyReplayBusy
    try:
        with heavy_replay_lease():
            return _evaluate_live_handoff_readiness(owner)
    except HeavyReplayBusy:
        return _unavailable_readiness("HEAVY_BACKTEST_BUSY")


def _evaluate_live_handoff_readiness(owner: str) -> dict:
    """Recompute entry parity from durable selected-account candles.

    Readiness is deliberately not persisted: every status read and every enable
    request recomputes against the currently selected account, preventing stale
    parity evidence from authorizing a different account or later data state.
    """
    active = _active_strategy(owner)
    if not active:
        return {
            **_unavailable_readiness("STRATEGY_STUDIO_ACTIVE_STRATEGY_REQUIRED"),
            "unresolved_reconciliation": has_unresolved_studio_reconciliation(owner),
        }

    definition = active.get("definition") or {}
    symbols = [
        str(symbol).upper().replace("/", "")
        for symbol in (definition.get("symbols") or [])
        if str(symbol or "").strip()
    ]
    identity = selected_identity()
    unresolved = has_unresolved_studio_reconciliation(owner)
    if identity is None:
        return {
            "ready": False,
            "parity_verified": False,
            "parity_status": "REQUIRES_VERIFICATION",
            "active_strategy_id": active.get("strategy_id"),
            "configured_symbols": symbols,
            "account_scope": None,
            "unresolved_reconciliation": unresolved,
            "reports": {},
            "reason": "CTRADER_ACCOUNT_NOT_SELECTED",
        }

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=PARITY_LOOKBACK_DAYS)
    reports = {}
    parity_verified = bool(symbols)
    parity_error = None
    for symbol in symbols:
        try:
            frame = load_simulation_5m(
                symbol,
                start,
                end,
                stream_scope=identity.scope,
            )
            report = compare_v3b_entry_decisions(
                symbol,
                frame,
                account_scope=identity.scope,
            )
            reports[symbol] = _parity_summary(report)
            parity_verified = parity_verified and bool(report.get("match"))
        except Exception as exc:
            parity_verified = False
            parity_error = str(exc)
            reports[symbol] = {
                "match": False,
                "compared_setups": 0,
                "mismatch_count": 0,
                "mismatches": [],
                "account_scope": identity.scope,
                "error": str(exc),
            }

    reason = None
    if unresolved:
        reason = "STRATEGY_STUDIO_RECONCILIATION_UNRESOLVED"
    elif not parity_verified:
        reason = (
            "STRATEGY_STUDIO_PARITY_UNAVAILABLE"
            if parity_error
            else "STRATEGY_STUDIO_PARITY_NOT_VERIFIED"
        )

    return {
        "ready": bool(parity_verified and not unresolved),
        "parity_verified": bool(parity_verified),
        "parity_status": "VERIFIED" if parity_verified else (
            "ERROR" if parity_error else "MISMATCH"
        ),
        "active_strategy_id": active.get("strategy_id"),
        "configured_symbols": symbols,
        "account_scope": identity.scope,
        "unresolved_reconciliation": bool(unresolved),
        "reports": reports,
        "reason": reason,
        "parity_window": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "lookback_days": PARITY_LOOKBACK_DAYS,
        },
    }


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
    try:
        readiness = evaluate_live_handoff_readiness(owner)
    except Exception as exc:
        readiness = _unavailable_readiness(
            f"STRATEGY_STUDIO_READINESS_UNAVAILABLE: {exc}"
        )
    return {
        "ok": True,
        **state,
        **readiness,
        "entry_parity_only": True,
        "post_entry_management_compared": False,
    }


@router.post("/live-handoff")
def strategy_live_handoff(payload: LiveHandoffRequest, request: Request):
    owner = owner_key(_actor(request, mutation=True))
    if payload.confirm is not True:
        raise HTTPException(
            status_code=400,
            detail="Strategy Studio LIVE handoff confirmation is required",
        )

    requested_strategy_id = str(payload.strategy_id or "").strip() or None
    if not payload.enabled:
        try:
            state = set_studio_live_state(
                owner,
                requested_strategy_id,
                False,
                True,
            )
        except Exception as exc:
            raise _service_http_error(exc) from exc
        return {
            "ok": True,
            "state": state,
            "live_auto_trade_changed": False,
            "broker_order_submitted": False,
        }

    readiness = evaluate_live_handoff_readiness(owner)
    if readiness.get("reason") == "HEAVY_BACKTEST_BUSY":
        raise HTTPException(status_code=429, detail="HEAVY_BACKTEST_BUSY")
    active_strategy_id = readiness.get("active_strategy_id")
    if not active_strategy_id:
        raise HTTPException(
            status_code=409,
            detail="STRATEGY_STUDIO_ACTIVE_STRATEGY_REQUIRED",
        )
    if requested_strategy_id and requested_strategy_id != active_strategy_id:
        raise HTTPException(
            status_code=409,
            detail="Requested strategy is not the active Strategy Studio strategy",
        )
    if readiness.get("unresolved_reconciliation"):
        raise HTTPException(
            status_code=409,
            detail="STRATEGY_STUDIO_RECONCILIATION_UNRESOLVED",
        )
    if not readiness.get("parity_verified"):
        raise HTTPException(
            status_code=409,
            detail=readiness.get("reason") or "STRATEGY_STUDIO_PARITY_NOT_VERIFIED",
        )
    if not readiness.get("ready"):
        raise HTTPException(
            status_code=409,
            detail=readiness.get("reason") or "STRATEGY_STUDIO_LIVE_NOT_READY",
        )

    try:
        state = set_studio_live_state(
            owner,
            active_strategy_id,
            True,
            True,
        )
    except Exception as exc:
        raise _service_http_error(exc) from exc
    return {
        "ok": True,
        "state": state,
        "readiness": readiness,
        "live_auto_trade_changed": False,
        "broker_order_submitted": False,
    }
