from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from services.password_reset_service import request_password_reset, reset_password

router = APIRouter(prefix="/auth", tags=["auth"])


class ForgotPasswordRequest(BaseModel):
    email: str


class ResetPasswordRequest(BaseModel):
    email: str
    code: str
    new_password: str


def _reset_error(exc: RuntimeError) -> HTTPException:
    code = str(exc)
    if code in {"RESET_CODE_COOLDOWN", "RESET_RATE_LIMITED"}:
        return HTTPException(status_code=429, detail=code)
    if code in {"EMAIL_PROVIDER_NOT_CONFIGURED", "EMAIL_FROM_NOT_CONFIGURED", "EMAIL_DELIVERY_FAILED"}:
        return HTTPException(status_code=503, detail=code)
    if code == "PASSWORD_LENGTH_INVALID":
        return HTTPException(status_code=400, detail=code)
    if code in {"INVALID_RESET_CODE", "RESET_CODE_EXPIRED", "RESET_ATTEMPTS_EXCEEDED"}:
        status = 429 if code == "RESET_ATTEMPTS_EXCEEDED" else 400
        return HTTPException(status_code=status, detail=code)
    return HTTPException(status_code=400, detail=code)


@router.post("/forgot-password")
def forgot_password(payload: ForgotPasswordRequest):
    try:
        # Unknown emails deliberately receive the same successful response shape.
        return request_password_reset(payload.email)
    except RuntimeError as exc:
        raise _reset_error(exc) from exc


@router.post("/reset-password")
def complete_password_reset(payload: ResetPasswordRequest):
    try:
        return reset_password(payload.email, payload.code, payload.new_password)
    except RuntimeError as exc:
        raise _reset_error(exc) from exc
