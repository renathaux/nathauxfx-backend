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


class ManualHistoryRequest(BaseModel):
    symbol: str
    timeframe: Literal["5m", "15m", "1h"] = "5m"
    start: datetime
    end: datetime


MAX_STATIC_SIMULATION_CANDLES = 10000


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
