"""Authenticated, read-only Strategy Studio simulator API.

Simulation candles are supplied by the frontend static replay-data library.
This route does not read historical candles from Neon and never places,
modifies, or closes broker orders.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ctrader_account_context import pinned_account
from ctrader_connector import get_ctrader_account_snapshot
from routes.strategy_studio import _actor
from services.strategy_simulator import run_simulation
from services.strategy_simulator_static_data import build_static_market_bundle


router = APIRouter(prefix="/strategy-simulator", tags=["strategy-simulator"])


class RiskOverride(BaseModel):
    method: Literal["PERCENT_BALANCE", "FIXED_DOLLARS"]
    value: float


class SimulationCandle(BaseModel):
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


class SimulationRequest(BaseModel):
    strategy_id: str
    strategy_name: str | None = None
    strategy_definition: dict
    symbol: str
    start: datetime
    end: datetime
    mode: Literal["FAST", "REPLAY"] = "FAST"
    risk_override: RiskOverride | None = None
    candles_5m: list[SimulationCandle]
    continuation: dict | None = None
    finalize: bool = True


class ManualHistoryRequest(BaseModel):
    symbol: str
    timeframe: Literal["5m", "15m", "1h"] = "5m"
    start: datetime
    end: datetime


MAX_STATIC_SIMULATION_CANDLES = 25000


def _snapshot_balance(snapshot) -> float | None:
    candidates = []
    if isinstance(snapshot, dict):
        candidates.extend([
            snapshot.get("balance"),
            (snapshot.get("account") or {}).get("balance")
            if isinstance(snapshot.get("account"), dict) else None,
        ])
    else:
        candidates.extend([
            getattr(snapshot, "balance", None),
            getattr(getattr(snapshot, "account", None), "balance", None),
        ])
    for raw in candidates:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def _continuation_balance(payload: dict | None) -> float | None:
    if not isinstance(payload, dict):
        return None
    try:
        value = float(payload.get("balance"))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _risk_override(payload: RiskOverride | None) -> dict | None:
    if payload is None:
        return None
    if float(payload.value) <= 0:
        raise HTTPException(
            status_code=400,
            detail="Risk override value must be positive",
        )
    return {"method": payload.method, "value": float(payload.value)}


@router.post("/run")
def strategy_simulation_run(payload: SimulationRequest, request: Request):
    """Run a virtual simulation from client-supplied static replay candles."""
    _actor(request)

    definition = payload.strategy_definition
    if not isinstance(definition, dict):
        raise HTTPException(
            status_code=400,
            detail="Static simulator strategy definition is required",
        )

    symbol = str(payload.symbol or "").upper().replace("/", "")
    if symbol not in definition.get("symbols", []):
        raise HTTPException(
            status_code=400,
            detail="Simulation symbol is not allowed by this saved strategy",
        )
    if payload.end <= payload.start:
        raise HTTPException(
            status_code=400,
            detail="Simulation end must be after start",
        )
    if not payload.candles_5m:
        raise HTTPException(
            status_code=409,
            detail="STATIC_SIMULATION_HISTORY_REQUIRED",
        )
    if len(payload.candles_5m) > MAX_STATIC_SIMULATION_CANDLES:
        raise HTTPException(
            status_code=413,
            detail=(
                "Static simulator history exceeds "
                f"{MAX_STATIC_SIMULATION_CANDLES} M5 candles"
            ),
        )

    override = _risk_override(payload.risk_override)

    try:
        with pinned_account() as identity:
            scope = identity.scope
            balance = _continuation_balance(payload.continuation)
            if balance is None:
                snapshot = get_ctrader_account_snapshot()
                balance = _snapshot_balance(snapshot)
                if balance is None:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "Selected cTrader account balance is unavailable "
                            "or nonpositive"
                        ),
                    )

            bundle = build_static_market_bundle(
                payload.candles_5m,
                payload.start,
                payload.end,
            )
            result = run_simulation(
                definition,
                bundle,
                symbol,
                balance,
                risk_override=override,
                include_replay=payload.mode == "REPLAY",
                evaluation_start=payload.start,
                evaluation_end=payload.end,
                continuation=payload.continuation,
                finalize_open_trade=payload.finalize,
            )
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"STRATEGY_SIMULATOR_UNAVAILABLE: {exc}",
        ) from exc

    return {
        "ok": True,
        "strategy_id": payload.strategy_id,
        "strategy_name": payload.strategy_name,
        "symbol": symbol,
        "mode": payload.mode,
        "account_scope": scope,
        "starting_balance": balance,
        "history_source": "STATIC_REPLAY_JSON",
        "neon_candle_reads": False,
        "assumptions": {
            "closed_candles_only": True,
            "spread": False,
            "commission": False,
            "slippage": False,
            "ambiguous_intrabar_excluded": True,
            "live_trading_enabled": False,
        },
        **result,
    }


@router.post("/manual-history")
def manual_replay_history(payload: ManualHistoryRequest, request: Request):
    """Retired database history endpoint.

    Manual Replay now loads /replay-data static JSON directly in the browser.
    Keeping a non-reading tombstone prevents stale clients from reintroducing
    historical Neon traffic.
    """
    _actor(request)
    raise HTTPException(
        status_code=410,
        detail="MANUAL_REPLAY_HISTORY_MOVED_TO_STATIC_JSON",
    )


class FastJobRequest(BaseModel):
    strategy_id: str
    strategy_name: str | None = None
    strategy_definition: dict
    symbol: Literal['EURUSD', 'XAUUSD']
    start: datetime
    end: datetime
    risk_override: RiskOverride | None = None


def _fast_jobs():
    import os
    from services.strategy_fast_jobs import manager
    if os.environ.get('SIMULATOR_FAST_JOBS_ENABLED') != '1':
        raise HTTPException(status_code=503, detail='Fast jobs are not enabled on this server yet.')
    return manager()


@router.post('/fast-jobs')
def create_fast_job(payload: FastJobRequest, request: Request):
    from routes.strategy_studio import owner_key
    from services.strategy_studio_schema import normalize_definition
    from services.strategy_fast_jobs import JobBusy
    actor=_actor(request, mutation=True)
    try:
        definition=normalize_definition(payload.strategy_definition)
        if payload.symbol not in definition['symbols']:raise ValueError('Simulation symbol is not allowed by this strategy.')
        if payload.start.tzinfo is None or payload.end.tzinfo is None:raise ValueError('Backtest dates must include a timezone.')
        days=(payload.end-payload.start).total_seconds()/86400
        if not 0 < days <= 5*366:raise ValueError('Fast Backtest requires a valid range of at most five years.')
        override=_risk_override(payload.risk_override)
    except ValueError as exc:raise HTTPException(status_code=400,detail=str(exc)) from exc
    jobs=_fast_jobs()
    with pinned_account() as identity:
        balance=_snapshot_balance(get_ctrader_account_snapshot())
        if balance is None:raise HTTPException(status_code=409,detail='Selected cTrader account balance is unavailable or nonpositive')
        job_payload=dict(strategy_id=payload.strategy_id,strategy_name=payload.strategy_name,strategy_definition=definition,symbol=payload.symbol,start=payload.start.isoformat(),end=payload.end.isoformat(),risk_override=override,starting_balance=balance,account_scope=identity.scope)
    try:return jobs.create(owner_key(actor),job_payload)
    except JobBusy as exc:raise HTTPException(status_code=429,detail=str(exc)) from exc


@router.get('/fast-jobs/{job_id}')
def get_fast_job(job_id: str, request: Request):
    from routes.strategy_studio import owner_key
    from services.strategy_fast_jobs import JobNotFound
    actor=_actor(request)
    try:return _fast_jobs().response(owner_key(actor),job_id)
    except JobNotFound:raise HTTPException(status_code=404,detail='Backtest job not found')


@router.post('/fast-jobs/{job_id}/cancel')
def cancel_fast_job(job_id: str, request: Request):
    from routes.strategy_studio import owner_key
    from services.strategy_fast_jobs import JobNotFound
    actor=_actor(request, mutation=True)
    try:return _fast_jobs().cancel(owner_key(actor),job_id)
    except JobNotFound:raise HTTPException(status_code=404,detail='Backtest job not found')
