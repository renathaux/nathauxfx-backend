from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from services.user_auth_service import (
    authenticate,
    change_password,
    clear_session_cookie,
    create_session,
    current_user,
    current_user_with_csrf,
    issue_email_verification,
    mask_email,
    public_user,
    request_session_token,
    revoke_session,
    session_snapshot,
    set_session_cookie,
    signup,
    verify_email_code,
)

router = APIRouter(prefix="/auth", tags=["auth"])


class SignupRequest(BaseModel):
    email: str
    password: str


class LoginRequest(BaseModel):
    email: str
    password: str


class VerifyEmailRequest(BaseModel):
    email: str
    code: str


class ResendVerificationRequest(BaseModel):
    email: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


def _delivery_error(exc: RuntimeError) -> HTTPException:
    code = str(exc)
    if code == "VERIFICATION_CODE_COOLDOWN":
        return HTTPException(status_code=429, detail=code)
    if code == "VERIFICATION_RATE_LIMITED":
        return HTTPException(status_code=429, detail=code)
    if code in {"EMAIL_PROVIDER_NOT_CONFIGURED", "EMAIL_FROM_NOT_CONFIGURED", "EMAIL_DELIVERY_FAILED"}:
        return HTTPException(status_code=503, detail=code)
    return HTTPException(status_code=400, detail=code)


def _verification_response(email: str, response: Response):
    try:
        verification = issue_email_verification(email)
    except RuntimeError as exc:
        if str(exc) == "VERIFICATION_CODE_COOLDOWN":
            clear_session_cookie(response)
            return {
                "ok": True,
                "verification_required": True,
                "email": mask_email(email),
                "expires_in": None,
                "resend_after": 60,
                "delivery": "already_sent",
            }
        raise
    clear_session_cookie(response)
    return {
        "ok": True,
        "verification_required": True,
        "email": verification["email"],
        "expires_in": verification["expires_in"],
        "resend_after": verification["resend_after"],
        "delivery": "sent",
    }


@router.post("/signup")
def create_account(payload: SignupRequest, response: Response):
    try:
        user = signup(payload.email, payload.password)
        return _verification_response(user["email"], response)
    except RuntimeError as exc:
        code = str(exc)
        if code == "EMAIL_ALREADY_REGISTERED":
            try:
                existing = authenticate(payload.email, payload.password)
            except RuntimeError as auth_exc:
                raise HTTPException(status_code=409, detail=code) from auth_exc
            if str(existing.get("role", "user")) != "user" or bool(existing.get("email_verified")):
                raise HTTPException(status_code=409, detail=code) from exc
            try:
                return _verification_response(str(existing["email"]), response)
            except RuntimeError as send_exc:
                raise _delivery_error(send_exc) from send_exc
        raise _delivery_error(exc) from exc


@router.post("/login")
def login(payload: LoginRequest, response: Response):
    try:
        row = authenticate(payload.email, payload.password)
        if str(row.get("role", "user")) == "user" and not bool(row.get("email_verified")):
            try:
                verification = issue_email_verification(str(row["email"]))
                delivery = "sent"
                masked = verification.get("email") or mask_email(str(row["email"]))
            except RuntimeError as send_exc:
                if str(send_exc) == "VERIFICATION_CODE_COOLDOWN":
                    delivery = "already_sent"
                    masked = mask_email(str(row["email"]))
                else:
                    raise _delivery_error(send_exc) from send_exc
            clear_session_cookie(response)
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "EMAIL_VERIFICATION_REQUIRED",
                    "email": masked,
                    "delivery": delivery,
                },
            )
        token, csrf, expires = create_session(str(row["id"]))
        set_session_cookie(response, token)
        return {
            "ok": True,
            "user": public_user(row),
            "session_token": token,
            "csrf_token": csrf,
            "expires_at": expires,
        }
    except HTTPException:
        raise
    except RuntimeError as exc:
        raise HTTPException(status_code=401, detail="INVALID_EMAIL_OR_PASSWORD") from exc


@router.post("/verify-email")
def verify_email(payload: VerifyEmailRequest, response: Response):
    try:
        user = verify_email_code(payload.email, payload.code)
        token, csrf, expires = create_session(str(user["id"]))
        set_session_cookie(response, token)
        return {
            "ok": True,
            "verified": True,
            "user": user,
            "session_token": token,
            "csrf_token": csrf,
            "expires_at": expires,
        }
    except RuntimeError as exc:
        code = str(exc)
        status = 429 if code == "VERIFICATION_ATTEMPTS_EXCEEDED" else 400
        raise HTTPException(status_code=status, detail=code) from exc


@router.post("/resend-verification")
def resend_verification(payload: ResendVerificationRequest):
    try:
        result = issue_email_verification(payload.email)
        return {
            "ok": True,
            "verification_required": not bool(result.get("verified")),
            "email": result.get("email") or mask_email(payload.email),
            "expires_in": result.get("expires_in"),
            "resend_after": result.get("resend_after"),
        }
    except RuntimeError as exc:
        if str(exc) == "EMAIL_NOT_REGISTERED":
            return {"ok": True, "verification_required": True}
        raise _delivery_error(exc) from exc


@router.get("/session")
def session(request: Request):
    token, _source = request_session_token(request)
    snapshot = session_snapshot(token)
    if not snapshot:
        return {"ok": True, "authenticated": False}
    user, csrf = snapshot
    return {
        "ok": True,
        "authenticated": True,
        "user": {"id": user.id, "email": user.email, "role": user.role, "email_verified": user.email_verified},
        "csrf_token": csrf,
    }


@router.post("/logout")
def logout(request: Request, response: Response):
    current_user_with_csrf(request)
    token, _source = request_session_token(request)
    revoke_session(token)
    clear_session_cookie(response)
    return {"ok": True, "authenticated": False}


@router.post("/change-password")
def update_password(payload: ChangePasswordRequest, request: Request):
    user = current_user_with_csrf(request)
    token, _source = request_session_token(request)
    try:
        return change_password(user.id, payload.current_password, payload.new_password, token)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/me")
def me(request: Request):
    user = current_user(request)
    return {"ok": True, "user": {"id": user.id, "email": user.email, "role": user.role, "email_verified": user.email_verified}}
