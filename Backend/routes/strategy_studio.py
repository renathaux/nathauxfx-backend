"""Authenticated Strategy Studio CRUD, parity diagnostics, and gated LIVE handoff.

The LIVE handoff endpoint only mutates StrategyStudioLiveState after explicit
confirmation and fresh safety checks. It never places orders, alters LIVE Auto,
or switches broker accounts.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
import os

from fastapi.routing import APIRoute
from starlette.concurrency import run_in_threadpool

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from ctrader_account_context import selected_identity
from services.customer_forex_guard import _bearer
from services.strategy_simulator import run_simulation
from services.strategy_simulator_data_source import load_market_bundle, load_simulation_5m
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
from services.strategy_studio_owner import canonical_strategy_owner


def owner_debug_snapshot(owner):
    """Read metadata only; never initialize or change LIVE/selection state."""
    from db import SessionLocal, engine
    from models import StrategyStudioLiveState
    from services.strategy_studio_models import SavedStrategy, StrategyStudioSelection
    with SessionLocal() as session:
        count = session.query(SavedStrategy).filter(SavedStrategy.owner_id == owner).count()
        selection = session.get(StrategyStudioSelection, owner)
        live = session.get(StrategyStudioLiveState, owner)
        return {
            "strategy_count": count,
            "database_host": engine.url.host,
            "database_name": engine.url.database,
            "active_strategy_id": selection.strategy_id if selection else None,
            "live_strategy_id": live.enabled_strategy_id if live and live.enabled else None,
        }


class StrategyOwnerDiagnosticRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        async def traced(request):
            response = None
            status_code = 500
            try:
                response = await handler(request)
                status_code = response.status_code
                response.headers["Cache-Control"] = "private, no-store"
                return response
            except HTTPException as exc:
                status_code = exc.status_code
                raise
            finally:
                if os.getenv("STRATEGY_STUDIO_OWNER_DEBUG", "1").lower() not in {"0", "false", "off"}:
                    data = {"route": self.path, "method": request.method, "status": status_code,
                            "role": None, "email": None, "actor_id": None, "owner_key": None,
                            "strategy_count": None, "active_strategy_id": None, "live_strategy_id": None}
                    data.update(getattr(request.state, "strategy_owner_debug", {}))
                    if data["owner_key"]:
                        try:
                            data.update(await run_in_threadpool(owner_debug_snapshot, data["owner_key"]))
                        except Exception:
                            data["snapshot_available"] = False
                    logging.getLogger("uvicorn.error").info("STRATEGY_STUDIO_OWNER_DEBUG = %s", json.dumps(data))
        return traced


router = APIRouter(prefix="/strategy-studio", tags=["strategy-studio"], route_class=StrategyOwnerDiagnosticRoute)
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


# Compatibility for simulator routes importing this name.
owner_key = canonical_strategy_owner


def _legacy_actor(request: Request, *, mutation: bool = False):
    del mutation
    try:
        import api
    except Exception:
        return None
    token = _bearer(request.headers)
    session = api.resolve_owner_session(token) if token else None
    if not isinstance(session, dict):
        return None
    if str(session.get("role") or "").lower() != "admin":
        return None
    return session


def _resolve_actor(request: Request, *, mutation: bool = False):
    # A valid legacy-owner Bearer token is explicit and must win over any
    # customer cookie that may also exist in the browser. Otherwise an admin
    # tab can be incorrectly scoped to a customer user and see an empty library.
    legacy = _legacy_actor(request, mutation=mutation)
    if legacy is not None:
        return legacy

    # Explicit owner credentials cannot silently become a different cookie actor.
    authorization = str(request.headers.get("authorization") or "").split(None, 1)
    if authorization and authorization[0].lower() == "bearer":
        raise HTTPException(status_code=401, detail="OWNER_SESSION_EXPIRED")

    resolver = current_user_with_csrf if mutation else current_user
    return resolver(request)


def _actor(request: Request, *, mutation: bool = False):
    actor = _resolve_actor(request, mutation=mutation)
    def field(name):
        value = actor.get(name) if isinstance(actor, dict) else getattr(actor, name, None)
        return str(value)[:320] if value is not None else None
    request.state.strategy_owner_debug = {
        "role": field("role"), "email": field("email"), "actor_id": field("id"),
        "owner_key": canonical_strategy_owner(actor),
    }
    return actor


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
        "readiness_verified": False,
        "readiness_status": "REQUIRES_VERIFICATION",
        # Legacy aliases kept temporarily for older frontends.
        "parity_verified": False,
        "parity_status": "REQUIRES_VERIFICATION",
        "active_strategy_id": None,
        "configured_symbols": [],
        "account_scope": None,
        "unresolved_reconciliation": False,
        "reports": {},
        "reason": reason,
        "validation_kind": "ACTIVE_STRATEGY_EXECUTABILITY",
        "v3b_parity_required": False,
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
    """Verify the active Strategy Studio definition against selected-account data.

    Strategy Studio is an independent executable strategy surface.  V3B parity
    remains available as a diagnostic endpoint, but it must not gate a strategy
    whose rules intentionally differ from V3B.
    """
    active = _active_strategy(owner)
    if not active:
        return {
            **_unavailable_readiness("STRATEGY_STUDIO_ACTIVE_STRATEGY_REQUIRED"),
            "unresolved_reconciliation": has_unresolved_studio_reconciliation(owner),
        }

    try:
        definition = normalize_definition(active.get("definition") or {})
    except Exception as exc:
        result = _unavailable_readiness("STRATEGY_STUDIO_DEFINITION_INVALID")
        result.update({
            "active_strategy_id": active.get("strategy_id"),
            "reports": {"definition": {"executable": False, "error": str(exc)}},
        })
        return result

    errors = validation_errors(definition)
    symbols = [
        str(symbol).upper().replace("/", "")
        for symbol in (definition.get("symbols") or [])
        if str(symbol or "").strip()
    ]
    unresolved = has_unresolved_studio_reconciliation(owner)

    risk_definition = definition.get("risk") or {}
    max_concurrent_positions = int(
        risk_definition.get("max_concurrent_positions") or 1
    )

    if errors:
        return {
            **_unavailable_readiness("STRATEGY_STUDIO_DEFINITION_INVALID"),
            "active_strategy_id": active.get("strategy_id"),
            "configured_symbols": symbols,
            "unresolved_reconciliation": unresolved,
            "reports": {"definition": {"executable": False, "errors": errors}},
        }

    # Multi-position execution is intentionally Simulator-only until LIVE has
    # matching per-symbol admission, combined-risk accounting, and broker
    # lifecycle concurrency. Never let a saved backtest rule silently imply
    # unsupported LIVE behavior.
    if max_concurrent_positions > 1:
        return {
            **_unavailable_readiness("STRATEGY_STUDIO_LIVE_MULTI_POSITION_NOT_SUPPORTED"),
            "active_strategy_id": active.get("strategy_id"),
            "configured_symbols": symbols,
            "unresolved_reconciliation": unresolved,
            "reports": {
                "position_stacking": {
                    "executable": False,
                    "max_concurrent_positions": max_concurrent_positions,
                    "max_combined_open_risk_percent": risk_definition.get(
                        "max_combined_open_risk_percent"
                    ),
                    "live_supported": False,
                }
            },
        }

    identity = selected_identity()
    if identity is None:
        return {
            **_unavailable_readiness("CTRADER_ACCOUNT_NOT_SELECTED"),
            "active_strategy_id": active.get("strategy_id"),
            "configured_symbols": symbols,
            "unresolved_reconciliation": unresolved,
        }

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=PARITY_LOOKBACK_DAYS)
    reports = {}
    readiness_verified = bool(symbols)
    execution_error = None

    for symbol in symbols:
        try:
            bundle = load_market_bundle(
                symbol,
                start,
                end,
                stream_scope=identity.scope,
            )
            frame_5m = bundle.get("5m")
            if frame_5m is None or frame_5m.empty:
                raise ValueError("STRATEGY_STUDIO_HISTORY_UNAVAILABLE")

            result = run_simulation(
                definition,
                bundle,
                symbol,
                10000.0,
                include_replay=False,
                evaluation_start=start,
                evaluation_end=end,
                finalize_open_trade=False,
            )
            diagnostics = result.get("diagnostics") or {}
            candles = int(diagnostics.get("candles_analyzed") or 0)
            executable = candles > 0
            readiness_verified = readiness_verified and executable
            reports[symbol] = {
                "executable": executable,
                "candles_analyzed": candles,
                "evaluations": int(diagnostics.get("evaluations") or 0),
                "signals_emitted": int(diagnostics.get("signals_emitted") or 0),
                "account_scope": identity.scope,
                "trading_timeframe": definition.get("trading_timeframe"),
                "structure_timeframe": definition.get("structure_timeframe"),
            }
        except Exception as exc:
            readiness_verified = False
            execution_error = str(exc)
            reports[symbol] = {
                "executable": False,
                "candles_analyzed": 0,
                "evaluations": 0,
                "signals_emitted": 0,
                "account_scope": identity.scope,
                "error": str(exc),
            }

    reason = None
    if unresolved:
        reason = "STRATEGY_STUDIO_RECONCILIATION_UNRESOLVED"
    elif not readiness_verified:
        reason = (
            "STRATEGY_STUDIO_EXECUTABILITY_UNAVAILABLE"
            if execution_error
            else "STRATEGY_STUDIO_EXECUTABILITY_NOT_VERIFIED"
        )

    status_value = "VERIFIED" if readiness_verified else (
        "ERROR" if execution_error else "REQUIRES_VERIFICATION"
    )
    return {
        "ready": bool(readiness_verified and not unresolved),
        "readiness_verified": bool(readiness_verified),
        "readiness_status": status_value,
        # Legacy aliases kept temporarily for already-deployed clients.
        "parity_verified": bool(readiness_verified),
        "parity_status": status_value,
        "active_strategy_id": active.get("strategy_id"),
        "configured_symbols": symbols,
        "account_scope": identity.scope,
        "unresolved_reconciliation": bool(unresolved),
        "reports": reports,
        "reason": reason,
        "validation_kind": "ACTIVE_STRATEGY_EXECUTABILITY",
        "v3b_parity_required": False,
        "validation_window": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "lookback_days": PARITY_LOOKBACK_DAYS,
        },
    }


@router.get("/strategies")
def strategies_list(request: Request):
    owner = owner_key(_actor(request))
    try:
        return {"ok": True, "owner_id": owner, "strategies": list_strategies(owner)}
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
        "entry_parity_only": False,
        "post_entry_management_compared": False,
        "v3b_parity_required": False,
        "readiness_validation": "ACTIVE_STRATEGY_EXECUTABILITY",
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
    if not readiness.get("readiness_verified", readiness.get("parity_verified")):
        raise HTTPException(
            status_code=409,
            detail=readiness.get("reason") or "STRATEGY_STUDIO_EXECUTABILITY_NOT_VERIFIED",
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
