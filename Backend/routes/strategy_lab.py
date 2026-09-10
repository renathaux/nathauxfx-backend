from fastapi import APIRouter, HTTPException, Request

from services.customer_forex_guard import _bearer
from services.strategy_lab import run_replay
from services.strategy_lab.models import ReplayRequest
from services.user_auth_service import require_admin

router = APIRouter(prefix="/strategy-lab", tags=["strategy-lab"])


def _require_strategy_lab_admin(request: Request):
    """Reuse database admin auth with the established legacy owner fallback."""
    try:
        return require_admin(request)
    except HTTPException:
        import api

        token = _bearer(request.headers)
        session = api.SESSIONS.get(token) if token else None
        if not isinstance(session, dict) or str(session.get("role") or "").lower() != "admin":
            raise HTTPException(status_code=403, detail="ADMIN_STRATEGY_LAB_REQUIRED")
        return session


@router.get("/strategies")
def strategies(request: Request):
    _require_strategy_lab_admin(request)
    return {
        "analysis_only": True,
        "strategies": [{
            "id": "baseline_v1", "name": "Baseline v1",
            "symbols": ["EURUSD"], "default_start": "2026-08-22T00:00:00Z",
        }],
    }


@router.post("/replay")
def replay(payload: ReplayRequest, request: Request):
    _require_strategy_lab_admin(request)
    try:
        return run_replay(payload.symbol, payload.strategy, payload.start, payload.end)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
