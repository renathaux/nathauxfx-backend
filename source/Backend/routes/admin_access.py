from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from services.customer_forex_guard import _bearer
from services.email_service import send_account_approved_email
from services.user_auth_service import (
    current_user_with_csrf,
    list_access_requests,
    require_admin,
    review_access_request,
)

router = APIRouter(prefix="/admin/access", tags=["admin-access"])


def _admin(request: Request, *, mutation: bool = False):
    try:
        administrator = current_user_with_csrf(request) if mutation else require_admin(request)
        if not administrator.is_admin:
            raise HTTPException(status_code=403, detail="ADMIN_REQUIRED")
        return administrator
    except HTTPException:
        import api

        token = _bearer(request.headers)
        session = api.SESSIONS.get(token) if token else None
        if not isinstance(session, dict) or str(session.get("role") or "").lower() != "admin":
            raise HTTPException(status_code=403, detail="ADMIN_REQUIRED")
        return session


class AccessDecision(BaseModel):
    decision: str


@router.get("/requests")
def requests(request: Request):
    _admin(request)
    return {"ok": True, "requests": list_access_requests()}


@router.post("/requests/{user_id}")
def decide(user_id: str, payload: AccessDecision, request: Request):
    administrator = _admin(request, mutation=True)
    reviewer = getattr(administrator, "email", None)
    if not reviewer and isinstance(administrator, dict):
        reviewer = administrator.get("email")
    reviewer = reviewer or "administrator"
    try:
        result = review_access_request(user_id, payload.decision, str(reviewer))
    except RuntimeError as exc:
        status = 404 if str(exc) == "ACCESS_REQUEST_NOT_FOUND" else 409
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    email_sent = False
    email_error = None
    if result["approval_status"] == "APPROVED":
        try:
            send_account_approved_email(result["email"], result["full_name"])
            email_sent = True
        except RuntimeError as exc:
            email_error = str(exc)
    return {"ok": True, **result, "email_sent": email_sent, "email_error": email_error}
