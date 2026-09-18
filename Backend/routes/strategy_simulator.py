"""Authenticated, read-only Strategy Studio simulator API.

The simulator consumes saved Strategy Studio definitions, the currently pinned
cTrader account balance, and durable closed candle history.  It never places,
modifies, or closes broker orders and never mutates LIVE Auto or strategy state.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ctrader_account_context import pinned_account
from ctrader_connector import get_ctrader_account_snapshot
from routes.strategy_studio import _actor, _service_http_error, owner_key
from services.strategy_simulator import run_simulation
from services.strategy_simulator_data_source import load_market_bundle
from services.strategy_studio_service import get_strategy


router = APIRouter(prefix="/strategy-simulator", tags=["strategy-simulator"])


class RiskOverride(BaseModel):
    method: Literal["PERCENT_BALANCE", "FIXED_DOLLARS"]
    value: float


class SimulationRequest(BaseModel):
    strategy_id: str
    symbol: str
    start: datetime
    end: datetime
    mode: Literal["FAST", "REPLAY"] = "FAST"
    risk_override: RiskOverride | None = None


class ManualHistoryRequest(BaseModel):
    symbol: str
    timeframe: Literal["5m", "15m", "1h"] = "5m"
    start: datetime
    end: datetime


MAX_MANUAL_REPLAY_DAYS = 31
MAX_MANUAL_REPLAY_CANDLES = 10000


def _snapshot_balance(snapshot) -> float | None:
    candidates = []
    if isinstance(snapshot, dict):
        candidates.extend([
            snapshot.get("balance"),
            (snapshot.get("account") or {}).get("balance") if isinstance(snapshot.get("account"), dict) else None,
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
        raise HTTPException(status_code=400, detail="Risk override value must be positive")
    return {"method": payload.method, "value": float(payload.value)}


@router.post("/run")
def strategy_simulation_run(payload: SimulationRequest, request: Request):
    actor = _actor(request)
    owner = owner_key(actor)
    try:
        strategy = get_strategy(owner, payload.strategy_id)
    except Exception as exc:
        raise _service_http_error(exc) from exc

    definition = strategy["definition"]
    symbol = str(payload.symbol or "").upper().replace("/", "")
    if symbol not in definition.get("symbols", []):
        raise HTTPException(status_code=400, detail="Simulation symbol is not allowed by this saved strategy")
    if payload.end <= payload.start:
        raise HTTPException(status_code=400, detail="Simulation end must be after start")

    override = _risk_override(payload.risk_override)

    try:
        with pinned_account() as identity:
            scope = identity.scope
            snapshot = get_ctrader_account_snapshot()
            balance = _snapshot_balance(snapshot)
            if balance is None:
                raise HTTPException(status_code=409, detail="Selected cTrader account balance is unavailable or nonpositive")
            bundle = load_market_bundle(
                symbol,
                payload.start,
                payload.end,
                stream_scope=scope,
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
        raise HTTPException(status_code=503, detail=f"STRATEGY_SIMULATOR_UNAVAILABLE: {exc}") from exc

    return {
        "ok": True,
        "strategy_id": strategy["strategy_id"],
        "strategy_name": strategy.get("name"),
        "symbol": symbol,
        "mode": payload.mode,
        "account_scope": scope,
        "starting_balance": balance,
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
    """Return account-scoped closed candles for manual replay only.

    This endpoint never loads a saved strategy and never imports broker order,
    LIVE Auto, or position mutation functions.
    """
    _actor(request)
    symbol = str(payload.symbol or "").upper().replace("/", "")
    if symbol not in {"EURUSD", "XAUUSD"}:
        raise HTTPException(status_code=400, detail="Manual replay symbol is unsupported")
    if payload.end <= payload.start:
        raise HTTPException(status_code=400, detail="Manual replay end must be after start")
    if (payload.end - payload.start).total_seconds() > MAX_MANUAL_REPLAY_DAYS * 86400:
        raise HTTPException(
            status_code=400,
            detail=f"Manual replay range is limited to {MAX_MANUAL_REPLAY_DAYS} days",
        )

    try:
        with pinned_account() as identity:
            scope = identity.scope
            bundle = load_market_bundle(
                symbol,
                payload.start,
                payload.end,
                stream_scope=scope,
            )
            frame = bundle.get(payload.timeframe)
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"MANUAL_REPLAY_HISTORY_UNAVAILABLE: {exc}") from exc

    if frame is None or frame.empty:
        raise HTTPException(status_code=409, detail="MANUAL_REPLAY_HISTORY_UNAVAILABLE")
    if len(frame) > MAX_MANUAL_REPLAY_CANDLES:
        raise HTTPException(
            status_code=413,
            detail=f"Manual replay returned more than {MAX_MANUAL_REPLAY_CANDLES} candles; choose a shorter range",
        )

    candles = []
    for timestamp, row in frame.sort_index().iterrows():
        candles.append({
            "timestamp": timestamp.isoformat(),
            "open": float(row["Open"]),
            "high": float(row["High"]),
            "low": float(row["Low"]),
            "close": float(row["Close"]),
            "volume": float(row.get("Volume", 0.0)),
        })

    return {
        "ok": True,
        "mode": "MANUAL_REPLAY",
        "strategy_id": None,
        "strategy_required": False,
        "live_trading_enabled": False,
        "broker_orders_enabled": False,
        "symbol": symbol,
        "timeframe": payload.timeframe,
        "account_scope": scope,
        "start": payload.start.isoformat(),
        "end": payload.end.isoformat(),
        "candles": candles,
    }
