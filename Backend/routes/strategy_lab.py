from fastapi import APIRouter, HTTPException

from services.strategy_lab import run_replay
from services.strategy_lab.models import ReplayRequest

router = APIRouter(prefix="/strategy-lab", tags=["strategy-lab"])


@router.get("/strategies")
def strategies():
    return {
        "analysis_only": True,
        "strategies": [{
            "id": "baseline_v1", "name": "Baseline v1",
            "symbols": ["EURUSD"], "default_start": "2026-08-22T00:00:00Z",
        }],
    }


@router.post("/replay")
def replay(request: ReplayRequest):
    try:
        return run_replay(request.symbol, request.strategy, request.start, request.end)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
