from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import HTTPException, Request, Response
from sqlalchemy import Boolean, Column, Float, Integer, MetaData, String, Table, Text, func, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from db import engine as default_engine
from services.email_service import send_verification_email

SESSION_COOKIE = "flowsignal_session"
USER_AUTH_SCHEME = "FlowSignalUser"
SESSION_SECONDS = int(os.getenv("FLOWSIGNAL_SESSION_SECONDS", str(60 * 60 * 24 * 7)))
PBKDF2_ITERATIONS = max(600_000, int(os.getenv("FLOWSIGNAL_PBKDF2_ITERATIONS", "600000")))
OTP_PBKDF2_ITERATIONS = max(120_000, int(os.getenv("FLOWSIGNAL_OTP_PBKDF2_ITERATIONS", "120000")))
OTP_TTL_SECONDS = max(60, int(os.getenv("FLOWSIGNAL_OTP_TTL_SECONDS", "600")))
OTP_RESEND_SECONDS = max(15, int(os.getenv("FLOWSIGNAL_OTP_RESEND_SECONDS", "60")))
OTP_MAX_ATTEMPTS = max(3, int(os.getenv("FLOWSIGNAL_OTP_MAX_ATTEMPTS", "5")))
OTP_MAX_SENDS_PER_HOUR = max(3, int(os.getenv("FLOWSIGNAL_OTP_MAX_SENDS_PER_HOUR", "5")))
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
OTP_RE = re.compile(r"^\d{6}$")

metadata = MetaData()
users = Table(
    "flowsignal_users", metadata,
    Column("id", String(36), primary_key=True),
    Column("email", String(320), nullable=False, unique=True, index=True),
    Column("full_name", String(160), nullable=False),
    Column("password_hash", Text, nullable=False),
    Column("role", String(16), nullable=False, default="user"),
    Column("is_active", Boolean, nullable=False, default=True),
    Column("email_verified", Boolean, nullable=False, default=False),
    Column("approval_status", String(24), nullable=False, default="PENDING_EMAIL"),
    Column("reviewed_at", Float),
    Column("reviewed_by", String(320)),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    Column("last_login_at", Float),
)
sessions = Table(
    "flowsignal_sessions", metadata,
    Column("token_hash", String(64), primary_key=True),
    Column("user_id", String(36), nullable=False, index=True),
    Column("csrf_token", String(64), nullable=False),
    Column("created_at", Float, nullable=False),
    Column("expires_at", Float, nullable=False, index=True),
    Column("last_seen_at", Float, nullable=False),
    Column("revoked_at", Float),
)
email_verification_codes = Table(
    "flowsignal_email_verification_codes", metadata,
    Column("id", String(36), primary_key=True),
    Column("user_id", String(36), nullable=False, index=True),
    Column("code_hash", Text, nullable=False),
    Column("created_at", Float, nullable=False),
    Column("expires_at", Float, nullable=False, index=True),
    Column("consumed_at", Float),
    Column("attempt_count", Integer, nullable=False, default=0),
)


@dataclass(frozen=True)
class CurrentUser:
    id: str
    email: str
    role: str
    email_verified: bool
    full_name: str = ""

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def _engine(engine: Engine | None = None) -> Engine:
    chosen = engine or default_engine
    metadata.create_all(chosen)
    return chosen


def normalize_email(value: str) -> str:
    email = str(value or "").strip().lower()
    if not EMAIL_RE.fullmatch(email):
        raise RuntimeError("INVALID_EMAIL")
    return email


def mask_email(value: str) -> str:
    email = normalize_email(value)
    local, domain = email.split("@", 1)
    visible = local[:2] if len(local) > 2 else local[:1]
    return f"{visible}{'*' * max(2, len(local) - len(visible))}@{domain}"


def hash_password(password: str) -> str:
    password = str(password or "")
    if len(password) < 10 or len(password) > 1024:
        raise RuntimeError("PASSWORD_LENGTH_INVALID")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, rounds, salt_hex, digest_hex = str(encoded).split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        iterations = int(rounds)
        if iterations < 100_000:
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
        actual = hashlib.pbkdf2_hmac("sha256", str(password).encode("utf-8"), salt, iterations)
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def _hash_otp(code: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", code.encode("ascii"), salt, OTP_PBKDF2_ITERATIONS)
    return f"otp_pbkdf2_sha256${OTP_PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def _verify_otp(code: str, encoded: str) -> bool:
    try:
        scheme, rounds, salt_hex, digest_hex = str(encoded).split("$", 3)
        if scheme != "otp_pbkdf2_sha256":
            return False
        iterations = int(rounds)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
        actual = hashlib.pbkdf2_hmac("sha256", code.encode("ascii"), salt, iterations)
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def public_user(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "email": row["email"],
        "full_name": row.get("full_name") or row["email"],
        "role": row["role"],
        "email_verified": bool(row["email_verified"]),
        "approval_status": str(row.get("approval_status") or "APPROVED"),
    }


def signup(email: str, password: str, full_name: str = "", *, engine: Engine | None = None) -> dict[str, Any]:
    chosen = _engine(engine)
    normalized = normalize_email(email)
    name = " ".join(str(full_name or "").strip().split()) or normalized.split("@", 1)[0]
    if len(name) < 2 or len(name) > 160:
        raise RuntimeError("FULL_NAME_INVALID")
    now = time.time()
    row = {
        "id": str(uuid.uuid4()),
        "email": normalized,
        "full_name": name,
        "password_hash": hash_password(password),
        "role": "user",
        "is_active": True,
        "email_verified": False,
        "approval_status": "PENDING_EMAIL",
        "reviewed_at": None,
        "reviewed_by": None,
        "created_at": now,
        "updated_at": now,
    }
    try:
        with chosen.begin() as connection:
            connection.execute(users.insert().values(**row))
    except IntegrityError as exc:
        raise RuntimeError("EMAIL_ALREADY_REGISTERED") from exc
    return public_user(row)


def authenticate(email: str, password: str, *, engine: Engine | None = None) -> dict[str, Any]:
    chosen = _engine(engine)
    normalized = normalize_email(email)
    with chosen.begin() as connection:
        row = connection.execute(select(users).where(users.c.email == normalized)).mappings().first()
        if not row or not row["is_active"] or not verify_password(password, row["password_hash"]):
            raise RuntimeError("INVALID_EMAIL_OR_PASSWORD")
        connection.execute(update(users).where(users.c.id == row["id"]).values(last_login_at=time.time(), updated_at=time.time()))
    return dict(row)


def list_access_requests(*, engine: Engine | None = None) -> list[dict[str, Any]]:
    chosen = _engine(engine)
    with chosen.begin() as connection:
        rows = connection.execute(
            select(users).where(users.c.role == "user").order_by(users.c.created_at.desc())
        ).mappings().all()
    return [{
        "id": str(row["id"]), "full_name": str(row["full_name"]),
        "email": str(row["email"]), "email_verified": bool(row["email_verified"]),
        "approval_status": str(row["approval_status"]),
        "created_at": float(row["created_at"]), "reviewed_at": row["reviewed_at"],
        "reviewed_by": row["reviewed_by"],
    } for row in rows]


def review_access_request(user_id: str, decision: str, reviewed_by: str, *, engine: Engine | None = None) -> dict[str, Any]:
    chosen = _engine(engine)
    normalized = str(decision or "").strip().upper()
    if normalized not in {"APPROVED", "DENIED"}:
        raise RuntimeError("INVALID_APPROVAL_DECISION")
    now = time.time()
    with chosen.begin() as connection:
        row = connection.execute(select(users).where(users.c.id == str(user_id))).mappings().first()
        if not row or str(row["role"]) != "user":
            raise RuntimeError("ACCESS_REQUEST_NOT_FOUND")
        if normalized == "APPROVED" and not bool(row["email_verified"]):
            raise RuntimeError("EMAIL_VERIFICATION_REQUIRED")
        connection.execute(update(users).where(users.c.id == str(user_id)).values(
            approval_status=normalized, is_active=normalized == "APPROVED",
            reviewed_at=now, reviewed_by=str(reviewed_by), updated_at=now,
        ))
        connection.execute(update(sessions).where(
            sessions.c.user_id == str(user_id), sessions.c.revoked_at.is_(None),
        ).values(revoked_at=now))
    return {"id": str(user_id), "full_name": str(row["full_name"]),
            "email": str(row["email"]), "approval_status": normalized}


def issue_email_verification(
    email: str,
    *,
    engine: Engine | None = None,
    sender: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    chosen = _engine(engine)
    normalized = normalize_email(email)
    now = time.time()
    send = sender or send_verification_email

    with chosen.begin() as connection:
        user = connection.execute(select(users).where(users.c.email == normalized)).mappings().first()
        if not user or not user["is_active"]:
            raise RuntimeError("EMAIL_NOT_REGISTERED")
        if bool(user["email_verified"]):
            return {"verified": True, "email": mask_email(normalized)}

        latest = connection.execute(
            select(email_verification_codes)
            .where(email_verification_codes.c.user_id == user["id"])
            .order_by(email_verification_codes.c.created_at.desc())
            .limit(1)
        ).mappings().first()
        if latest and float(latest["created_at"]) > now - OTP_RESEND_SECONDS:
            raise RuntimeError("VERIFICATION_CODE_COOLDOWN")

        sends_last_hour = connection.execute(
            select(func.count())
            .select_from(email_verification_codes)
            .where(
                email_verification_codes.c.user_id == user["id"],
                email_verification_codes.c.created_at >= now - 3600,
            )
        ).scalar_one()
        if int(sends_last_hour or 0) >= OTP_MAX_SENDS_PER_HOUR:
            raise RuntimeError("VERIFICATION_RATE_LIMITED")

        connection.execute(
            update(email_verification_codes)
            .where(
                email_verification_codes.c.user_id == user["id"],
                email_verification_codes.c.consumed_at.is_(None),
            )
            .values(consumed_at=now)
        )
        code = f"{secrets.randbelow(1_000_000):06d}"
        code_id = str(uuid.uuid4())
        connection.execute(email_verification_codes.insert().values(
            id=code_id,
            user_id=user["id"],
            code_hash=_hash_otp(code),
            created_at=now,
            expires_at=now + OTP_TTL_SECONDS,
            consumed_at=None,
            attempt_count=0,
        ))

    try:
        send(normalized, code)
    except Exception as exc:
        with chosen.begin() as connection:
            connection.execute(
                update(email_verification_codes)
                .where(email_verification_codes.c.id == code_id)
                .values(consumed_at=time.time())
            )
        if isinstance(exc, RuntimeError):
            raise
        raise RuntimeError("EMAIL_DELIVERY_FAILED") from exc

    return {
        "verified": False,
        "verification_required": True,
        "email": mask_email(normalized),
        "expires_in": OTP_TTL_SECONDS,
        "resend_after": OTP_RESEND_SECONDS,
    }


def verify_email_code(email: str, code: str, *, engine: Engine | None = None) -> dict[str, Any]:
    chosen = _engine(engine)
    normalized = normalize_email(email)
    supplied = str(code or "").strip()
    if not OTP_RE.fullmatch(supplied):
        raise RuntimeError("INVALID_VERIFICATION_CODE")
    now = time.time()

    with chosen.begin() as connection:
        user = connection.execute(select(users).where(users.c.email == normalized)).mappings().first()
        if not user or not user["is_active"]:
            raise RuntimeError("INVALID_VERIFICATION_CODE")
        if bool(user["email_verified"]):
            return public_user(dict(user))

        record = connection.execute(
            select(email_verification_codes)
            .where(
                email_verification_codes.c.user_id == user["id"],
                email_verification_codes.c.consumed_at.is_(None),
            )
            .order_by(email_verification_codes.c.created_at.desc())
            .limit(1)
        ).mappings().first()
        if not record or float(record["expires_at"]) <= now:
            if record:
                connection.execute(
                    update(email_verification_codes)
                    .where(email_verification_codes.c.id == record["id"])
                    .values(consumed_at=now)
                )
            raise RuntimeError("VERIFICATION_CODE_EXPIRED")

        attempts = int(record["attempt_count"] or 0)
        if attempts >= OTP_MAX_ATTEMPTS:
            connection.execute(
                update(email_verification_codes)
                .where(email_verification_codes.c.id == record["id"])
                .values(consumed_at=now)
            )
            raise RuntimeError("VERIFICATION_ATTEMPTS_EXCEEDED")

        if not _verify_otp(supplied, str(record["code_hash"])):
            attempts += 1
            values: dict[str, Any] = {"attempt_count": attempts}
            if attempts >= OTP_MAX_ATTEMPTS:
                values["consumed_at"] = now
            connection.execute(
                update(email_verification_codes)
                .where(email_verification_codes.c.id == record["id"])
                .values(**values)
            )
            raise RuntimeError(
                "VERIFICATION_ATTEMPTS_EXCEEDED"
                if attempts >= OTP_MAX_ATTEMPTS
                else "INVALID_VERIFICATION_CODE"
            )

        connection.execute(
            update(email_verification_codes)
            .where(email_verification_codes.c.id == record["id"])
            .values(consumed_at=now, attempt_count=attempts)
        )
        connection.execute(
            update(users)
            .where(users.c.id == user["id"])
            .values(email_verified=True, approval_status="PENDING_ADMIN", updated_at=now)
        )
        connection.execute(
            update(sessions)
            .where(sessions.c.user_id == user["id"], sessions.c.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        verified = dict(user)
        verified["email_verified"] = True
        verified["approval_status"] = "PENDING_ADMIN"
        verified["updated_at"] = now
        return public_user(verified)


def create_session(user_id: str, *, engine: Engine | None = None) -> tuple[str, str, float]:
    chosen = _engine(engine)
    token = secrets.token_urlsafe(48)
    csrf = secrets.token_urlsafe(32)
    now = time.time()
    expires = now + SESSION_SECONDS
    with chosen.begin() as connection:
        connection.execute(sessions.insert().values(
            token_hash=_token_hash(token), user_id=user_id, csrf_token=csrf,
            created_at=now, expires_at=expires, last_seen_at=now, revoked_at=None,
        ))
    return token, csrf, expires


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE, token, max_age=SESSION_SECONDS, httponly=True, secure=True,
        samesite="none", path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="none")


def request_session_token(request: Request) -> tuple[str, str]:
    authorization = str(request.headers.get("Authorization", "")).strip()
    prefix = f"{USER_AUTH_SCHEME} "
    if authorization.startswith(prefix):
        token = authorization[len(prefix):].strip()
        if token:
            return token, "header"
    cookie = str(request.cookies.get(SESSION_COOKIE, "") or "").strip()
    if cookie:
        return cookie, "cookie"
    return "", "none"


def session_snapshot(token: str, *, engine: Engine | None = None) -> tuple[CurrentUser, str] | None:
    if not token:
        return None
    chosen = _engine(engine)
    now = time.time()
    with chosen.begin() as connection:
        session = connection.execute(select(sessions).where(sessions.c.token_hash == _token_hash(token))).mappings().first()
        if not session or session["revoked_at"] is not None or float(session["expires_at"]) <= now:
            return None
        user = connection.execute(select(users).where(users.c.id == session["user_id"])).mappings().first()
        if not user or not user["is_active"]:
            return None
        if str(user["role"]) == "user" and (
            not bool(user["email_verified"])
            or str(user.get("approval_status") or "APPROVED") != "APPROVED"
        ):
            return None
        connection.execute(update(sessions).where(sessions.c.token_hash == session["token_hash"]).values(last_seen_at=now))
    return CurrentUser(
        str(user["id"]), str(user["email"]), str(user["role"]),
        bool(user["email_verified"]), str(user.get("full_name") or ""),
    ), str(session["csrf_token"])


def current_user(request: Request) -> CurrentUser:
    token, _source = request_session_token(request)
    snapshot = session_snapshot(token)
    if not snapshot:
        raise HTTPException(status_code=401, detail="AUTHENTICATION_REQUIRED")
    return snapshot[0]


def current_user_with_csrf(request: Request) -> CurrentUser:
    token, _source = request_session_token(request)
    snapshot = session_snapshot(token)
    if not snapshot:
        raise HTTPException(status_code=401, detail="AUTHENTICATION_REQUIRED")
    supplied = request.headers.get("X-FlowSignal-CSRF", "")
    if not supplied or not hmac.compare_digest(supplied, snapshot[1]):
        raise HTTPException(status_code=403, detail="CSRF_VALIDATION_FAILED")
    return snapshot[0]


def revoke_session(token: str, *, engine: Engine | None = None) -> None:
    if not token:
        return
    chosen = _engine(engine)
    with chosen.begin() as connection:
        connection.execute(update(sessions).where(sessions.c.token_hash == _token_hash(token)).values(revoked_at=time.time()))


def change_password(user_id: str, current_password: str, new_password: str, current_token: str,
                    *, engine: Engine | None = None) -> dict[str, Any]:
    chosen = _engine(engine)
    now = time.time()
    new_hash = hash_password(new_password)
    if hmac.compare_digest(str(new_password), str(current_password)):
        raise RuntimeError("NEW_PASSWORD_MUST_DIFFER")
    with chosen.begin() as connection:
        user = connection.execute(select(users).where(users.c.id == user_id)).mappings().first()
        if not user or not user["is_active"] or not verify_password(current_password, str(user["password_hash"])):
            raise RuntimeError("CURRENT_PASSWORD_INVALID")
        if verify_password(new_password, str(user["password_hash"])):
            raise RuntimeError("NEW_PASSWORD_MUST_DIFFER")
        connection.execute(update(users).where(users.c.id == user_id).values(password_hash=new_hash, updated_at=now))
        connection.execute(update(sessions).where(
            sessions.c.user_id == user_id,
            sessions.c.revoked_at.is_(None),
            sessions.c.token_hash != _token_hash(current_token),
        ).values(revoked_at=now))
    return {"ok": True, "other_sessions_revoked": True}


def require_admin(request: Request) -> CurrentUser:
    user = current_user(request)
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="ADMIN_REQUIRED")
    return user
