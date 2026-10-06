from fastapi import APIRouter, HTTPException, Query

from fundamentals.insight_service import get_fundamental_insight
from fundamentals.insight_cache import get_or_calculate
from fundamentals.gold_insight_service import get_xauusd_fundamental_insight
from fundamentals.repositories.observations import provider_health


router = APIRouter(prefix="/fundamentals", tags=["fundamentals"])


def _mark_execution_connected(payload):
    result = dict(payload or {})
    guidance = dict(result.get("trading_guidance") or {})
    preference = str(guidance.get("preference") or "").upper()
    guidance["execution_connected"] = True

    if preference == "PREFER_BUY":
        guidance["message"] = (
            "Fundamental BUY bias is an active entry filter. Opposing SELL "
            "strategy entries are blocked while this directional bias is active."
        )
    elif preference == "PREFER_SELL":
        guidance["message"] = (
            "Fundamental SELL bias is an active entry filter. Opposing BUY "
            "strategy entries are blocked while this directional bias is active."
        )
    elif preference == "NEUTRAL":
        guidance["message"] = (
            "Fundamentals are neutral, so they do not block otherwise valid "
            "technical strategy entries."
        )
    else:
        guidance["message"] = (
            "Fundamental coverage is insufficient, so the macro filter does not "
            "block otherwise valid technical strategy entries."
        )

    result["trading_guidance"] = guidance
    return result


@router.get("/insight")
def fundamental_insight(
    symbol: str = Query(default="EURUSD"),
    refresh: bool = Query(default=False),
):
    normalized = str(symbol or "").upper().replace("/", "")
    if normalized not in {"EURUSD", "XAUUSD"}:
        raise HTTPException(
            status_code=422,
            detail="Fundamental Insight currently supports EURUSD and XAUUSD only.",
        )

    def calculate():
        if normalized == "XAUUSD":
            return get_xauusd_fundamental_insight()
        # GET remains genuinely read-only. Snapshot creation belongs to an
        # independent calculation/ingestion path, not the user-facing request.
        return get_fundamental_insight(normalized, persist=False)

    insight = get_or_calculate(normalized, calculate, bypass=refresh is True)
    return _mark_execution_connected(insight)


@router.get("/health")
def fundamental_health():
    try:
        return provider_health()
    except Exception as exc:
        return {
            "engine_readiness": "NOT_READY",
            "status": "UNAVAILABLE",
            "error": str(exc),
        }
