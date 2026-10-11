from __future__ import annotations

import secrets
import time
import uuid
from typing import Any

from sqlalchemy import Column, Float, Integer, String, Table, Text, func, select, update
from sqlalchemy.engine import Engine

from services.email_service import send_password_reset_email
from services.user_auth_service import (
    OTP_MAX_ATTEMPTS,
    OTP_MAX_SENDS_PER_HOUR,
    OTP_RE,
    OTP_RESEND_SECONDS,
    OTP_TTL_SECONDS,
    _engine,
    _hash_otp,
    _verify_otp,
    hash_password,
    mask_email,
    metadata,
    normalize_email,
    sessions,
    users,
)

password_reset_codes = Table(
    "flowsignal_password_reset_codes",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("user_id", String(36), nullable=False, index=True),
    Column("code_hash", Text, nullable=False),
    Column("created_at", Float, nullable=False),
    Column("expires_at", Float, nullable=False, index=True),
    Column("consumed_at", Float),
    Column("attempt_count", Integer, nullable=False, default=0),
)


def request_password_reset(email: str, *, engine: Engine | None = None) -> dict[str, Any]:
    chosen = _engine(engine)
    normalized = normalize_email(email)
    now = time.time()

    with chosen.begin() as connection:
        user = connection.execute(select(users).where(users.c.email == normalized)).mappings().first()
        if not user or not user["is_active"]:
            return {
                "ok": True,
                "email": mask_email(normalized),
                "expires_in": OTP_TTL_SECONDS,
                "resend_after": OTP_RESEND_SECONDS,
            }

        latest = connection.execute(
            select(password_reset_codes)
            .where(password_reset_codes.c.user_id == user["id"])
            .order_by(password_reset_codes.c.created_at.desc())
            .limit(1)
        ).mappings().first()
        if latest and float(latest["created_at"]) > now - OTP_RESEND_SECONDS:
            raise RuntimeError("RESET_CODE_COOLDOWN")

        sends_last_hour = connection.execute(
            select(func.count())
            .select_from(password_reset_codes)
            .where(
                password_reset_codes.c.user_id == user["id"],
                password_reset_codes.c.created_at >= now - 3600,
            )
        ).scalar_one()
        if int(sends_last_hour or 0) >= OTP_MAX_SENDS_PER_HOUR:
            raise RuntimeError("RESET_RATE_LIMITED")

        connection.execute(
            update(password_reset_codes)
            .where(
                password_reset_codes.c.user_id == user["id"],
                password_reset_codes.c.consumed_at.is_(None),
            )
            .values(consumed_at=now)
        )
        code = f"{secrets.randbelow(1_000_000):06d}"
        code_id = str(uuid.uuid4())
        connection.execute(password_reset_codes.insert().values(
            id=code_id,
            user_id=user["id"],
            code_hash=_hash_otp(code),
            created_at=now,
            expires_at=now + OTP_TTL_SECONDS,
            consumed_at=None,
            attempt_count=0,
        ))

    try:
        send_password_reset_email(normalized, code)
    except Exception as exc:
        with chosen.begin() as connection:
            connection.execute(
                update(password_reset_codes)
                .where(password_reset_codes.c.id == code_id)
                .values(consumed_at=time.time())
            )
        if isinstance(exc, RuntimeError):
            raise
        raise RuntimeError("EMAIL_DELIVERY_FAILED") from exc

    return {
        "ok": True,
        "email": mask_email(normalized),
        "expires_in": OTP_TTL_SECONDS,
        "resend_after": OTP_RESEND_SECONDS,
    }


def reset_password(email: str, code: str, new_password: str, *, engine: Engine | None = None) -> dict[str, Any]:
    chosen = _engine(engine)
    normalized = normalize_email(email)
    supplied = str(code or "").strip()
    if not OTP_RE.fullmatch(supplied):
        raise RuntimeError("INVALID_RESET_CODE")
    new_hash = hash_password(new_password)
    now = time.time()

    with chosen.begin() as connection:
        user = connection.execute(select(users).where(users.c.email == normalized)).mappings().first()
        if not user or not user["is_active"]:
            raise RuntimeError("INVALID_RESET_CODE")

        record = connection.execute(
            select(password_reset_codes)
            .where(
                password_reset_codes.c.user_id == user["id"],
                password_reset_codes.c.consumed_at.is_(None),
            )
            .order_by(password_reset_codes.c.created_at.desc())
            .limit(1)
        ).mappings().first()
        if not record or float(record["expires_at"]) <= now:
            if record:
                connection.execute(
                    update(password_reset_codes)
                    .where(password_reset_codes.c.id == record["id"])
                    .values(consumed_at=now)
                )
            raise RuntimeError("RESET_CODE_EXPIRED")

        attempts = int(record["attempt_count"] or 0)
        if attempts >= OTP_MAX_ATTEMPTS:
            connection.execute(
                update(password_reset_codes)
                .where(password_reset_codes.c.id == record["id"])
                .values(consumed_at=now)
            )
            raise RuntimeError("RESET_ATTEMPTS_EXCEEDED")

        if not _verify_otp(supplied, str(record["code_hash"])):
            attempts += 1
            values: dict[str, Any] = {"attempt_count": attempts}
            if attempts >= OTP_MAX_ATTEMPTS:
                values["consumed_at"] = now
            connection.execute(
                update(password_reset_codes)
                .where(password_reset_codes.c.id == record["id"])
                .values(**values)
            )
            raise RuntimeError(
                "RESET_ATTEMPTS_EXCEEDED" if attempts >= OTP_MAX_ATTEMPTS else "INVALID_RESET_CODE"
            )

        connection.execute(
            update(password_reset_codes)
            .where(password_reset_codes.c.id == record["id"])
            .values(consumed_at=now, attempt_count=attempts)
        )
        connection.execute(
            update(users)
            .where(users.c.id == user["id"])
            .values(password_hash=new_hash, updated_at=now)
        )
        connection.execute(
            update(sessions)
            .where(sessions.c.user_id == user["id"], sessions.c.revoked_at.is_(None))
            .values(revoked_at=now)
        )

    return {"ok": True, "password_reset": True}
