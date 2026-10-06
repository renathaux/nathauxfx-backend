from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from functools import wraps

from fastapi.responses import JSONResponse
from sqlalchemy import text

from db import engine as database_engine

# These are owner/global trading-state endpoints. Customer Forex remains
# signal/read-only. The existing owner login token is preserved as authority.
EXACT_PATHS = {
    "/market-data-source",
    "/paper-auto-toggle",
    "/live-auto-toggle",
    "/execute-trade",
    "/execute-live-order",
    "/connect-ctrader",
    "/refresh-ctrader-accounts",
    "/set-active-ctrader-account",
    "/forget-ctrader-account",
    "/disconnect-ctrader",
    "/close-live-trade",
    "/modify-live-position-levels",
    "/ctrader/disconnect",
    "/ctrader/accounts/refresh",
    "/ctrader/accounts/active",
    "/ctrader/accounts/forget",
    "/ctrader/accounts/clear",
}
PREFIX_PATHS = (
    "/settings/",
    "/strategy/settings",
)
# Owner sessions should survive browser closes and normal deploys. Ten years is
# effectively "until logout" for the product while still keeping a finite TTL.
OWNER_SESSION_TTL = timedelta(days=3650)


def _sensitive(path: str, method: str) -> bool:
    if path in EXACT_PATHS:
        return True
    if method.upper() in {"POST", "PUT", "PATCH", "DELETE"} and any(path.startswith(prefix) for prefix in PREFIX_PATHS):
        return True
    return False


def _bearer(headers) -> str:
    raw = str(headers.get("authorization") or "")
    if not raw.lower().startswith("bearer "):
        return ""
    return raw.split(" ", 1)[1].strip()


def _token_hash(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def _persist_owner_session(token: str) -> bool:
    """Persist only a token hash, never the raw owner bearer token."""
    if not token:
        return False
    now = datetime.now(timezone.utc)
    expires_at = now + OWNER_SESSION_TTL
    try:
        with database_engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO flowsignal_owner_sessions
                        (token_hash, created_at, last_seen_at, expires_at)
                    VALUES
                        (:token_hash, :now, :now, :expires_at)
                    ON CONFLICT (token_hash) DO UPDATE SET
                        last_seen_at = EXCLUDED.last_seen_at,
                        expires_at = EXCLUDED.expires_at
                    """
                ),
                {
                    "token_hash": _token_hash(token),
                    "now": now,
                    "expires_at": expires_at,
                },
            )
            connection.execute(
                text("DELETE FROM flowsignal_owner_sessions WHERE expires_at <= :now"),
                {"now": now},
            )
        return True
    except Exception as exc:
        # Persistence is a deploy-survival enhancement. A currently valid
        # in-memory owner session must not be blocked if Neon is temporarily
        # unavailable.
        print("OWNER_SESSION_PERSIST_WARNING =", type(exc).__name__)
        return False


def persist_owner_session(token: str) -> bool:
    """Persist a freshly authenticated owner token immediately at login."""
    persisted = _persist_owner_session(token)
    print("OWNER_SESSION_LOGIN_PERSIST =", {
        "persisted": bool(persisted),
        "ttl_hours": int(OWNER_SESSION_TTL.total_seconds() // 3600),
    })
    return persisted


def _persisted_owner_session_valid(token: str) -> bool:
    if not token:
        return False
    now = datetime.now(timezone.utc)
    try:
        with database_engine.begin() as connection:
            row = connection.execute(
                text(
                    """
                    SELECT token_hash
                    FROM flowsignal_owner_sessions
                    WHERE token_hash = :token_hash
                      AND expires_at > :now
                    LIMIT 1
                    """
                ),
                {"token_hash": _token_hash(token), "now": now},
            ).first()
            if row is None:
                return False
            connection.execute(
                text(
                    """
                    UPDATE flowsignal_owner_sessions
                    SET last_seen_at = :now,
                        expires_at = :expires_at
                    WHERE token_hash = :token_hash
                    """
                ),
                {
                    "token_hash": _token_hash(token),
                    "now": now,
                    "expires_at": now + OWNER_SESSION_TTL,
                },
            )
            return True
    except Exception as exc:
        print("OWNER_SESSION_LOOKUP_WARNING =", type(exc).__name__)
        return False


def validate_owner_session(token: str, legacy_sessions: dict) -> bool:
    """Validate and, when needed, rehydrate one owner token."""
    token = str(token or "").strip()
    if not token:
        return False
    session = legacy_sessions.get(token)
    role = (
        str(session.get("role") or "").lower()
        if isinstance(session, dict)
        else ""
    )
    if role == "admin":
        _persist_owner_session(token)
        return True
    if not _persisted_owner_session_valid(token):
        return False
    legacy_sessions[token] = {
        "email": "persisted-owner-session",
        "role": "admin",
        "auth_method": "persisted_owner_session",
    }
    return True


def revoke_owner_session(token: str, legacy_sessions: dict) -> bool:
    """Revoke an owner token both in memory and in durable storage."""
    token = str(token or "").strip()
    if not token:
        return True
    legacy_sessions.pop(token, None)
    try:
        with database_engine.begin() as connection:
            connection.execute(
                text("DELETE FROM flowsignal_owner_sessions WHERE token_hash = :token_hash"),
                {"token_hash": _token_hash(token)},
            )
        return True
    except Exception as exc:
        print("OWNER_SESSION_REVOKE_WARNING =", type(exc).__name__)
        return False


def install_owner_forex_mutation_guard(app, legacy_sessions: dict) -> dict:
    """Wrap sensitive legacy routes after route registration.

    Customer Forex stays read-only. Owner mutations require an admin session.
    Owner sessions are persisted as hashes in Neon at login and refreshed here,
    so a Render restart/deploy does not invalidate an authenticated owner tab.
    """
    installed = 0
    for route in list(getattr(app, "routes", [])):
        path = str(getattr(route, "path", "") or "")
        methods = set(getattr(route, "methods", set()) or set())
        if not path or not any(_sensitive(path, method) for method in methods):
            continue
        if getattr(route, "_flowsignal_owner_guarded", False):
            continue
        original = route.app

        @wraps(original)
        async def guarded(scope, receive, send, _original=original):
            method = str(scope.get("method") or "GET").upper()
            request_path = str(scope.get("path") or "")
            if _sensitive(request_path, method):
                headers = {
                    k.decode("latin-1").lower(): v.decode("latin-1")
                    for k, v in scope.get("headers", [])
                }
                token = _bearer(headers)
                if not token:
                    response = JSONResponse(
                        {
                            "ok": False,
                            "reason": "OWNER_SESSION_REQUIRED",
                            "detail": "OWNER_SESSION_REQUIRED",
                        },
                        status_code=401,
                    )
                    await response(scope, receive, send)
                    return

                session = legacy_sessions.get(token)
                if validate_owner_session(token, legacy_sessions):
                    pass
                elif isinstance(session, dict):
                    response = JSONResponse(
                        {
                            "ok": False,
                            "reason": "ADMIN_FOREX_MUTATION_REQUIRED",
                            "detail": "ADMIN_FOREX_MUTATION_REQUIRED",
                        },
                        status_code=403,
                    )
                    await response(scope, receive, send)
                    return
                else:
                    response = JSONResponse(
                        {
                            "ok": False,
                            "reason": "OWNER_SESSION_EXPIRED",
                            "detail": "OWNER_SESSION_EXPIRED",
                        },
                        status_code=401,
                    )
                    await response(scope, receive, send)
                    return

            await _original(scope, receive, send)

        route.app = guarded
        route._flowsignal_owner_guarded = True
        installed += 1
    return {"ok": True, "guarded_routes": installed}
