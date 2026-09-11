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
        "strategies": [
            {
                "id": "baseline_v1",
                "name": "Baseline v1",
                "symbols": ["EURUSD"],
                "default_start": "2026-08-22T00:00:00Z",
                "experimental": False,
            },
            {
                "id": "v2_m5_quality",
                "name": "V2 — M5 quality 55/25",
                "symbols": ["EURUSD"],
                "default_start": "2026-08-22T00:00:00Z",
                "experimental": True,
                "description": "Baseline plus M5 body >= 55% and close-side wick <= 25%.",
            },
            {
                "id": "v2a_m5_quality_50_30",
                "name": "V2A — M5 quality 50/30",
                "symbols": ["EURUSD"],
                "default_start": "2026-08-22T00:00:00Z",
                "experimental": True,
                "description": "Baseline plus M5 body >= 50% and close-side wick <= 30%.",
            },
            {
                "id": "v2b_m5_quality_45_35",
                "name": "V2B — M5 quality 45/35",
                "symbols": ["EURUSD"],
                "default_start": "2026-08-22T00:00:00Z",
                "experimental": True,
                "description": "Baseline plus M5 body >= 45% and close-side wick <= 35%.",
            },
            {
                "id": "v2c_m15_quality_60_30",
                "name": "V2C — M15 quality 60/30",
                "symbols": ["EURUSD"],
                "default_start": "2026-08-22T00:00:00Z",
                "experimental": True,
                "description": "Baseline confirmation plus M15 break body >= 60% and close-side wick <= 30%.",
            },
            {
                "id": "v3_m5_two_close",
                "name": "V3 — Pure M5 BOS + next close",
                "symbols": ["EURUSD"],
                "default_start": "2026-08-22T00:00:00Z",
                "experimental": True,
                "description": (
                    "5m only: a closed 5m BOS, then the immediate next 5m candle must "
                    "close in the same direction and remain beyond the BOS level; entry "
                    "is at that second close. No 15m logic."
                ),
            },
            {
                "id": "v3a_m5_bos_body_50",
                "name": "V3A — Pure M5 + BOS body >= 50%",
                "symbols": ["EURUSD"],
                "default_start": "2026-08-22T00:00:00Z",
                "experimental": True,
                "description": (
                    "V3 unchanged except the 5m BOS candle body must cover at least "
                    "50% of its full range. No second-candle quality or 15m filter."
                ),
            },
            {
                "id": "v3b_m5_frozen_candidate",
                "name": "V3B — Frozen M5 candidate",
                "symbols": ["EURUSD"],
                "default_start": "2026-08-17T03:20:00Z",
                "experimental": True,
                "frozen_research_candidate": True,
                "description": (
                    "Frozen EURUSD research candidate: pure 5m BOS, BOS body >=50%, "
                    "immediate next-candle confirmation, fixed 1.90R target, protection "
                    "armed at 70% of target path and stop locked at 60% of target path. "
                    "No 15m logic and no partial close."
                ),
            },
        ],
    }


@router.post("/replay")
def replay(payload: ReplayRequest, request: Request):
    _require_strategy_lab_admin(request)
    try:
        return run_replay(payload.symbol, payload.strategy, payload.start, payload.end)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
