"""Encrypted durable storage for cTrader OAuth tokens.

The database never stores the raw access or refresh token. A dedicated
CTRADER_TOKEN_ENCRYPTION_KEY is preferred; the stable cTrader client secret is
used as a backwards-compatible key source when that setting is absent.
"""

import base64
import hashlib
import os
import threading
from datetime import datetime, timezone

from cryptography.fernet import Fernet, InvalidToken

from db import SessionLocal
from models import CTraderOAuthToken


PROVIDER = "ctrader"
_LOCK = threading.RLock()


def _key_material():
    return (
        os.getenv("CTRADER_TOKEN_ENCRYPTION_KEY")
        or os.getenv("CTRADER_CLIENT_SECRET")
        or ""
    ).strip()


def _fernet():
    material = _key_material()
    if not material:
        raise RuntimeError(
            "CTRADER_TOKEN_ENCRYPTION_KEY or CTRADER_CLIENT_SECRET is required "
            "for durable cTrader token storage"
        )
    key = base64.urlsafe_b64encode(
        hashlib.sha256(f"flowsignal:ctrader:{material}".encode("utf-8")).digest()
    )
    return Fernet(key)


def save_tokens(
    access_token,
    refresh_token,
    session_factory=None,
    updated_by="ctrader_oauth",
):
    access = str(access_token or "").strip()
    refresh = str(refresh_token or "").strip()
    if not access:
        return False

    cipher = _fernet()
    encrypted_access = cipher.encrypt(access.encode("utf-8")).decode("ascii")
    encrypted_refresh = (
        cipher.encrypt(refresh.encode("utf-8")).decode("ascii")
        if refresh
        else None
    )
    factory = session_factory or SessionLocal
    try:
        with _LOCK:
            with factory() as session:
                row = session.get(CTraderOAuthToken, PROVIDER)
                if row is None:
                    row = CTraderOAuthToken(provider=PROVIDER)
                    session.add(row)
                row.encrypted_access_token = encrypted_access
                row.encrypted_refresh_token = encrypted_refresh
                row.updated_at = datetime.now(timezone.utc)
                row.updated_by = str(updated_by or "ctrader_oauth")
                session.commit()
    except Exception as exc:
        print("CTRADER_TOKEN_DURABLE_SAVE_ERROR =", type(exc).__name__)
        return False
    return True


def load_tokens(session_factory=None):
    factory = session_factory or SessionLocal
    try:
        with factory() as session:
            row = session.get(CTraderOAuthToken, PROVIDER)
            if row is None:
                return {}
            encrypted_access = row.encrypted_access_token
            encrypted_refresh = row.encrypted_refresh_token
            updated_at = row.updated_at
    except Exception as exc:
        print("CTRADER_TOKEN_DURABLE_LOAD_ERROR =", type(exc).__name__)
        return {}

    try:
        cipher = _fernet()
        access = cipher.decrypt(encrypted_access.encode("ascii")).decode("utf-8")
        refresh = (
            cipher.decrypt(encrypted_refresh.encode("ascii")).decode("utf-8")
            if encrypted_refresh
            else ""
        )
    except (InvalidToken, ValueError, TypeError) as exc:
        print("CTRADER_TOKEN_DURABLE_DECRYPT_ERROR =", type(exc).__name__)
        return {}

    return {
        "access_token": access,
        "refresh_token": refresh,
        "updated_at": updated_at.isoformat() if updated_at else None,
        "source": "encrypted_database",
    }


def clear_tokens(session_factory=None):
    factory = session_factory or SessionLocal
    try:
        with _LOCK:
            with factory() as session:
                row = session.get(CTraderOAuthToken, PROVIDER)
                if row is not None:
                    session.delete(row)
                    session.commit()
    except Exception as exc:
        print("CTRADER_TOKEN_DURABLE_CLEAR_ERROR =", type(exc).__name__)
        return False
    return True
