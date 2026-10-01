"""Temporary, opt-in admin diagnostic. No legacy authentication fallback."""
import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from services.ctrader_symbol_metadata import collect_symbol_market_hours
from services.user_auth_service import require_admin

router = APIRouter()
_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


@router.get("/admin/ctrader/symbol-market-hours", include_in_schema=False)
def symbol_market_hours(request: Request):
    try:
        require_admin(request)
    except HTTPException as exc:
        status = exc.status_code if exc.status_code in {401, 403} else 503
        return JSONResponse({"detail": "ACCESS_DENIED"}, status_code=status, headers=_NO_STORE)
    except Exception:
        return JSONResponse({"detail": "SYMBOL_METADATA_UNAVAILABLE"}, status_code=503, headers=_NO_STORE)
    if os.getenv("CTRADER_SYMBOL_METADATA_DIAGNOSTIC_ENABLED") != "1":
        return JSONResponse({"detail": "NOT_FOUND"}, status_code=404, headers=_NO_STORE)
    try:
        return JSONResponse(collect_symbol_market_hours(), headers=_NO_STORE)
    except Exception:
        # Never return/log exception text, upstream payloads or stack locals.
        return JSONResponse({"detail": "SYMBOL_METADATA_UNAVAILABLE"}, status_code=503, headers=_NO_STORE)
