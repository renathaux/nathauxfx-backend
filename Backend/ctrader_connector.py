# =========================
# 📡 CTRADER CONNECTOR
# =========================
import json
import os
import base64
import socket
import ssl
import struct
import uuid
import threading
import time
import re
import math
from urllib.parse import urlencode
import pandas as pd
import requests
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from pathlib import Path
from paths import CANDLE_CACHE_DIR, DATA_DIR
from services.broker_account_state_service import (
    load_active_account_selection,
    save_active_account_selection,
)
from services.ctrader_token_service import (
    clear_tokens as clear_durable_ctrader_tokens,
    load_tokens as load_durable_ctrader_tokens,
    save_tokens as save_durable_ctrader_tokens,
)
from services.settings_service import get_tp1_ratio_of_tp2

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*args, **kwargs):
        return False

ENV_PATH = Path(__file__).resolve().parent / ".env"
CTRADER_ACCOUNTS_PATH = DATA_DIR / "ctrader_accounts.json"
CTRADER_CANDLE_CACHE_DIR = CANDLE_CACHE_DIR
load_dotenv(dotenv_path=ENV_PATH)

def read_env_file_values():
    values = {}

    if not ENV_PATH.exists():
        return values

    try:
        lines = ENV_PATH.read_text().splitlines()
    except Exception:
        return values

    for line in lines:
        stripped = line.strip()

        if not stripped or stripped.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")

    return values

def load_ctrader_env_file_if_needed():
    values = read_env_file_values()

    for key, value in values.items():
        if key.startswith("CTRADER_") and not os.getenv(key):
            os.environ[key] = value

    redirect_uri = values.get("CTRADER_REDIRECT_URI")

    if redirect_uri:
        os.environ["CTRADER_REDIRECT_URI"] = redirect_uri

load_ctrader_env_file_if_needed()

print("CTRADER_REDIRECT_URI_LOADED =", {
    "env_value": os.getenv("CTRADER_REDIRECT_URI"),
    "file_value": read_env_file_values().get("CTRADER_REDIRECT_URI"),
})

print("CTRADER ENV LOADED")

CTRADER_TOKEN_HYDRATION = {
    "checked_at": 0.0,
    "loaded": False,
    "source": None,
}
CTRADER_TOKEN_HYDRATION_SECONDS = 30


def hydrate_ctrader_tokens_from_storage(force=False):
    """Prefer encrypted database tokens over deployment-time token values."""
    now = time.time()
    if (
        not force
        and CTRADER_TOKEN_HYDRATION["checked_at"]
        and now - CTRADER_TOKEN_HYDRATION["checked_at"]
        < CTRADER_TOKEN_HYDRATION_SECONDS
    ):
        return dict(CTRADER_TOKEN_HYDRATION)

    stored = load_durable_ctrader_tokens()
    access_token = str(stored.get("access_token") or "").strip()
    refresh_token = str(stored.get("refresh_token") or "").strip()
    if access_token:
        os.environ["CTRADER_ACCESS_TOKEN"] = access_token
        if refresh_token:
            os.environ["CTRADER_REFRESH_TOKEN"] = refresh_token
        CTRADER_TOKEN_HYDRATION.update({
            "checked_at": now,
            "loaded": True,
            "source": "encrypted_database",
        })
        print("CTRADER_TOKEN_DURABLE_LOAD =", {
            "ok": True,
            "source": "encrypted_database",
            "has_access_token": True,
            "has_refresh_token": bool(refresh_token),
        })
    else:
        CTRADER_TOKEN_HYDRATION.update({
            "checked_at": now,
            "loaded": False,
            "source": "environment_fallback",
        })
    return dict(CTRADER_TOKEN_HYDRATION)


def persist_ctrader_tokens(access_token, refresh_token, updated_by):
    saved = save_durable_ctrader_tokens(
        access_token,
        refresh_token,
        updated_by=updated_by,
    )
    if saved:
        CTRADER_TOKEN_HYDRATION.update({
            "checked_at": time.time(),
            "loaded": True,
            "source": "encrypted_database",
        })
    print("CTRADER_TOKEN_DURABLE_SAVE =", {
        "ok": bool(saved),
        "updated_by": str(updated_by),
        "has_access_token": bool(access_token),
        "has_refresh_token": bool(refresh_token),
    })
    return bool(saved)

class CTraderApiError(RuntimeError):
    def __init__(self, message, data=None):
        super().__init__(message)
        self.data = data or {}

def describe_ctrader_error(error):
    data = getattr(error, "data", None)

    if isinstance(data, dict):
        payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
        reason = (
            payload.get("description")
            or payload.get("errorCode")
            or payload.get("error_code")
            or payload.get("message")
            or data.get("description")
            or data.get("errorCode")
            or data.get("message")
        )

        if reason:
            return f"{error}: {reason}"

    return str(error)

CONNECTED = {
    "connected": False,
    "status": False,
    "mode": "demo",   # demo or live
    "account_id": None,
    "execution_ready": False
}

DEBUG_OPEN_POSITIONS = None
LAST_CTRADER_CANDLE_ERROR = None
LAST_CTRADER_CANDLE_SUCCESS = None
LAST_CTRADER_POSITION_FETCH_ERROR = None
CTRADER_CANDLE_CACHE = {}
CTRADER_CANDLE_MISS_LIMIT = 3
CTRADER_CANDLE_RECOVERY_MAX_AGE_SECONDS = {
    "5m": 15 * 60,
    "15m": 45 * 60,
    "1h": 150 * 60,
}
LIVE_TICKS = {
    "EURUSD": {
        "bid": None,
        "ask": None,
        "mid": None,
        "timestamp": None,
    },
    "XAUUSD": {
        "bid": None,
        "ask": None,
        "mid": None,
        "timestamp": None,
    },
}
CTRADER_CONNECTION_CACHE = {
    "checked_at": 0,
    "state": None,
    "last_success_at": 0,
    "consecutive_failures": 0,
}
CTRADER_CONNECTION_CACHE_SECONDS = 15
CTRADER_CONNECTION_GRACE_SECONDS = 90
LIVE_TICKS_LOCK = threading.RLock()
LIVE_PRICE_THREAD = None
LIVE_PRICE_THREAD_STARTED = False
LIVE_PRICE_LAST_ERROR = None
LIVE_PRICE_STALE_SECONDS = 10
CTRADER_HEARTBEAT_PAYLOAD_TYPE = 51
CTRADER_HEARTBEAT_INTERVAL_SECONDS = 8
CTRADER_STREAM_READ_TIMEOUT_SECONDS = 2
LIVE_TICK_CANDLE_STALE_SECONDS = 90

CTRADER_JSON_ENDPOINTS = {
    "demo": ("demo.ctraderapi.com", 5036),
    "live": ("live.ctraderapi.com", 5036),
}
CTRADER_TOKEN_URL = "https://openapi.ctrader.com/apps/token"
CTRADER_AUTH_URL = "https://id.ctrader.com/my/settings/openapi/grantingaccess/"

PAYLOAD_APPLICATION_AUTH_REQ = 2100
PAYLOAD_APPLICATION_AUTH_RES = 2101
PAYLOAD_ACCOUNT_AUTH_REQ = 2102
PAYLOAD_ACCOUNT_AUTH_RES = 2103
PAYLOAD_NEW_ORDER_REQ = 2106
PAYLOAD_AMEND_POSITION_SLTP_REQ = 2110
PAYLOAD_CLOSE_POSITION_REQ = 2111
PAYLOAD_SYMBOLS_LIST_REQ = 2114
PAYLOAD_SYMBOLS_LIST_RES = 2115
PAYLOAD_TRADER_REQ = 2121
PAYLOAD_TRADER_RES = 2122
PAYLOAD_RECONCILE_REQ = 2124
PAYLOAD_RECONCILE_RES = 2125
PAYLOAD_EXECUTION_EVENT = 2126
PAYLOAD_SUBSCRIBE_SPOTS_REQ = 2127
PAYLOAD_SUBSCRIBE_SPOTS_RES = 2128
PAYLOAD_SPOT_EVENT = 2131
PAYLOAD_ORDER_ERROR_EVENT = 2132
PAYLOAD_DEAL_LIST_REQ = 2133
PAYLOAD_DEAL_LIST_RES = 2134
PAYLOAD_ORDER_LIST_REQ = 2175
PAYLOAD_ORDER_LIST_RES = 2176
PAYLOAD_GET_TRENDBARS_REQ = 2137
PAYLOAD_GET_TRENDBARS_RES = 2138
PAYLOAD_ERROR_RES = 2142
PAYLOAD_GET_ACCOUNT_LIST_BY_ACCESS_TOKEN_REQ = 2149
PAYLOAD_GET_ACCOUNT_LIST_BY_ACCESS_TOKEN_RES = 2150
PAYLOAD_GET_POSITION_UNREALIZED_PNL_REQ = 2187
PAYLOAD_GET_POSITION_UNREALIZED_PNL_RES = 2188

DEFAULT_CTRADER_ACCOUNT_SETTINGS = {
    "active_account_id": None,
    "active_account_env": None,
    "accounts": [],
    "forgotten_account_ids": [],
    "last_refresh": None,
    "active_account_snapshot": None,
    "active_account_snapshot_refreshed_at": None,
}

CTRADER_UNAUTHORIZED_ACCOUNT_MESSAGE = (
    "Selected cTrader account is not authorized. Reconnect or choose an authorized account."
)
CTRADER_ACCOUNT_AUTH_SELECTION_DEBUG = {
    "active_account_id": None,
    "authorized_account_ids": [],
    "selected_account_source": None,
    "is_active_account_authorized": False,
    "reason": None,
}

TRADE_LEVEL_RULES = {
    "EURUSD": {
        "min_distance": 0.0008,
        "precision": 5,
        "pip_size": 0.0001,
    },
    "XAUUSD": {
        "min_distance": 1.5,
        "precision": 2,
        "pip_size": 0.01,
    },
}

CTRADER_PAYLOAD_VOLUME_SCALE = {
    "EURUSD": 100,
    "XAUUSD": 100,
}

CTRADER_TRENDBAR_PERIODS = {
    "5m": 5,
    "5min": 5,
    "m5": 5,
    "15m": 7,
    "15min": 7,
    "m15": 7,
    "1h": 9,
    "h1": 9,
}

CTRADER_TRENDBAR_PERIOD_MINUTES = {
    5: 5,
    7: 15,
    9: 60,
}

CTRADER_CANDLE_TTLS = {
    "5m": 15,
    "5min": 15,
    "m5": 15,
    "15m": 60,
    "15min": 60,
    "m15": 60,
    "1h": 300,
    "h1": 300,
}


def check_ctrader_connection_capability(force=False):
    config = get_ctrader_config()
    now = time.time()
    cached_state = CTRADER_CONNECTION_CACHE.get("state")

    if (
        not force
        and cached_state
        and now - CTRADER_CONNECTION_CACHE.get("checked_at", 0) < CTRADER_CONNECTION_CACHE_SECONDS
    ):
        return dict(cached_state)

    local_connected = bool(
        CONNECTED.get("connected")
        or CONNECTED.get("status")
    )
    state = {
        "connected": False,
        "status": bool(CONNECTED.get("status")),
        "mode": CONNECTED.get("mode") or (config.get("env") if config else "demo"),
        "account_id": CONNECTED.get("account_id") if local_connected else None,
        "execution_ready": False,
        "auth_ok": False,
        "account_found": False,
        "reason": "missing or invalid cTrader config",
        **get_ctrader_account_selection_debug(),
    }

    if config:
        state["mode"] = config.get("env") or state["mode"]
        state["account_id"] = config.get("account_id")

        try:
            host, port = CTRADER_JSON_ENDPOINTS[config["env"]]
            account_id = int(config["account_id"])
            sock = open_ctrader_json_socket(host, port)

            try:
                auth_state = authorize_ctrader_socket(sock, config, account_id)
            finally:
                try:
                    sock.close()
                except Exception:
                    pass

            state.update({
                "connected": True,
                "status": True,
                "execution_ready": True,
                "auth_ok": True,
                "account_found": bool(
                    auth_state.get("configured_account_id_found")
                ),
                "reason": "authenticated",
                "degraded": False,
                **get_ctrader_account_selection_debug(),
            })
            CTRADER_CONNECTION_CACHE["last_success_at"] = now
            CTRADER_CONNECTION_CACHE["consecutive_failures"] = 0
        except Exception as e:
            state["reason"] = str(e)
            state["connected"] = False
            state["status"] = bool(CONNECTED.get("status"))
            state["execution_ready"] = False
            state["auth_ok"] = False
            state["account_found"] = False
            state.update(get_ctrader_account_selection_debug())

    if not state["connected"]:
        try:
            positions = get_open_positions()
            position_error = get_ctrader_position_fetch_error()

            if position_error is None:
                state.update({
                    "connected": True,
                    "status": True,
                    "execution_ready": bool(config),
                    "reason": "positions fetched",
                    **get_ctrader_account_selection_debug(),
                })

                if config:
                    state["account_id"] = config.get("account_id")
                state["degraded"] = False
                CTRADER_CONNECTION_CACHE["last_success_at"] = now
                CTRADER_CONNECTION_CACHE["consecutive_failures"] = 0
        except Exception:
            pass

    if not state["connected"]:
        CTRADER_CONNECTION_CACHE["consecutive_failures"] = (
            int(CTRADER_CONNECTION_CACHE.get("consecutive_failures") or 0) + 1
        )
        last_success_at = float(
            CTRADER_CONNECTION_CACHE.get("last_success_at") or 0
        )
        previous_state = CTRADER_CONNECTION_CACHE.get("state") or {}
        within_grace = (
            last_success_at > 0
            and now - last_success_at <= CTRADER_CONNECTION_GRACE_SECONDS
        )

        if within_grace and previous_state.get("connected"):
            failure_reason = state.get("reason")
            state.update({
                "connected": True,
                "status": True,
                "execution_ready": False,
                "auth_ok": bool(previous_state.get("auth_ok")),
                "account_found": bool(previous_state.get("account_found")),
                "account_id": (
                    state.get("account_id")
                    or previous_state.get("account_id")
                ),
                "reason": f"temporary cTrader interruption: {failure_reason}",
                "degraded": True,
            })

    state["consecutive_failures"] = int(
        CTRADER_CONNECTION_CACHE.get("consecutive_failures") or 0
    )
    state["last_success_at"] = (
        CTRADER_CONNECTION_CACHE.get("last_success_at") or None
    )

    CTRADER_CONNECTION_CACHE["checked_at"] = now
    CTRADER_CONNECTION_CACHE["state"] = dict(state)

    return state

def get_connection_state(force=False):
    return check_ctrader_connection_capability(force=force)


def get_ctrader_connection_snapshot():
    """Return status immediately without opening a broker socket."""
    now = time.time()
    cached_state = CTRADER_CONNECTION_CACHE.get("state")
    if isinstance(cached_state, dict):
        state = dict(cached_state)
        checked_at = float(CTRADER_CONNECTION_CACHE.get("checked_at") or 0)
        status_age = max(0.0, now - checked_at) if checked_at else None
        state["status_age_seconds"] = status_age
        if (
            state.get("connected")
            and status_age is not None
            and status_age > CTRADER_CONNECTION_GRACE_SECONDS
        ):
            state["degraded"] = True
            state["execution_ready"] = False
            state["reason"] = "broker verification delayed; last confirmed state shown"
        state["snapshot_source"] = "connection_cache"
        return state

    config = get_ctrader_config()
    local_connected = bool(CONNECTED.get("connected") or CONNECTED.get("status"))
    selection = get_ctrader_account_selection_debug()
    return {
        "connected": local_connected,
        "status": local_connected,
        "mode": CONNECTED.get("mode") or (config.get("env") if config else "demo"),
        "account_id": (
            CONNECTED.get("account_id")
            or (config.get("account_id") if config else None)
        ),
        "execution_ready": False,
        "auth_ok": False,
        "account_found": False,
        "reason": "broker verification pending" if config else "missing or invalid cTrader config",
        "degraded": bool(config),
        "consecutive_failures": int(
            CTRADER_CONNECTION_CACHE.get("consecutive_failures") or 0
        ),
        "last_success_at": CTRADER_CONNECTION_CACHE.get("last_success_at") or None,
        "status_age_seconds": None,
        "snapshot_source": "local_selection",
        **selection,
    }

def load_ctrader_account_settings():
    data = {}
    if CTRADER_ACCOUNTS_PATH.exists():
        try:
            data = json.loads(CTRADER_ACCOUNTS_PATH.read_text())
        except Exception:
            data = {}

    settings = dict(DEFAULT_CTRADER_ACCOUNT_SETTINGS)

    if isinstance(data, dict):
        settings.update({
            key: data.get(key, settings[key])
            for key in settings
        })

    if not isinstance(settings.get("accounts"), list):
        settings["accounts"] = []

    if not isinstance(settings.get("forgotten_account_ids"), list):
        settings["forgotten_account_ids"] = []

    durable_selection = load_active_account_selection()
    if not settings.get("active_account_id") and durable_selection.get("active_account_id"):
        settings["active_account_id"] = durable_selection["active_account_id"]
        settings["active_account_env"] = durable_selection.get("active_account_env")

    return settings

def save_ctrader_account_settings(settings):
    payload = dict(DEFAULT_CTRADER_ACCOUNT_SETTINGS)
    payload.update(settings or {})
    CTRADER_ACCOUNTS_PATH.write_text(json.dumps(payload, indent=2, default=str))
    save_active_account_selection(
        payload.get("active_account_id"),
        payload.get("active_account_env"),
    )
    return payload

def clear_active_ctrader_account_balance_cache(settings=None, persist=True):
    current_settings = settings if isinstance(settings, dict) else load_ctrader_account_settings()
    current_settings["active_account_snapshot"] = None
    current_settings["active_account_snapshot_refreshed_at"] = None

    for account in current_settings.get("accounts", []):
        if not isinstance(account, dict):
            continue
        account["balance_verified"] = False
        account["equity_verified"] = False
        account["balance_source"] = "stale_cache_cleared"
        account["equity_source"] = "stale_cache_cleared"

    if persist:
        save_ctrader_account_settings(current_settings)
    return current_settings

def get_selected_ctrader_account_source():
    settings = load_ctrader_account_settings()

    if settings.get("active_account_id"):
        return "settings"

    if os.getenv("ACTIVE_CTRADER_ACCOUNT_ID"):
        return "env:ACTIVE_CTRADER_ACCOUNT_ID"

    if os.getenv("CTRADER_ACCOUNT_ID"):
        return "env:CTRADER_ACCOUNT_ID"

    return None

def get_active_ctrader_account_id():
    settings = load_ctrader_account_settings()
    return (
        settings.get("active_account_id")
        or os.getenv("ACTIVE_CTRADER_ACCOUNT_ID")
        or os.getenv("CTRADER_ACCOUNT_ID")
    )

def clear_active_ctrader_account_selection(reason, authorized_account_ids=None):
    settings = load_ctrader_account_settings()
    active_account_id = get_active_ctrader_account_id()
    selected_source = get_selected_ctrader_account_source()
    authorized_set = {
        str(account_id)
        for account_id in (authorized_account_ids or [])
        if account_id is not None
    }

    if authorized_set:
        settings["accounts"] = [
            account
            for account in settings.get("accounts", [])
            if str(account.get("account_id")) in authorized_set
        ]

    settings["active_account_id"] = None
    settings["active_account_env"] = None
    save_ctrader_account_settings(settings)

    os.environ.pop("ACTIVE_CTRADER_ACCOUNT_ID", None)
    os.environ.pop("ACTIVE_CTRADER_ACCOUNT_ENV", None)
    os.environ.pop("CTRADER_ACCOUNT_ID", None)
    update_env_file_values({
        "ACTIVE_CTRADER_ACCOUNT_ID": "",
        "ACTIVE_CTRADER_ACCOUNT_ENV": "",
        "CTRADER_ACCOUNT_ID": "",
    })

    CONNECTED["account_id"] = None
    CONNECTED["execution_ready"] = False
    CONNECTED["connected"] = False
    CONNECTED["status"] = False
    clear_ctrader_connection_cache()

    debug = update_ctrader_account_auth_selection_debug(
        active_account_id,
        sorted(authorized_set),
        reason=reason,
    )
    debug["selected_account_source"] = selected_source
    CTRADER_ACCOUNT_AUTH_SELECTION_DEBUG.update(debug)
    print("CTRADER_ACTIVE_ACCOUNT_CLEARED =", {
        "active_account_id": active_account_id,
        "authorized_account_ids": sorted(authorized_set),
        "selected_account_source": selected_source,
        "reason": reason,
    })
    return {
        "ok": True,
        "active_account_id": None,
        "authorized_account_ids": sorted(authorized_set),
        "reason": reason,
    }

def update_ctrader_account_auth_selection_debug(
    active_account_id,
    authorized_account_ids,
    reason=None,
):
    authorized = [
        str(account_id)
        for account_id in (authorized_account_ids or [])
        if account_id is not None
    ]
    active = str(active_account_id) if active_account_id else None
    CTRADER_ACCOUNT_AUTH_SELECTION_DEBUG.update({
        "active_account_id": active,
        "authorized_account_ids": authorized,
        "selected_account_source": get_selected_ctrader_account_source(),
        "is_active_account_authorized": bool(active and active in authorized),
        "reason": reason,
    })
    return dict(CTRADER_ACCOUNT_AUTH_SELECTION_DEBUG)

def get_ctrader_account_selection_debug():
    return dict(CTRADER_ACCOUNT_AUTH_SELECTION_DEBUG)

def normalize_ctrader_env(value=None):
    env = str(value or "").strip().lower()

    if env in CTRADER_JSON_ENDPOINTS:
        return env

    fallback = str(os.getenv("CTRADER_ENV", "demo")).strip().lower()
    return fallback if fallback in CTRADER_JSON_ENDPOINTS else "demo"

def get_ctrader_env_for_account_item(account):
    if not isinstance(account, dict):
        return normalize_ctrader_env()

    env = str(account.get("env") or account.get("mode") or "").strip().lower()

    if env in CTRADER_JSON_ENDPOINTS:
        return env

    is_live = account.get("isLive")

    if is_live is True:
        return "live"

    if is_live is False:
        return "demo"

    return normalize_ctrader_env()

def get_saved_ctrader_account(account_id):
    account_id_text = str(account_id or "").strip()

    if not account_id_text:
        return None

    settings = load_ctrader_account_settings()

    for account in settings.get("accounts", []):
        if str(account.get("account_id")) == account_id_text:
            return account

    return None

def get_active_ctrader_account_env():
    settings = load_ctrader_account_settings()
    active_account_id = (
        settings.get("active_account_id")
        or os.getenv("ACTIVE_CTRADER_ACCOUNT_ID")
    )
    active_env = settings.get("active_account_env")

    if active_account_id:
        for account in settings.get("accounts", []):
            if str(account.get("account_id")) == str(active_account_id):
                active_env = get_ctrader_env_for_account_item(account)
                break

    return normalize_ctrader_env(
        active_env
        or os.getenv("ACTIVE_CTRADER_ACCOUNT_ENV")
        or os.getenv("CTRADER_ENV", "demo")
    )

def clear_ctrader_connection_cache():
    CTRADER_CONNECTION_CACHE["checked_at"] = 0
    CTRADER_CONNECTION_CACHE["state"] = None
    CTRADER_CONNECTION_CACHE["last_success_at"] = 0
    CTRADER_CONNECTION_CACHE["consecutive_failures"] = 0
    CTRADER_CANDLE_CACHE.clear()

def get_ctrader_redirect_uri():
    return get_ctrader_redirect_uri_debug()["final_value"]

def get_ctrader_redirect_uri_debug():
    file_value = read_env_file_values().get("CTRADER_REDIRECT_URI")
    env_value = os.getenv("CTRADER_REDIRECT_URI")
    callback_value = os.getenv("CTRADER_CALLBACK_URL")
    oauth_value = os.getenv("CTRADER_OAUTH_REDIRECT_URI")
    fallback_value = callback_value or oauth_value or ""
    final_value = file_value or env_value or fallback_value or ""
    source = (
        "env_file"
        if file_value
        else "env"
        if env_value
        else "fallback"
        if fallback_value
        else "missing"
    )

    return {
        "env_value": env_value,
        "file_value": file_value,
        "fallback_value": fallback_value,
        "final_value": final_value,
        "source": source,
    }

def build_ctrader_authorization_url():
    client_id = os.getenv("CTRADER_CLIENT_ID")
    redirect_debug = get_ctrader_redirect_uri_debug()
    redirect_uri = redirect_debug["final_value"]

    if not client_id:
        return {
            "ok": False,
            "reason": "Missing CTRADER_CLIENT_ID",
        }

    if not redirect_uri:
        return {
            "ok": False,
            "reason": (
                "Missing CTRADER_REDIRECT_URI. For local use "
                "http://127.0.0.1:8001/ctrader/callback. For production use "
                "https://api.nathauxfx.com/ctrader/callback. It must exactly match the URI registered in cTrader Open API."
            ),
        }

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": "trading",
    }
    authorization_url = f"{os.getenv('CTRADER_AUTH_URL', CTRADER_AUTH_URL)}?{urlencode(params)}"

    print("CTRADER_REDIRECT_URI_USED =", redirect_debug)
    print("CTRADER_OAUTH_REDIRECT_DEBUG =", {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "auth_url": authorization_url,
    })

    return {
        "ok": True,
        "authorization_url": authorization_url,
        "redirect_uri": redirect_uri,
        "redirect_uri_debug": redirect_debug,
    }

def exchange_ctrader_authorization_code(code):
    redirect_uri = get_ctrader_redirect_uri()
    client_id = os.getenv("CTRADER_CLIENT_ID")
    client_secret = os.getenv("CTRADER_CLIENT_SECRET")

    missing = [
        key for key, value in {
            "CTRADER_CLIENT_ID": client_id,
            "CTRADER_CLIENT_SECRET": client_secret,
            "CTRADER_REDIRECT_URI": redirect_uri,
        }.items()
        if not value
    ]

    if missing:
        return {
            "ok": False,
            "reason": f"Missing cTrader OAuth setting: {', '.join(missing)}",
        }

    try:
        response = requests.post(
            CTRADER_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "client_secret": client_secret,
            },
            timeout=12,
        )
    except Exception as exc:
        return {
            "ok": False,
            "reason": f"cTrader token request failed: {exc}",
        }

    try:
        payload = response.json()
    except Exception:
        payload = {}

    if not response.ok:
        return {
            "ok": False,
            "reason": f"cTrader token error HTTP {response.status_code}: {payload.get('error') or payload.get('message') or response.text[:120]}",
        }

    access_token = payload.get("accessToken") or payload.get("access_token")
    refresh_token = payload.get("refreshToken") or payload.get("refresh_token")

    if not access_token:
        return {
            "ok": False,
            "reason": "cTrader token response missing access token",
        }

    os.environ["CTRADER_ACCESS_TOKEN"] = access_token

    if refresh_token:
        os.environ["CTRADER_REFRESH_TOKEN"] = refresh_token

    update_env_file_values({
        "CTRADER_ACCESS_TOKEN": access_token,
        "CTRADER_REFRESH_TOKEN": refresh_token or os.getenv("CTRADER_REFRESH_TOKEN", ""),
    })
    durable_saved = persist_ctrader_tokens(
        access_token,
        refresh_token or os.getenv("CTRADER_REFRESH_TOKEN", ""),
        updated_by="oauth_callback",
    )
    clear_ctrader_connection_cache()

    return {
        "ok": True,
        "access_token_saved": True,
        "refresh_token_saved": bool(refresh_token),
        "durable_token_saved": durable_saved,
    }

def clear_ctrader_saved_accounts():
    settings = load_ctrader_account_settings()
    settings["active_account_id"] = None
    settings["active_account_env"] = None
    settings["accounts"] = []
    settings["forgotten_account_ids"] = []
    settings["last_refresh"] = None
    save_ctrader_account_settings(settings)

    os.environ.pop("ACTIVE_CTRADER_ACCOUNT_ID", None)
    os.environ.pop("ACTIVE_CTRADER_ACCOUNT_ENV", None)
    os.environ.pop("CTRADER_ACCOUNT_ID", None)
    update_env_file_values({
        "ACTIVE_CTRADER_ACCOUNT_ID": "",
        "ACTIVE_CTRADER_ACCOUNT_ENV": "",
        "CTRADER_ACCOUNT_ID": "",
    })
    CONNECTED["account_id"] = None
    CONNECTED["execution_ready"] = False
    clear_ctrader_connection_cache()

    return {
        "ok": True,
        "active_account_id": None,
        "accounts": [],
    }

def set_active_ctrader_account(account_id):
    account_id = str(account_id or "").strip()

    if not account_id:
        return {
            "ok": False,
            "reason": "Missing cTrader account id",
        }

    settings = load_ctrader_account_settings()
    account_lookup = {
        str(item.get("account_id")): item
        for item in settings.get("accounts", [])
        if item.get("account_id")
    }
    account_ids = set(account_lookup)

    if account_ids and account_id not in account_ids:
        update_ctrader_account_auth_selection_debug(
            account_id,
            sorted(account_ids),
            reason=CTRADER_UNAUTHORIZED_ACCOUNT_MESSAGE,
        )
        return {
            "ok": False,
            "reason": CTRADER_UNAUTHORIZED_ACCOUNT_MESSAGE,
            "account_id": account_id,
            "active_account_id": account_id,
            "authorized_account_ids": sorted(account_ids),
            "selected_account_source": "request",
            "is_active_account_authorized": False,
        }

    account = account_lookup.get(account_id) or {}
    account_env = get_ctrader_env_for_account_item(account)
    auth_result = verify_ctrader_account_auth(
        account_id,
        config={"env": account_env},
    )

    if not auth_result.get("ok"):
        print("ACTIVE_ACCOUNT_SELECTED_DEBUG =", {
            "ok": False,
            "account_id": account_id,
            "env": account_env,
            "reason": auth_result.get("reason"),
        })
        return auth_result

    settings["active_account_id"] = account_id
    settings["active_account_env"] = account_env
    clear_active_ctrader_account_balance_cache(settings, persist=False)
    save_ctrader_account_settings(settings)
    os.environ["ACTIVE_CTRADER_ACCOUNT_ID"] = account_id
    os.environ["ACTIVE_CTRADER_ACCOUNT_ENV"] = account_env
    os.environ["CTRADER_ACCOUNT_ID"] = account_id
    update_env_file_values({
        "ACTIVE_CTRADER_ACCOUNT_ID": account_id,
        "ACTIVE_CTRADER_ACCOUNT_ENV": account_env,
        "CTRADER_ACCOUNT_ID": account_id,
    })
    CONNECTED["account_id"] = account_id
    CONNECTED["mode"] = account_env
    CONNECTED["execution_ready"] = True
    clear_ctrader_connection_cache()
    try:
        fresh_snapshot = get_ctrader_account_snapshot()
    except Exception as exc:
        fresh_snapshot = {
            "ok": False,
            "broker": "ctrader",
            "mode": account_env,
            "account_id": account_id,
            "reason": describe_ctrader_error(exc),
            "balance": None,
            "equity": None,
            "balance_verified": False,
            "equity_verified": False,
            "cached_balance_used": False,
        }
        print("ACCOUNT_BALANCE_VERIFICATION_FAILED =", fresh_snapshot)

    print("ACTIVE_ACCOUNT_SELECTED_DEBUG =", {
        "ok": True,
        "account_id": account_id,
        "env": account_env,
        "fresh_balance_verified": fresh_snapshot.get("balance_verified"),
        "fresh_equity_verified": fresh_snapshot.get("equity_verified"),
        "fresh_balance": fresh_snapshot.get("balance"),
        "fresh_equity": fresh_snapshot.get("equity"),
    })

    return {
        "ok": True,
        "account_id": account_id,
        "env": account_env,
        "fresh_account_snapshot": fresh_snapshot,
        **get_ctrader_account_selection_debug(),
    }

def forget_ctrader_account(account_id):
    account_id = str(account_id or "").strip()
    settings = load_ctrader_account_settings()
    accounts = [
        item for item in settings.get("accounts", [])
        if str(item.get("account_id")) != account_id
    ]
    forgotten = {
        str(value)
        for value in settings.get("forgotten_account_ids", [])
        if value is not None
    }

    if account_id:
        forgotten.add(account_id)

    if str(settings.get("active_account_id")) == account_id:
        settings["active_account_id"] = None
        settings["active_account_env"] = None

    settings["accounts"] = accounts
    settings["forgotten_account_ids"] = sorted(forgotten)
    save_ctrader_account_settings(settings)
    clear_ctrader_connection_cache()

    return {
        "ok": True,
        "account_id": account_id,
        "active_account_id": settings.get("active_account_id"),
        "accounts": accounts,
    }

def clear_ctrader_tokens_and_accounts():
    settings = load_ctrader_account_settings()
    settings["active_account_id"] = None
    settings["active_account_env"] = None
    settings["accounts"] = []
    settings["last_refresh"] = None
    save_ctrader_account_settings(settings)

    for key in [
        "CTRADER_ACCESS_TOKEN",
        "CTRADER_REFRESH_TOKEN",
        "ACTIVE_CTRADER_ACCOUNT_ID",
        "ACTIVE_CTRADER_ACCOUNT_ENV",
        "CTRADER_ACCOUNT_ID",
    ]:
        os.environ.pop(key, None)

    update_env_file_values({
        "CTRADER_ACCESS_TOKEN": "",
        "CTRADER_REFRESH_TOKEN": "",
        "ACTIVE_CTRADER_ACCOUNT_ID": "",
        "ACTIVE_CTRADER_ACCOUNT_ENV": "",
        "CTRADER_ACCOUNT_ID": "",
    })
    clear_durable_ctrader_tokens()
    CTRADER_TOKEN_HYDRATION.update({
        "checked_at": time.time(),
        "loaded": False,
        "source": "explicit_disconnect",
    })
    clear_ctrader_connection_cache()

def normalize_live_price(value):
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None

    if numeric <= 0:
        return None

    return numeric / 100000

def update_live_tick(symbol, bid, ask, server_timestamp=None):
    normalized_symbol = normalize_symbol(symbol)

    if normalized_symbol not in LIVE_TICKS:
        return

    bid_value = normalize_live_price(bid)
    ask_value = normalize_live_price(ask)

    if bid_value is None and ask_value is None:
        return

    current = LIVE_TICKS.get(normalized_symbol, {})

    if bid_value is None:
        bid_value = current.get("bid")

    if ask_value is None:
        ask_value = current.get("ask")

    if bid_value is None and ask_value is None:
        return

    if bid_value is not None and ask_value is not None:
        mid_value = (bid_value + ask_value) / 2
    else:
        mid_value = bid_value if bid_value is not None else ask_value

    now = time.time()

    with LIVE_TICKS_LOCK:
        LIVE_TICKS[normalized_symbol] = {
            "bid": bid_value,
            "ask": ask_value,
            "mid": mid_value,
            "timestamp": now,
            "server_timestamp": server_timestamp,
        }

    print(
        f"CTRADER LIVE TICK: {normalized_symbol} "
        f"bid={bid_value} ask={ask_value}"
    )

def get_ctrader_live_price_status():
    now = time.time()

    with LIVE_TICKS_LOCK:
        prices = {
            symbol: dict(values)
            for symbol, values in LIVE_TICKS.items()
        }

    timestamps = [
        values.get("timestamp")
        for values in prices.values()
        if values.get("timestamp")
    ]
    stale_symbols = [
        symbol
        for symbol, values in prices.items()
        if not values.get("timestamp")
        or now - float(values.get("timestamp")) > LIVE_PRICE_STALE_SECONDS
    ]
    last_update = max(timestamps) if timestamps else None

    return {
        "live_prices": prices,
        "live_price_health": "STALE" if stale_symbols else "OK",
        "live_price_stale_symbols": stale_symbols,
        "live_price_last_update": last_update,
        "live_price_last_error": LIVE_PRICE_LAST_ERROR,
    }

def get_live_prices():
    return get_ctrader_live_price_status()

def get_timeframe_minutes(timeframe):
    normalized = str(timeframe or "").lower()
    period = CTRADER_TRENDBAR_PERIODS.get(normalized)

    if period is None:
        return None

    return CTRADER_TRENDBAR_PERIOD_MINUTES.get(period)

def floor_datetime_to_minutes(value, minutes):
    if not value or not minutes:
        return None

    timestamp = pd.Timestamp(value)

    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    else:
        timestamp = timestamp.tz_convert("UTC")

    bucket_minute = (timestamp.minute // minutes) * minutes

    return timestamp.replace(
        minute=bucket_minute,
        second=0,
        microsecond=0,
        nanosecond=0,
    )

def get_live_tick_snapshot(symbol):
    normalized_symbol = normalize_symbol(symbol)

    with LIVE_TICKS_LOCK:
        tick = dict(LIVE_TICKS.get(normalized_symbol) or {})

    now = time.time()
    timestamp = tick.get("timestamp")
    tick_age = None

    if timestamp:
        try:
            tick_age = now - float(timestamp)
        except (TypeError, ValueError):
            tick_age = None

    is_stale = tick_age is None or tick_age > LIVE_TICK_CANDLE_STALE_SECONDS

    print("CTRADER_LIVE_TICK_STATUS =", {
        "symbol": normalized_symbol,
        "tick_timestamp": timestamp,
        "now": now,
        "age_seconds": None if tick_age is None else round(tick_age, 3),
        "stale_threshold": LIVE_PRICE_STALE_SECONDS,
        "candle_stale_threshold": LIVE_TICK_CANDLE_STALE_SECONDS,
        "is_stale": is_stale,
        "bid": tick.get("bid"),
        "ask": tick.get("ask"),
        "mid": tick.get("mid"),
        "server_timestamp": tick.get("server_timestamp"),
    })

    if not timestamp:
        return None

    if is_stale:
        return None

    price = tick.get("mid") or tick.get("bid") or tick.get("ask")

    try:
        price = float(price)
    except (TypeError, ValueError):
        return None

    if normalized_symbol == "XAUUSD" and price > 100000:
        price = price / 1000

    return {
        **tick,
        "price": price,
        "age_seconds": tick_age,
    }

def append_current_forming_candle(candles, symbol, timeframe):
    if candles is None or candles.empty:
        return candles

    timeframe_minutes = get_timeframe_minutes(timeframe)

    if not timeframe_minutes:
        return candles

    tick = get_live_tick_snapshot(symbol)
    now_utc = datetime.now(timezone.utc)
    last_closed_time = pd.Timestamp(candles.index[-1])

    if last_closed_time.tzinfo is None:
        last_closed_time = last_closed_time.tz_localize("UTC")
    else:
        last_closed_time = last_closed_time.tz_convert("UTC")

    synthetic_time = floor_datetime_to_minutes(now_utc, timeframe_minutes)
    age_minutes = round((now_utc - last_closed_time.to_pydatetime()).total_seconds() / 60, 2)

    if tick is None:
        print("CTRADER_CURRENT_CANDLE_DEBUG =", {
            "symbol": normalize_symbol(symbol),
            "timeframe": timeframe,
            "last_closed_candle_time": last_closed_time.isoformat(),
            "synthetic_current_candle_time": synthetic_time.isoformat() if synthetic_time is not None else None,
            "now_utc": now_utc.isoformat(),
            "candle_age_minutes": age_minutes,
            "candle_stale_threshold": LIVE_TICK_CANDLE_STALE_SECONDS,
            "appended": False,
            "reason": "live tick unavailable or stale",
        })
        return candles

    result = candles.copy()
    price = tick["price"]
    frame_gap_minutes = (
        synthetic_time - last_closed_time
    ).total_seconds() / 60 if synthetic_time is not None else 0

    # A live tick cannot repair hours of missing OHLC history. Bridging a
    # stale cached close to the current price creates a giant fake candle and
    # can contaminate structure calculations.
    if frame_gap_minutes > timeframe_minutes * 2:
        print("CTRADER_CURRENT_CANDLE_DEBUG =", {
            "symbol": normalize_symbol(symbol),
            "timeframe": timeframe,
            "last_closed_candle_time": last_closed_time.isoformat(),
            "synthetic_current_candle_time": (
                synthetic_time.isoformat()
                if synthetic_time is not None
                else None
            ),
            "now_utc": now_utc.isoformat(),
            "candle_age_minutes": age_minutes,
            "live_tick_price": price,
            "appended": False,
            "reason": "cached candle gap too large for synthetic OHLC",
        })
        return candles

    if synthetic_time is not None and last_closed_time < synthetic_time:
        previous_close = float(result["Close"].iloc[-1])
        result.loc[synthetic_time, ["Open", "High", "Low", "Close", "Volume"]] = [
            previous_close,
            max(previous_close, price),
            min(previous_close, price),
            price,
            0,
        ]
        appended = True
    elif synthetic_time is not None and last_closed_time == synthetic_time:
        current_high = float(result.loc[synthetic_time, "High"])
        current_low = float(result.loc[synthetic_time, "Low"])
        result.loc[synthetic_time, "High"] = max(current_high, price)
        result.loc[synthetic_time, "Low"] = min(current_low, price)
        result.loc[synthetic_time, "Close"] = price
        appended = False
    else:
        appended = False

    print("CTRADER_CURRENT_CANDLE_DEBUG =", {
        "symbol": normalize_symbol(symbol),
        "timeframe": timeframe,
        "last_closed_candle_time": last_closed_time.isoformat(),
        "synthetic_current_candle_time": synthetic_time.isoformat() if synthetic_time is not None else None,
        "now_utc": now_utc.isoformat(),
        "candle_age_minutes": age_minutes,
        "candle_stale_threshold": LIVE_TICK_CANDLE_STALE_SECONDS,
        "live_tick_price": price,
        "appended": appended,
    })

    return result.sort_index()


def build_trade_payload(
    ok,
    mode,
    symbol,
    action,
    entry,
    sl,
    tp1,
    tp2,
    volume=None,
    reason=None
):
    return {
        "ok": ok,
        "broker": "ctrader",
        "mode": mode,
        "symbol": symbol,
        "action": action,
        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "volume": volume,
        "reason": reason,
    }


def normalize_symbol(symbol):
    value = str(symbol or "").strip().upper()

    if value in ["GOLD", "XAUUSD"]:
        return "XAUUSD"

    return value

def normalize_ctrader_symbol_name(symbol):
    return "".join(
        char for char in normalize_symbol(symbol)
        if char.isalnum()
    )

def get_default_symbol_digits(symbol):
    normalized = normalize_symbol(symbol)

    if normalized == "EURUSD":
        return 5
    if normalized == "XAUUSD":
        return 2

    return None

def get_ctrader_candle_cache_key(symbol, timeframe):
    return f"{normalize_symbol(symbol)}:{str(timeframe or '').lower()}"

def get_ctrader_candle_cache_path(symbol, timeframe):
    account_id = get_active_ctrader_account_id() or "no-account"
    safe_timeframe = re.sub(r"[^a-zA-Z0-9_-]", "_", str(timeframe or "").lower())
    return CTRADER_CANDLE_CACHE_DIR / (
        f"{account_id}_{normalize_symbol(symbol)}_{safe_timeframe}.json"
    )

def persist_ctrader_candle_cache(symbol, timeframe, data):
    if data is None or data.empty:
        return False

    try:
        CTRADER_CANDLE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_path = get_ctrader_candle_cache_path(symbol, timeframe)
        temp_path = cache_path.with_suffix(".tmp")
        records = data.reset_index()
        records.to_json(
            temp_path,
            orient="records",
            date_format="iso",
        )
        temp_path.replace(cache_path)
        return True
    except Exception as exc:
        print("CANDLE_CACHE_PERSIST_ERROR =", {
            "symbol": normalize_symbol(symbol),
            "timeframe": timeframe,
            "error": str(exc),
        })
        return False

def load_persisted_ctrader_candle_cache(symbol, timeframe):
    cache_path = get_ctrader_candle_cache_path(symbol, timeframe)

    if not cache_path.exists():
        return None

    try:
        data = pd.read_json(cache_path, orient="records")
        datetime_column = "Datetime" if "Datetime" in data.columns else "index"
        if datetime_column not in data.columns:
            return None

        data[datetime_column] = pd.to_datetime(
            data[datetime_column],
            errors="coerce",
            utc=True,
        )
        data = data.dropna(subset=[datetime_column])
        data = data.set_index(datetime_column).sort_index()
        data.index.name = "Datetime"
        data = data[["Open", "High", "Low", "Close", "Volume"]]
        data = data.dropna(subset=["Open", "High", "Low", "Close"])
        if data.empty:
            return None

        restored_at = datetime.fromtimestamp(
            cache_path.stat().st_mtime,
            tz=timezone.utc,
        )
        print("CANDLE_PROVIDER_DEBUG =", {
            "event": "disk_cache_restored",
            "symbol": normalize_symbol(symbol),
            "timeframe": timeframe,
            "rows": len(data),
            "cache_path": str(cache_path),
        })
        return {
            "data": data,
            "fetched_at": restored_at,
            "last_attempt": restored_at,
            "last_successful_fetch": restored_at,
            "last_error": None,
            "missed_fetch_count": 0,
            "source": "ctrader_cache",
            "symbol": normalize_symbol(symbol),
            "timeframe": str(timeframe or "").lower(),
            "persisted": True,
        }
    except Exception as exc:
        print("CANDLE_CACHE_LOAD_ERROR =", {
            "symbol": normalize_symbol(symbol),
            "timeframe": timeframe,
            "error": str(exc),
        })
        return None

def hydrate_persisted_ctrader_candle_cache(symbol, timeframe):
    cached = load_persisted_ctrader_candle_cache(symbol, timeframe)
    if cached:
        CTRADER_CANDLE_CACHE[
            get_ctrader_candle_cache_key(symbol, timeframe)
        ] = cached
    return cached

def get_ctrader_candle_health(symbol, timeframe):
    cache_key = get_ctrader_candle_cache_key(symbol, timeframe)
    cached = CTRADER_CANDLE_CACHE.get(cache_key) or {}
    data = cached.get("data")
    last_candle_time = None

    if data is not None and not data.empty:
        try:
            last_candle_time = pd.Timestamp(data.index[-1]).isoformat()
        except Exception:
            last_candle_time = None

    missed_fetch_count = int(cached.get("missed_fetch_count") or 0)
    has_candles = bool(data is not None and not data.empty)
    normalized_timeframe = str(timeframe or "").lower()
    last_candle_age_seconds = None

    if last_candle_time:
        try:
            last_timestamp = pd.Timestamp(last_candle_time)
            if last_timestamp.tzinfo is None:
                last_timestamp = last_timestamp.tz_localize("UTC")
            else:
                last_timestamp = last_timestamp.tz_convert("UTC")
            last_candle_age_seconds = max(
                0,
                (
                    datetime.now(timezone.utc)
                    - last_timestamp.to_pydatetime()
                ).total_seconds(),
            )
        except Exception:
            last_candle_age_seconds = None

    max_recovery_age = CTRADER_CANDLE_RECOVERY_MAX_AGE_SECONDS.get(
        normalized_timeframe,
        15 * 60,
    )
    usable = bool(
        has_candles
        and last_candle_age_seconds is not None
        and last_candle_age_seconds <= max_recovery_age
    )

    return {
        "cache_key": cache_key,
        "symbol": normalize_symbol(symbol),
        "timeframe": normalized_timeframe,
        "has_candles": has_candles,
        "source": cached.get("source") or ("ctrader_cache" if has_candles else "unavailable"),
        "last_candle_time": last_candle_time,
        "last_successful_fetch": (
            cached.get("last_successful_fetch").isoformat()
            if isinstance(cached.get("last_successful_fetch"), datetime)
            else cached.get("last_successful_fetch")
        ),
        "last_attempt": (
            cached.get("last_attempt").isoformat()
            if isinstance(cached.get("last_attempt"), datetime)
            else cached.get("last_attempt")
        ),
        "last_error": cached.get("last_error"),
        "missed_fetch_count": missed_fetch_count,
        "miss_limit": CTRADER_CANDLE_MISS_LIMIT,
        "last_candle_age_seconds": (
            round(last_candle_age_seconds, 1)
            if last_candle_age_seconds is not None
            else None
        ),
        "max_recovery_age_seconds": max_recovery_age,
        "recovery_mode": bool(has_candles and missed_fetch_count > 0),
        "usable": usable,
    }

def get_ctrader_candle_cache_status():
    entries = {
        key: get_ctrader_candle_health(
            value.get("symbol"),
            value.get("timeframe"),
        )
        for key, value in CTRADER_CANDLE_CACHE.items()
    }
    return {
        "ctrader_cache_keys": sorted(CTRADER_CANDLE_CACHE.keys()),
        "ctrader_last_success": LAST_CTRADER_CANDLE_SUCCESS,
        "ctrader_last_error": LAST_CTRADER_CANDLE_ERROR,
        "candle_entries": entries,
    }

def normalize_ctrader_candles(raw, symbol=None):
    if not raw:
        return pd.DataFrame()

    candles = raw

    if isinstance(raw, dict):
        candles = (
            raw.get("candles")
            or raw.get("trendbar")
            or raw.get("trendbars")
            or raw.get("data")
            or raw.get("values")
            or []
        )

    if not isinstance(candles, list):
        print("CTRADER CANDLES NORMALIZE: unsupported raw candle shape")
        return pd.DataFrame()

    rows = []

    for candle in candles:
        if not isinstance(candle, dict):
            continue

        timestamp = (
            candle.get("timestamp")
            or candle.get("time")
            or candle.get("datetime")
            or candle.get("utcTimestampInMinutes")
        )
        digits = candle.get("digits")
        low_raw = candle.get("low")

        if (
            low_raw is not None
            and (
                candle.get("deltaOpen") is not None
                or candle.get("deltaHigh") is not None
                or candle.get("deltaClose") is not None
            )
        ):
            if digits is None:
                print("CTRADER CANDLES NORMALIZE: missing symbol digits")
                return pd.DataFrame()

            divisor = 10 ** int(digits)
            low_price = int(low_raw) / divisor
            open_price = (int(low_raw) + int(candle.get("deltaOpen") or 0)) / divisor
            high_price = (int(low_raw) + int(candle.get("deltaHigh") or 0)) / divisor
            close_price = (int(low_raw) + int(candle.get("deltaClose") or 0)) / divisor
        else:
            open_price = candle.get("open") or candle.get("Open")
            high_price = candle.get("high") or candle.get("High")
            low_price = candle.get("low") or candle.get("Low")
            close_price = candle.get("close") or candle.get("Close")

        volume = candle.get("volume") or candle.get("Volume") or 0

        if open_price is None or high_price is None or low_price is None or close_price is None:
            continue

        rows.append({
            "Datetime": timestamp,
            "Open": open_price,
            "High": high_price,
            "Low": low_price,
            "Close": close_price,
            "Volume": volume,
        })

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)

    for col in ["Open", "High", "Low", "Close", "Volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    normalized_symbol = normalize_symbol(symbol) if symbol else None

    if normalized_symbol == "XAUUSD" and not df.empty:
        ohlc_cols = ["Open", "High", "Low", "Close"]
        max_ohlc = df[ohlc_cols].max().max()

        if pd.notna(max_ohlc) and float(max_ohlc) > 100000:
            before_close = float(df["Close"].iloc[-1])
            factor = 1000
            df[ohlc_cols] = df[ohlc_cols] / factor
            after_close = float(df["Close"].iloc[-1])
            print("XAUUSD_PRICE_SCALE_FIX =", {
                "before_close": before_close,
                "after_close": after_close,
                "factor": factor,
            })

    if "Datetime" in df.columns:
        numeric_datetime = pd.to_numeric(df["Datetime"], errors="coerce")

        if numeric_datetime.notna().all():
            max_timestamp = numeric_datetime.max()
            unit = "m" if max_timestamp < 10000000000 else "ms"
            df["Datetime"] = pd.to_datetime(numeric_datetime, unit=unit, errors="coerce", utc=True)
        else:
            df["Datetime"] = pd.to_datetime(df["Datetime"], errors="coerce", utc=True)

        df = df.dropna(subset=["Datetime"])
        df = df.sort_values("Datetime").set_index("Datetime")

    df = df.dropna(subset=["Open", "High", "Low", "Close"])

    return df[["Open", "High", "Low", "Close", "Volume"]]

def get_ctrader_market_data(symbol, timeframe, limit=500, force_refresh=False):
    global LAST_CTRADER_CANDLE_ERROR, LAST_CTRADER_CANDLE_SUCCESS

    execution_symbol = normalize_symbol(symbol)
    normalized_timeframe = str(timeframe or "").lower()
    period = CTRADER_TRENDBAR_PERIODS.get(normalized_timeframe)
    ttl_seconds = CTRADER_CANDLE_TTLS.get(normalized_timeframe, 300)
    cache_key = get_ctrader_candle_cache_key(execution_symbol, normalized_timeframe)
    now = datetime.now(timezone.utc)
    LAST_CTRADER_CANDLE_ERROR = None

    if not period:
        LAST_CTRADER_CANDLE_ERROR = f"Unsupported cTrader candle timeframe: {timeframe}"
        print("CTRADER_CANDLES_ERROR_DEBUG =", {
            "symbol": execution_symbol,
            "timeframe": timeframe,
            "account_id": get_active_ctrader_account_id(),
            "error": LAST_CTRADER_CANDLE_ERROR,
        })
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

    cached = CTRADER_CANDLE_CACHE.get(cache_key)
    if not cached:
        cached = load_persisted_ctrader_candle_cache(
            execution_symbol,
            normalized_timeframe,
        )
        if cached:
            CTRADER_CANDLE_CACHE[cache_key] = cached

    if cached and not force_refresh:
        age = (now - cached["fetched_at"]).total_seconds()

        if age < ttl_seconds and not cached["data"].empty:
            cached["source"] = "ctrader_cache"
            print(f"CTRADER CANDLE CACHE HIT: {execution_symbol} {timeframe}")
            result = append_current_forming_candle(
                cached["data"].copy(),
                execution_symbol,
                normalized_timeframe,
            )
            cached["data"] = result.copy()
            print("CANDLE_PROVIDER_DEBUG =", {
                **get_ctrader_candle_health(execution_symbol, normalized_timeframe),
                "event": "cache_hit",
                "rows": len(result),
            })
            return result

    try:
        print(f"CTRADER CANDLE CACHE REFRESH: {execution_symbol} {timeframe}")
        if cached:
            cached["last_attempt"] = now
        config = get_ctrader_config()

        if not config:
            LAST_CTRADER_CANDLE_ERROR = "Missing or invalid cTrader config for candle fetch"
            print("CTRADER_CANDLES_ERROR_DEBUG =", {
                "symbol": execution_symbol,
                "timeframe": timeframe,
                "account_id": get_active_ctrader_account_id(),
                "error": LAST_CTRADER_CANDLE_ERROR,
            })
            if cached and not cached["data"].empty:
                cached["missed_fetch_count"] = int(cached.get("missed_fetch_count") or 0) + 1
                cached["last_error"] = LAST_CTRADER_CANDLE_ERROR
                cached["source"] = "ctrader_cache"
                result = append_current_forming_candle(
                    cached["data"].copy(),
                    execution_symbol,
                    normalized_timeframe,
                )
                cached["data"] = result.copy()
                print("CANDLE_PROVIDER_DEBUG =", {
                    **get_ctrader_candle_health(execution_symbol, normalized_timeframe),
                    "event": "fetch_failed_cache_used",
                    "rows": len(result),
                })
                return result
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

        candles = fetch_ctrader_trendbars(
            config,
            execution_symbol,
            period,
            limit
        )

        if candles.empty:
            LAST_CTRADER_CANDLE_ERROR = f"No cTrader candles returned for {execution_symbol} {timeframe}"
            print("CTRADER_CANDLES_ERROR_DEBUG =", {
                "symbol": execution_symbol,
                "timeframe": timeframe,
                "account_id": config.get("account_id"),
                "error": LAST_CTRADER_CANDLE_ERROR,
            })
            if cached and not cached["data"].empty:
                cached["missed_fetch_count"] = int(cached.get("missed_fetch_count") or 0) + 1
                cached["last_error"] = LAST_CTRADER_CANDLE_ERROR
                cached["source"] = "ctrader_cache"
                result = append_current_forming_candle(
                    cached["data"].copy(),
                    execution_symbol,
                    normalized_timeframe,
                )
                cached["data"] = result.copy()
                print("CANDLE_PROVIDER_DEBUG =", {
                    **get_ctrader_candle_health(execution_symbol, normalized_timeframe),
                    "event": "empty_fetch_cache_used",
                    "rows": len(result),
                })
                return result
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

        CTRADER_CANDLE_CACHE[cache_key] = {
            "data": candles.copy(),
            "fetched_at": now,
            "last_attempt": now,
            "last_successful_fetch": now,
            "last_error": None,
            "missed_fetch_count": 0,
            "source": "ctrader",
            "symbol": execution_symbol,
            "timeframe": normalized_timeframe,
        }
        persist_ctrader_candle_cache(
            execution_symbol,
            normalized_timeframe,
            candles,
        )
        LAST_CTRADER_CANDLE_SUCCESS = now.isoformat()

        print(
            "CTRADER CANDLES OK:",
            execution_symbol,
            timeframe,
            len(candles)
        )

        result = append_current_forming_candle(
            candles,
            execution_symbol,
            normalized_timeframe,
        )
        print("CANDLE_PROVIDER_DEBUG =", {
            **get_ctrader_candle_health(execution_symbol, normalized_timeframe),
            "event": "fetch_success",
            "rows": len(result),
        })
        return result

    except Exception as e:
        LAST_CTRADER_CANDLE_ERROR = describe_ctrader_error(e)
        print("CTRADER_CANDLES_ERROR_DEBUG =", {
            "symbol": execution_symbol,
            "timeframe": timeframe,
            "account_id": config.get("account_id") if "config" in locals() and config else get_active_ctrader_account_id(),
            "error": LAST_CTRADER_CANDLE_ERROR,
        })
        if cached and not cached["data"].empty:
            cached["missed_fetch_count"] = int(cached.get("missed_fetch_count") or 0) + 1
            cached["last_attempt"] = now
            cached["last_error"] = LAST_CTRADER_CANDLE_ERROR
            cached["source"] = "ctrader_cache"
            result = append_current_forming_candle(
                cached["data"].copy(),
                execution_symbol,
                normalized_timeframe,
            )
            cached["data"] = result.copy()
            print("CANDLE_PROVIDER_DEBUG =", {
                **get_ctrader_candle_health(execution_symbol, normalized_timeframe),
                "event": "exception_cache_used",
                "rows": len(result),
            })
            return result

    return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

def start_ctrader_live_price_stream():
    global LIVE_PRICE_THREAD, LIVE_PRICE_THREAD_STARTED

    if LIVE_PRICE_THREAD_STARTED and LIVE_PRICE_THREAD and LIVE_PRICE_THREAD.is_alive():
        return {
            "ok": True,
            "status": "already_running",
        }

    config = get_ctrader_config()

    if not config:
        LIVE_PRICE_LAST_ERROR = "Missing or invalid cTrader config"
        return {
            "ok": False,
            "status": "not_started",
            "reason": LIVE_PRICE_LAST_ERROR,
            **get_ctrader_account_selection_debug(),
        }

    auth_check = verify_ctrader_account_auth(
        config.get("account_id"),
        config=config,
    )

    if not auth_check.get("ok"):
        LIVE_PRICE_LAST_ERROR = auth_check.get("reason")
        return {
            "ok": False,
            "status": "not_started",
            "reason": LIVE_PRICE_LAST_ERROR,
            **get_ctrader_account_selection_debug(),
        }

    LIVE_PRICE_THREAD_STARTED = True
    LIVE_PRICE_THREAD = threading.Thread(
        target=ctrader_live_price_stream_loop,
        daemon=True,
    )
    LIVE_PRICE_THREAD.start()

    return {
        "ok": True,
        "status": "started",
        **get_ctrader_account_selection_debug(),
    }

def ctrader_live_price_stream_loop():
    global LIVE_PRICE_LAST_ERROR

    while True:
        sock = None

        try:
            config = get_ctrader_config()

            if not config:
                LIVE_PRICE_LAST_ERROR = "Missing or invalid cTrader config"
                time.sleep(10)
                continue

            account_id = int(config["account_id"])
            host, port = CTRADER_JSON_ENDPOINTS[config["env"]]
            sock = open_ctrader_json_socket(host, port)
            authorize_ctrader_socket(sock, config, account_id)
            symbol_details = fetch_ctrader_symbol_details(sock, account_id)
            subscribed = []
            symbol_by_id = {}

            for symbol in ["EURUSD", "XAUUSD"]:
                symbol_info = resolve_ctrader_symbol(symbol_details, symbol)

                if not symbol_info:
                    print("CTRADER LIVE PRICE SYMBOL MISSING:", symbol)
                    continue

                subscribed.append(int(symbol_info["symbol_id"]))
                symbol_by_id[str(symbol_info["symbol_id"])] = symbol

            if not subscribed:
                raise RuntimeError("No cTrader symbols available for live price stream")

            send_ctrader_request(
                sock,
                PAYLOAD_SUBSCRIBE_SPOTS_REQ,
                {
                    "ctidTraderAccountId": account_id,
                    "symbolId": subscribed,
                    "subscribeToSpotTimestamp": True,
                },
                PAYLOAD_SUBSCRIBE_SPOTS_RES,
            )

            for symbol in symbol_by_id.values():
                print("CTRADER LIVE PRICE SUBSCRIBED:", symbol)

            LIVE_PRICE_LAST_ERROR = None
            sock.settimeout(CTRADER_STREAM_READ_TIMEOUT_SECONDS)
            last_heartbeat_sent = time.monotonic()

            while True:
                if (
                    time.monotonic() - last_heartbeat_sent
                    >= CTRADER_HEARTBEAT_INTERVAL_SECONDS
                ):
                    send_ctrader_heartbeat(sock)
                    last_heartbeat_sent = time.monotonic()

                try:
                    incoming = websocket_recv_text(sock)
                except (socket.timeout, TimeoutError):
                    continue

                if not incoming:
                    continue

                data = json.loads(incoming)
                incoming_type = data.get("payloadType")

                if incoming_type == PAYLOAD_ERROR_RES:
                    raise RuntimeError(f"cTrader live price error response: {data}")

                if incoming_type != PAYLOAD_SPOT_EVENT:
                    continue

                payload = data.get("payload", {})
                symbol_id = payload.get("symbolId")
                symbol = symbol_by_id.get(str(symbol_id))

                if not symbol:
                    continue

                update_live_tick(
                    symbol,
                    payload.get("bid"),
                    payload.get("ask"),
                    payload.get("timestamp"),
                )

        except Exception as e:
            LIVE_PRICE_LAST_ERROR = str(e)
            print("CTRADER LIVE PRICE STREAM ERROR:", e)
            time.sleep(5)
        finally:
            try:
                if sock:
                    sock.close()
            except Exception:
                pass


def normalize_trade_levels(symbol, action, entry, sl, tp1, tp2):
    normalized_symbol = normalize_symbol(symbol)
    normalized_action = str(action or "").strip().upper()
    rules = TRADE_LEVEL_RULES.get(normalized_symbol)

    def reject(reason):
        payload = build_trade_payload(
            False,
            CONNECTED.get("mode", "demo"),
            normalized_symbol,
            normalized_action,
            entry,
            sl,
            tp1,
            tp2,
            reason=reason
        )
        print(
            "CTRADER TRADE REJECTED:",
            normalized_symbol,
            normalized_action,
            entry,
            sl,
            tp1,
            tp2,
            reason
        )
        return payload

    if not rules:
        return reject("Unsupported cTrader symbol")

    if normalized_action not in ["BUY", "SELL"]:
        return reject("Action must be BUY or SELL")

    try:
        entry_value = float(entry)
        sl_value = float(sl)
        tp1_value = float(tp1)
        tp2_value = float(tp2)
    except (TypeError, ValueError):
        return reject("Entry, SL, TP1, and TP2 must be valid numbers")

    if not all(math.isfinite(value) for value in [
        entry_value,
        sl_value,
        tp1_value,
        tp2_value,
    ]):
        return reject("Entry, SL, TP1, and TP2 must be finite real numbers")

    precision = rules["precision"]
    min_distance = rules["min_distance"]
    pip_size = rules.get("pip_size") or min_distance
    buffer_distance = pip_size

    entry_value = round(entry_value, precision)
    sl_value = round(sl_value, precision)
    tp1_value = round(tp1_value, precision)
    tp2_value = round(tp2_value, precision)
    original_sl_value = sl_value
    original_tp1_value = tp1_value
    original_tp2_value = tp2_value

    if normalized_action == "BUY":
        if sl_value >= entry_value:
            return reject("BUY SL must be below entry")
        if tp1_value <= entry_value or tp2_value <= entry_value:
            return reject("BUY TP1 and TP2 must be above entry")

        distances = [
            entry_value - sl_value,
            tp1_value - entry_value,
            tp2_value - entry_value,
        ]
    else:
        if sl_value <= entry_value:
            return reject("SELL SL must be above entry")
        if tp1_value >= entry_value or tp2_value >= entry_value:
            return reject("SELL TP1 and TP2 must be below entry")

        distances = [
            sl_value - entry_value,
            entry_value - tp1_value,
            entry_value - tp2_value,
        ]

    original_sl_distance = distances[0]
    original_tp1_distance = distances[1]
    original_tp2_distance = distances[2]
    adjusted = False
    minimum_required_distance = min_distance + buffer_distance

    if original_sl_distance < min_distance:
        return reject("Strategy SL is below the broker minimum distance")

    adjusted_sl_distance = abs(entry_value - sl_value)
    tp1_ratio = get_tp1_ratio_of_tp2()
    minimum_tp2_distance = minimum_required_distance / tp1_ratio
    adjusted_tp2_distance = max(
        original_tp2_distance,
        minimum_tp2_distance,
    )

    if adjusted_tp2_distance != original_tp2_distance:
        adjusted = True

    if normalized_action == "BUY":
        tp2_value = round(entry_value + adjusted_tp2_distance, precision)
        tp1_value = round(
            entry_value + ((tp2_value - entry_value) * tp1_ratio),
            precision,
        )
    else:
        tp2_value = round(entry_value - adjusted_tp2_distance, precision)
        tp1_value = round(
            entry_value - ((entry_value - tp2_value) * tp1_ratio),
            precision,
        )

    adjusted_tp1_distance = abs(tp1_value - entry_value)

    if tp1_value != original_tp1_value or tp2_value != original_tp2_value:
        adjusted = True

    if normalized_action == "BUY":
        final_distances = [
            entry_value - sl_value,
            tp1_value - entry_value,
            tp2_value - entry_value,
        ]
        invalid_after_adjustment = (
            sl_value >= entry_value
            or tp1_value <= entry_value
            or tp2_value <= entry_value
        )
    else:
        final_distances = [
            sl_value - entry_value,
            entry_value - tp1_value,
            entry_value - tp2_value,
        ]
        invalid_after_adjustment = (
            sl_value <= entry_value
            or tp1_value >= entry_value
            or tp2_value >= entry_value
        )

    distance_details = {
        "symbol": normalized_symbol,
        "side": normalized_action,
        "entry_price": entry_value,
        "entry": entry_value,
        "original_sl_price": original_sl_value,
        "original_tp_price": original_tp1_value,
        "original_tp2_price": original_tp2_value,
        "sl_price": sl_value,
        "sl": sl_value,
        "tp_price": tp1_value,
        "tp1": tp1_value,
        "tp2_price": tp2_value,
        "tp2": tp2_value,
        "pip_size": pip_size,
        "broker_min_distance": min_distance,
        "broker_minimum_distance": min_distance,
        "sl_distance": round(final_distances[0], precision),
        "tp1_distance": round(final_distances[1], precision),
        "tp2_distance": round(final_distances[2], precision),
        "sl_distance_pips": round(final_distances[0] / pip_size, 2),
        "tp1_distance_pips": round(final_distances[1] / pip_size, 2),
        "tp_distance_pips": round(final_distances[1] / pip_size, 2),
        "tp2_distance_pips": round(final_distances[2] / pip_size, 2),
        "original_sl_distance": round(original_sl_distance, precision),
        "original_tp1_distance": round(original_tp1_distance, precision),
        "original_tp2_distance": round(original_tp2_distance, precision),
        "original_sl_distance_pips": round(original_sl_distance / pip_size, 2),
        "original_tp1_distance_pips": round(original_tp1_distance / pip_size, 2),
        "original_tp_distance_pips": round(original_tp1_distance / pip_size, 2),
        "original_tp2_distance_pips": round(original_tp2_distance / pip_size, 2),
        "broker_minimum_distance_pips": round(min_distance / pip_size, 2),
        "buffer_pips": round(buffer_distance / pip_size, 2),
        "adjusted": adjusted,
    }
    failed_distances = []

    for label, distance in [
        ("SL", final_distances[0]),
        ("TP1", final_distances[1]),
        ("TP2", final_distances[2]),
    ]:
        if distance < min_distance:
            failed_distances.append(label)

    if invalid_after_adjustment:
        failed_distances.append("invalid_direction")

    distance_details["failed_distance_fields"] = failed_distances
    distance_details["failed_distance"] = ", ".join(failed_distances) if failed_distances else None
    distance_details["should_block_for_distance"] = bool(failed_distances)

    broker_min_distance_debug = {
        "symbol": normalized_symbol,
        "entry": entry_value,
        "sl": sl_value,
        "tp1": tp1_value,
        "tp2": tp2_value,
        "broker_min_distance": min_distance,
        "sl_distance": round(final_distances[0], precision),
        "tp1_distance": round(final_distances[1], precision),
        "tp2_distance": round(final_distances[2], precision),
        "pip_size": pip_size,
        "sl_distance_pips": distance_details["sl_distance_pips"],
        "tp1_distance_pips": distance_details["tp1_distance_pips"],
        "tp2_distance_pips": distance_details["tp2_distance_pips"],
        "failed_distance_fields": failed_distances,
        "blocked": bool(failed_distances),
        "reason": (
            f"{distance_details['failed_distance']} below broker minimum"
            if failed_distances
            else (
                "broker distance adjusted"
                if adjusted
                else "broker distance ok"
            )
        ),
    }
    distance_details["broker_min_distance_debug"] = broker_min_distance_debug
    print("BROKER_MIN_DISTANCE_DEBUG =", broker_min_distance_debug)

    if invalid_after_adjustment or any(distance < min_distance for distance in final_distances):
        payload = reject(
            "Adjusted broker distances would make trade invalid: "
            f"entry {entry_value}, SL {sl_value}, TP {tp1_value}, "
            f"SL distance {distance_details['sl_distance_pips']} pips, "
            f"TP distance {distance_details['tp_distance_pips']} pips, "
            f"broker minimum {distance_details['broker_minimum_distance_pips']} pips"
        )
        payload["distance_details"] = distance_details
        return payload

    adjustment_reason = None

    if adjusted:
        adjustment_reason = (
            "Broker distance adjusted: "
            f"entry {entry_value}, SL {original_sl_value} -> {sl_value} "
            f"({distance_details['original_sl_distance_pips']} -> {distance_details['sl_distance_pips']} pips), "
            f"TP {original_tp1_value} -> {tp1_value} "
            f"({distance_details['original_tp_distance_pips']} -> {distance_details['tp_distance_pips']} pips), "
            f"broker minimum {distance_details['broker_minimum_distance_pips']} pips"
        )

    payload = build_trade_payload(
        True,
        CONNECTED.get("mode", "demo"),
        normalized_symbol,
        normalized_action,
        entry_value,
        sl_value,
        tp1_value,
        tp2_value,
        reason=None
    )
    payload["distance_details"] = distance_details
    payload["adjusted_for_broker_distance"] = adjusted
    payload["adjustment_reason"] = adjustment_reason

    return payload


def connect_account(account_id, mode="demo"):
    """
    Connect cTrader account
    """
    config = get_ctrader_config()
    selected_mode = config.get("env") if config else str(mode or "demo").lower()
    selected_account_id = account_id or (config.get("account_id") if config else None)

    if selected_account_id:
        active_result = set_active_ctrader_account(selected_account_id)

        if not active_result.get("ok"):
            CONNECTED["connected"] = bool(config and config.get("access_token"))
            CONNECTED["status"] = CONNECTED["connected"]
            CONNECTED["mode"] = selected_mode
            CONNECTED["account_id"] = None
            CONNECTED["execution_ready"] = False

            return {
                "ok": False,
                "connected": CONNECTED["connected"],
                "mode": selected_mode,
                "account_id": None,
                "execution_ready": False,
                "reason": active_result.get("reason"),
            }

        config = get_ctrader_config()
        selected_mode = config.get("env") if config else active_result.get("env", selected_mode)

    CONNECTED["connected"] = True
    CONNECTED["status"] = True
    CONNECTED["mode"] = selected_mode
    CONNECTED["account_id"] = selected_account_id
    CONNECTED["execution_ready"] = bool(config)

    print(f"cTrader connected -> {selected_mode}")

    return {
        "ok": True,
        "connected": True,
        "mode": selected_mode,
        "account_id": selected_account_id,
        "execution_ready": bool(config)
    }


def disconnect_account():

    CONNECTED["connected"] = False
    CONNECTED["status"] = False
    CONNECTED["account_id"] = None
    CONNECTED["execution_ready"] = False
    clear_ctrader_tokens_and_accounts()

    print("cTrader disconnected")

    return {
        "ok": True
    }

def convert_lots_to_ctrader_volume(volume, lot_size=None):
    try:
        lots = float(volume)
    except (TypeError, ValueError):
        lots = 0.01

    try:
        broker_lot_size = float(lot_size)
    except (TypeError, ValueError):
        broker_lot_size = 0

    if lots <= 0:
        lots = 0.01

    if lots >= 1000:
        return int(lots)

    if broker_lot_size <= 0:
        raise ValueError("Missing broker lotSize for cTrader volume conversion")

    return max(1, int(round(lots * broker_lot_size)))

def convert_ctrader_volume_to_lots(volume_units, lot_size=None):
    try:
        units = float(volume_units)
    except (TypeError, ValueError):
        return None

    try:
        broker_lot_size = float(lot_size)
    except (TypeError, ValueError):
        broker_lot_size = 0

    if units <= 0:
        return None

    if broker_lot_size <= 0:
        return None

    return units / broker_lot_size

def convert_price_distance_to_relative_units(distance):
    try:
        value = abs(float(distance))
    except (TypeError, ValueError):
        return None

    return max(1, int(round(value * 100000)))

def prices_close(a, b, tolerance=0.00001):
    try:
        return abs(float(a) - float(b)) <= float(tolerance)
    except (TypeError, ValueError):
        return False

def parse_ctrader_bad_volume_message(message):
    text = str(message or "")
    match = re.search(
        r"Order volume\s*=\s*([0-9]+(?:\.[0-9]+)?).*?"
        r"minimum allowed volume\s*=\s*([0-9]+(?:\.[0-9]+)?)",
        text
    )

    if not match:
        return {
            "broker_reported_volume_if_rejected": None,
            "broker_minimum_from_error": None,
        }

    return {
        "broker_reported_volume_if_rejected": float(match.group(1)),
        "broker_minimum_from_error": float(match.group(2)),
    }

def build_ctrader_order_payload_volume_check(symbol, calculated_volume_units, volume_in_payload, risk=None, rejection_message=None):
    risk = risk or {}
    rejection = parse_ctrader_bad_volume_message(rejection_message)
    broker_reported_volume = rejection["broker_reported_volume_if_rejected"]
    normalized_symbol = normalize_symbol(symbol)
    configured_scale_factor = CTRADER_PAYLOAD_VOLUME_SCALE.get(normalized_symbol, 1)
    scale_factor = configured_scale_factor
    broker_interpreted_volume = None

    try:
        broker_interpreted_volume = float(volume_in_payload) / float(configured_scale_factor)
    except (TypeError, ValueError, ZeroDivisionError):
        broker_interpreted_volume = None

    if broker_reported_volume:
        try:
            scale_factor = float(volume_in_payload) / float(broker_reported_volume)
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    min_volume = risk.get("min_volume_units")
    max_volume = risk.get("max_volume_units")
    step_volume = risk.get("volume_step_units")
    final_risk_percent = risk.get("final_risk_percent")
    target_risk_percent = risk.get("risk_percent")
    maximum_allowed_risk_percent = risk.get("maximum_allowed_risk_percent")
    risk_tolerance_percent = risk.get("risk_tolerance_percent", 0.05)

    try:
        min_volume_value = float(min_volume)
    except (TypeError, ValueError):
        min_volume_value = None

    try:
        max_volume_value = float(max_volume)
    except (TypeError, ValueError):
        max_volume_value = None

    try:
        step_volume_value = float(step_volume)
    except (TypeError, ValueError):
        step_volume_value = None

    try:
        lot_contract_size = float(risk.get("lot_contract_size") or risk.get("lot_size_contract") or 0)
    except (TypeError, ValueError):
        lot_contract_size = 0

    broker_min_lots = (
        min_volume_value / lot_contract_size
        if min_volume_value is not None and lot_contract_size > 0
        else None
    )

    try:
        final_risk_percent_value = float(final_risk_percent)
    except (TypeError, ValueError):
        final_risk_percent_value = None

    try:
        target_risk_percent_value = float(target_risk_percent)
    except (TypeError, ValueError):
        target_risk_percent_value = 0.5

    try:
        maximum_allowed_risk_percent_value = float(maximum_allowed_risk_percent)
    except (TypeError, ValueError):
        maximum_allowed_risk_percent_value = target_risk_percent_value + 0.01

    try:
        risk_tolerance_percent_value = max(0.0, float(risk_tolerance_percent))
    except (TypeError, ValueError):
        risk_tolerance_percent_value = 0.05

    minimum_source = "metadata"

    if min_volume_value is None and rejection["broker_minimum_from_error"] is not None:
        min_volume_value = float(rejection["broker_minimum_from_error"])
        minimum_source = "rejection"

    aligns_with_step = True

    if broker_interpreted_volume is not None and step_volume_value:
        aligns_with_step = abs(
            broker_interpreted_volume % step_volume_value
        ) < 1e-9

    risk_within_limit = (
        final_risk_percent_value is not None
        and final_risk_percent_value > 0
        and final_risk_percent_value
        <= maximum_allowed_risk_percent_value + 1e-9
    )
    risk_difference = (
        final_risk_percent_value - maximum_allowed_risk_percent_value
        if final_risk_percent_value is not None
        else None
    )
    print("RISK_VALIDATION =", {
        "symbol": normalized_symbol,
        "actual_risk": final_risk_percent_value,
        "max_risk": maximum_allowed_risk_percent_value,
        "tolerance": risk_tolerance_percent_value,
        "difference": (
            round(risk_difference, 4)
            if risk_difference is not None
            else None
        ),
        "decision": "ALLOW" if risk_within_limit else "BLOCK",
    })

    below_minimum_volume = (
        broker_interpreted_volume is not None
        and min_volume_value is not None
        and broker_interpreted_volume < min_volume_value
    )
    valid_in_broker_units = (
        broker_interpreted_volume is not None
        and not below_minimum_volume
        and (max_volume_value is None or broker_interpreted_volume <= max_volume_value)
        and aligns_with_step
        and risk_within_limit
    )
    failed_checks = []

    if broker_interpreted_volume is None:
        failed_checks.append("invalid payload volume")
    if below_minimum_volume:
        failed_checks.append("below broker minimum")
    if (
        broker_interpreted_volume is not None
        and max_volume_value is not None
        and broker_interpreted_volume > max_volume_value
    ):
        failed_checks.append("above broker maximum")
    if not aligns_with_step:
        failed_checks.append("not aligned with broker step")
    if not risk_within_limit:
        failed_checks.append("risk above allowed tolerance")
    blocked_reason = (
        None
        if valid_in_broker_units
        else f"cTrader payload volume safety check failed: {', '.join(failed_checks)}"
    )

    return {
        "symbol": normalized_symbol,
        "risk_percent": risk.get("risk_percent"),
        "risk_money": risk.get("risk_amount"),
        "risk_amount": risk.get("risk_amount"),
        "stop_loss_pips": risk.get("sl_pips"),
        "sl_pips": risk.get("sl_pips"),
        "stop_loss_price_distance": risk.get("stop_loss_price_distance"),
        "pip_value": risk.get("pip_value_per_lot"),
        "pip_value_per_lot": risk.get("pip_value_per_lot"),
        "calculated_lots": risk.get("calculated_lots") or risk.get("raw_lots"),
        "rounded_lots": risk.get("rounded_lots") or risk.get("lot_size"),
        "lot_size_before_rounding": risk.get("calculated_lots") or risk.get("raw_lots"),
        "lot_size_after_rounding": risk.get("lot_size") or risk.get("rounded_lots"),
        "broker_min_lots": broker_min_lots,
        "broker_min_volume": min_volume_value,
        "broker_max_volume": max_volume_value,
        "broker_volume_step": step_volume_value,
        "volume_step": step_volume_value,
        "final_volume": int(volume_in_payload) if volume_in_payload is not None else None,
        "payload_volume": int(volume_in_payload) if volume_in_payload is not None else None,
        "requested_units": int(calculated_volume_units) if calculated_volume_units is not None else None,
        "max_lot_cap_used": False,
        "calculated_volume_units": int(calculated_volume_units) if calculated_volume_units is not None else None,
        "volume_before_payload": int(calculated_volume_units) if calculated_volume_units is not None else None,
        "volume_in_payload": int(volume_in_payload) if volume_in_payload is not None else None,
        "broker_interpreted_volume": broker_interpreted_volume,
        "broker_reported_volume_if_rejected": broker_reported_volume,
        "broker_minimum_from_error": rejection["broker_minimum_from_error"],
        "scale_factor_detected": scale_factor,
        "scale_factor_configured": configured_scale_factor,
        "minVolume": min_volume_value,
        "minimum_source": minimum_source,
        "maxVolume": max_volume_value,
        "stepVolume": step_volume_value,
        "aligns_with_step": aligns_with_step,
        "final_risk_percent": final_risk_percent_value,
        "target_risk_percent": target_risk_percent_value,
        "maximum_allowed_risk_percent": maximum_allowed_risk_percent_value,
        "risk_tolerance_percent": risk_tolerance_percent_value,
        "risk_difference_percent": risk_difference,
        "risk_within_limit": risk_within_limit,
        "risk_still_0_5_percent": risk_within_limit,
        "below_minimum_volume": below_minimum_volume,
        "minimum_volume_warning": below_minimum_volume,
        "failed_checks": failed_checks,
        "payload_volume_valid_in_broker_units": valid_in_broker_units,
        "blocked_reason": blocked_reason,
    }

def log_volume_safety_debug(volume_check):
    if not isinstance(volume_check, dict):
        return

    print("VOLUME_SAFETY_DEBUG =", {
        "symbol": volume_check.get("symbol"),
        "risk_percent": volume_check.get("risk_percent"),
        "stop_loss_pips": volume_check.get("stop_loss_pips"),
        "stop_loss_price_distance": volume_check.get("stop_loss_price_distance"),
        "lot_size_before_rounding": volume_check.get("lot_size_before_rounding"),
        "lot_size_after_rounding": volume_check.get("lot_size_after_rounding"),
        "broker_min_volume": volume_check.get("broker_min_volume"),
        "broker_max_volume": volume_check.get("broker_max_volume"),
        "broker_volume_step": volume_check.get("broker_volume_step"),
        "final_volume": volume_check.get("final_volume"),
        "blocked_reason": volume_check.get("blocked_reason"),
    })


def classify_ctrader_failure(order_dispatched):
    """Classify unknown exceptions solely by whether the order was dispatched."""
    return "AMBIGUOUS" if order_dispatched else "FAILED_BEFORE_SEND"


def _validate_ctrader_order_reference_lengths(client_order_id, label, comment):
    if client_order_id is not None and len(str(client_order_id)) > 50:
        raise ValueError("cTrader clientOrderId exceeds 50 characters")
    if label is not None and len(str(label)) > 100:
        raise ValueError("cTrader label exceeds 100 characters")
    if comment is not None and len(str(comment)) > 100:
        raise ValueError("cTrader comment exceeds 100 characters")


def build_ctrader_market_order_payload(
    account_id,
    symbol_id,
    symbol,
    action,
    entry,
    sl,
    tp1,
    volume,
    volume_units=None,
    digits=5,
    lot_size=100000,
    risk=None,
    client_order_id=None,
    broker_label=None,
    broker_comment=None,
):
    normalized_action = str(action or "").upper()
    normalized_symbol = normalize_symbol(symbol)
    trade_side = "BUY" if normalized_action == "BUY" else "SELL"
    try:
        precision = int(digits)
    except (TypeError, ValueError):
        precision = get_default_symbol_digits(symbol) or 5

    entry_value = round(float(entry), precision)
    sl_value = round(float(sl), precision)
    # Historical parameter name is tp1, but place_market_order passes the
    # broker take-profit here, which must be TP2 for NathauxFX trades.
    broker_tp2_value = round(float(tp1), precision)

    if not all(math.isfinite(value) for value in [
        entry_value,
        sl_value,
        broker_tp2_value,
    ]):
        print("LIVE_ORDER_BLOCKED_MISSING_LEVELS", {
            "symbol": normalized_symbol,
            "reason": "non-finite broker protection level",
        })
        raise ValueError(
            "LIVE_ORDER_BLOCKED_MISSING_LEVELS: levels must be finite numbers"
        )

    invalid_direction = (
        trade_side == "BUY"
        and (sl_value >= entry_value or broker_tp2_value <= entry_value)
    ) or (
        trade_side == "SELL"
        and (sl_value <= entry_value or broker_tp2_value >= entry_value)
    )
    if invalid_direction:
        print("LIVE_ORDER_BLOCKED_MISSING_LEVELS", {
            "symbol": normalized_symbol,
            "side": trade_side,
            "entry": entry_value,
            "sl": sl_value,
            "tp2": broker_tp2_value,
            "reason": "invalid SL/TP direction or distance",
        })
        raise ValueError(
            "LIVE_ORDER_BLOCKED_MISSING_LEVELS: invalid SL/TP distance"
        )

    calculated_volume_units = (
        int(volume_units)
        if volume_units
        else convert_lots_to_ctrader_volume(volume, lot_size)
    )
    volume_scale = CTRADER_PAYLOAD_VOLUME_SCALE.get(normalized_symbol, 1)
    payload_volume = int(calculated_volume_units * volume_scale)
    volume_check = build_ctrader_order_payload_volume_check(
        symbol,
        calculated_volume_units,
        payload_volume,
        risk=risk
    )

    if not volume_check.get("payload_volume_valid_in_broker_units"):
        log_volume_safety_debug(volume_check)
        raise ValueError(
            "cTrader payload volume safety check failed: "
            f"{volume_check}"
        )

    resolved_client_order_id = str(
        client_order_id or f"flowsignal-{uuid.uuid4()}"
    )
    resolved_label = str(broker_label or resolved_client_order_id)
    resolved_comment = str(broker_comment or f"FS:{resolved_client_order_id}")
    _validate_ctrader_order_reference_lengths(
        resolved_client_order_id, resolved_label, resolved_comment
    )

    payload = {
        "ctidTraderAccountId": int(account_id),
        "symbolId": int(symbol_id),
        "orderType": "MARKET",
        "tradeSide": trade_side,
        "volume": payload_volume,
        "label": resolved_label,
        "comment": resolved_comment,
        "clientOrderId": resolved_client_order_id,
    }
    payload["_volume_check"] = volume_check

    relative_sl = convert_price_distance_to_relative_units(entry_value - sl_value)
    relative_tp = convert_price_distance_to_relative_units(broker_tp2_value - entry_value)

    if not relative_sl or not relative_tp:
        print("LIVE_ORDER_BLOCKED_MISSING_LEVELS", {
            "symbol": normalized_symbol,
            "entry": entry_value,
            "sl": sl_value,
            "tp2": broker_tp2_value,
            "relativeStopLoss": relative_sl,
            "relativeTakeProfit": relative_tp,
        })
        raise ValueError(
            "LIVE_ORDER_BLOCKED_MISSING_LEVELS: broker SL/TP payload is required"
        )

    print(
        "CTRADER ORDER LEVELS:",
        {
            "symbol": symbol,
            "digits": precision,
            "entry": entry_value,
            "sl": sl_value,
            "broker_tp2": broker_tp2_value,
            "relativeStopLoss": relative_sl,
            "relativeTakeProfit": relative_tp,
            "lotSize": lot_size,
        }
    )

    payload["relativeStopLoss"] = relative_sl
    payload["relativeTakeProfit"] = relative_tp

    if not payload.get("relativeStopLoss") or not payload.get("relativeTakeProfit"):
        print("LIVE_ORDER_BLOCKED_MISSING_LEVELS", {
            "symbol": normalized_symbol,
            "payload_has_sl": bool(payload.get("relativeStopLoss")),
            "payload_has_tp": bool(payload.get("relativeTakeProfit")),
        })
        raise ValueError(
            "LIVE_ORDER_BLOCKED_MISSING_LEVELS: refusing unprotected live order"
        )

    return payload


def place_market_order(
    symbol,
    side=None,
    volume=None,
    action=None,
    entry=None,
    sl=None,
    tp=None,
    tp1=None,
    tp2=None,
    mode=None,
    volume_units=None,
    risk=None,
    client_order_id=None,
    broker_label=None,
    broker_comment=None,
):
    normalized_action = str(action or side or "").upper()
    normalized_symbol = normalize_symbol(symbol)
    selected_tp1 = tp1 if tp1 is not None else tp
    broker_take_profit = tp2 if tp2 is not None else selected_tp1

    print(f"CTRADER MARKET ORDER REQUEST -> {normalized_symbol} {normalized_action}")

    try:
        _validate_ctrader_order_reference_lengths(
            client_order_id, broker_label, broker_comment
        )
    except ValueError as exc:
        return {
            "ok": False,
            "broker": "ctrader",
            "mode": mode or CONNECTED.get("mode", "demo"),
            "symbol": normalized_symbol,
            "action": normalized_action,
            "side": normalized_action,
            "reason": str(exc),
            "status": "FAILED_BEFORE_SEND",
            "broker_result": "FAILED_BEFORE_SEND",
            "broker_order_sent": False,
        }

    config = get_ctrader_config()
    order_payload = None
    volume_check = None

    if not config:
        return {
            "ok": False,
            "broker": "ctrader",
            "mode": mode or CONNECTED.get("mode", "demo"),
            "symbol": normalized_symbol,
            "action": normalized_action,
            "side": normalized_action,
            "reason": "Missing or invalid cTrader config",
            "status": "FAILED_BEFORE_SEND",
            "broker_result": "FAILED_BEFORE_SEND",
            "broker_order_sent": False,
        }

    account_id = int(config["account_id"])
    host, port = CTRADER_JSON_ENDPOINTS[config["env"]]
    sock = None
    order_dispatched = False
    order_payload = None

    try:
        sock = open_ctrader_json_socket(host, port)
        authorize_ctrader_socket(sock, config, account_id)
        symbol_details = fetch_ctrader_symbol_details(sock, account_id)
        symbol_info = resolve_ctrader_symbol(symbol_details, normalized_symbol)

        if not symbol_info:
            raise RuntimeError(f"cTrader symbol not found: {normalized_symbol}")

        order_payload = build_ctrader_market_order_payload(
            account_id,
            symbol_info["symbol_id"],
            normalized_symbol,
            normalized_action,
            entry,
            sl,
            broker_take_profit,
            volume,
            volume_units=volume_units,
            digits=symbol_info.get("digits", get_default_symbol_digits(normalized_symbol) or 5),
            lot_size=(
                symbol_info.get("raw", {}).get("lotSize")
                or get_symbol_risk_fallback(normalized_symbol)["lot_size"]
            ),
            risk=risk,
            client_order_id=client_order_id,
            broker_label=broker_label,
            broker_comment=broker_comment,
        )
        volume_check = order_payload.pop("_volume_check", {})

        safe_payload = {
            **order_payload,
            "clientOrderId": "***",
        }
        if normalized_symbol == "XAUUSD":
            print("XAUUSD_CTRADER_EXACT_ORDER_PAYLOAD:")
            print(json.dumps(order_payload, indent=2, default=str))
        print("CTRADER NEW ORDER REQUEST:", safe_payload)
        print("CTRADER NEW ORDER JSON:", json.dumps(safe_payload, default=str))
        print("CTRADER_TP_PROTECTION_AUDIT =", {
            "stage": "payload_built",
            "symbol": normalized_symbol,
            "side": normalized_action,
            "entry": entry,
            "SL": sl,
            "TP1": selected_tp1,
            "TP2": tp2,
            "broker_take_profit": broker_take_profit,
            "payload_tp_field": "relativeTakeProfit",
            "payload_relativeTakeProfit": order_payload.get("relativeTakeProfit"),
            "payload_sl_field": "relativeStopLoss",
            "payload_relativeStopLoss": order_payload.get("relativeStopLoss"),
            "tp1_managed_by_flowsignal": True,
            "tp2_sent_to_broker": True,
        })
        print("CTRADER_ORDER_PAYLOAD_VOLUME_CHECK:", volume_check)
        log_volume_safety_debug(volume_check)

        # Once this flag is set, every unknown failure is ambiguous and must
        # be reconciled. It must never be converted into a retryable failure.
        order_dispatched = True
        response = send_ctrader_request(
            sock,
            PAYLOAD_NEW_ORDER_REQ,
            order_payload,
            PAYLOAD_EXECUTION_EVENT,
        )
        payload = response.get("payload", {})

        if payload.get("errorCode"):
            volume_check = build_ctrader_order_payload_volume_check(
                normalized_symbol,
                volume_units,
                order_payload.get("volume") if order_payload else volume_units,
                risk=risk,
                rejection_message=payload.get("description") or payload
            )
            print("CTRADER_ORDER_PAYLOAD_VOLUME_CHECK:", volume_check)
            log_volume_safety_debug(volume_check)
            return {
                "ok": False,
                "broker": "ctrader",
                "mode": config["env"],
                "symbol": normalized_symbol,
                "action": normalized_action,
                "side": normalized_action,
                "reason": payload.get("errorCode"),
                "status": "REJECTED",
                "broker_result": "DEFINITELY_REJECTED",
                "broker_order_sent": True,
                "raw": payload,
                "volume_safety": volume_check,
            }

        order = payload.get("order") or {}
        position = payload.get("position") or {}
        deal = payload.get("deal") or {}
        execution_type = str(payload.get("executionType") or "").upper()
        order_status = str(order.get("orderStatus") or "").upper()
        confirmed_rejection = (
            execution_type in {"7", "ORDER_REJECTED"}
            or order_status in {"3", "ORDER_STATUS_REJECTED", "REJECTED"}
        )
        if confirmed_rejection and not position and not deal:
            return {
                "ok": False,
                "broker": "ctrader",
                "mode": config["env"],
                "symbol": normalized_symbol,
                "action": normalized_action,
                "side": normalized_action,
                "reason": payload.get("errorCode") or "cTrader confirmed order rejection",
                "status": "REJECTED",
                "broker_result": "DEFINITELY_REJECTED",
                "broker_order_sent": True,
                "order_id": order.get("orderId") or payload.get("orderId"),
                "raw": payload,
            }
        accepted_position_id = (
            position.get("positionId")
            or deal.get("positionId")
            or payload.get("positionId")
        )
        accepted_order_id = (
            order.get("orderId") or deal.get("orderId") or payload.get("orderId")
        )
        accepted_deal_id = deal.get("dealId") or payload.get("dealId")
        if not any((accepted_position_id, accepted_order_id, accepted_deal_id)):
            return {
                "ok": False,
                "broker": "ctrader",
                "mode": config["env"],
                "symbol": normalized_symbol,
                "action": normalized_action,
                "side": normalized_action,
                "reason": "Malformed cTrader execution response without broker identity",
                "status": "UNKNOWN",
                "broker_result": "AMBIGUOUS",
                "broker_order_sent": True,
                "raw": payload,
            }
        if not accepted_position_id:
            return {
                "ok": False,
                "broker": "ctrader",
                "mode": config["env"],
                "symbol": normalized_symbol,
                "action": normalized_action,
                "side": normalized_action,
                "reason": "cTrader acknowledged an order without a confirmed position",
                "status": "UNKNOWN",
                "broker_result": "AMBIGUOUS",
                "broker_order_sent": True,
                "order_id": accepted_order_id,
                "deal_id": accepted_deal_id,
                "raw": payload,
            }
        broker_sl_confirmed = False
        broker_tp_confirmed = False
        broker_sl_after_send = first_present(
            position.get("stopLoss"),
            order.get("stopLoss"),
            deal.get("stopLoss"),
            payload.get("stopLoss"),
        )
        broker_tp_after_send = first_present(
            position.get("takeProfit"),
            order.get("takeProfit"),
            deal.get("takeProfit"),
            payload.get("takeProfit"),
        )
        broker_position_after_send = None
        tp_amend_result = None

        if broker_sl_after_send is not None:
            broker_sl_confirmed = prices_close(
                broker_sl_after_send,
                sl,
                tolerance=10 ** -int(symbol_info.get("digits", get_default_symbol_digits(normalized_symbol) or 5)),
            )

        if broker_tp_after_send is not None:
            broker_tp_confirmed = prices_close(
                broker_tp_after_send,
                broker_take_profit,
                tolerance=10 ** -int(symbol_info.get("digits", get_default_symbol_digits(normalized_symbol) or 5)),
            )

        if accepted_position_id and (not broker_sl_confirmed or not broker_tp_confirmed):
            try:
                positions_after_send = fetch_ctrader_open_positions(config)
                broker_position_after_send = next(
                    (
                        item
                        for item in positions_after_send
                        if str(item.get("position_id")) == str(accepted_position_id)
                    ),
                    None,
                )
                if isinstance(broker_position_after_send, dict):
                    broker_sl_after_send = broker_position_after_send.get("stop_loss")
                    broker_tp_after_send = broker_position_after_send.get("take_profit")
                broker_sl_confirmed = prices_close(
                    broker_sl_after_send,
                    sl,
                    tolerance=10 ** -int(symbol_info.get("digits", get_default_symbol_digits(normalized_symbol) or 5)),
                )
                broker_tp_confirmed = prices_close(
                    broker_tp_after_send,
                    broker_take_profit,
                    tolerance=10 ** -int(symbol_info.get("digits", get_default_symbol_digits(normalized_symbol) or 5)),
                )
            except Exception as verify_error:
                print("CTRADER_TP_VERIFY_ERROR:", {
                    "symbol": normalized_symbol,
                    "position_id": accepted_position_id,
                    "error": str(verify_error),
                })

        if accepted_position_id and (not broker_sl_confirmed or not broker_tp_confirmed):
            tp_amend_result = modify_position_sltp(
                accepted_position_id,
                stop_loss_price=sl,
                take_profit_price=broker_take_profit,
            )
            if tp_amend_result.get("ok"):
                try:
                    positions_after_amend = fetch_ctrader_open_positions(config)
                    broker_position_after_send = next(
                        (
                            item
                            for item in positions_after_amend
                            if str(item.get("position_id")) == str(accepted_position_id)
                        ),
                        None,
                    )
                    if isinstance(broker_position_after_send, dict):
                        broker_sl_after_send = broker_position_after_send.get("stop_loss")
                        broker_tp_after_send = broker_position_after_send.get("take_profit")
                    broker_sl_confirmed = prices_close(
                        broker_sl_after_send,
                        sl,
                        tolerance=10 ** -int(symbol_info.get("digits", get_default_symbol_digits(normalized_symbol) or 5)),
                    )
                    broker_tp_confirmed = prices_close(
                        broker_tp_after_send,
                        broker_take_profit,
                        tolerance=10 ** -int(symbol_info.get("digits", get_default_symbol_digits(normalized_symbol) or 5)),
                    )
                except Exception as verify_error:
                    print("CTRADER_TP_REVERIFY_ERROR:", {
                        "symbol": normalized_symbol,
                        "position_id": accepted_position_id,
                        "error": str(verify_error),
                    })

        if not broker_sl_confirmed or not broker_tp_confirmed:
            missing_protection = []
            if not broker_sl_confirmed:
                missing_protection.append("SL")
            if not broker_tp_confirmed:
                missing_protection.append("TP")
            reason = (
                "cTrader accepted the position but broker "
                f"{'/'.join(missing_protection)} was not confirmed"
            )
            print("LIVE_RISK_ERROR:", {
                "symbol": normalized_symbol,
                "position_id": accepted_position_id,
                "expected_sl": sl,
                "broker_stop_loss": broker_sl_after_send,
                "expected_tp2": broker_take_profit,
                "broker_take_profit": broker_tp_after_send,
                "amend_result": tp_amend_result,
                "broker_position": broker_position_after_send,
            })
            print("CTRADER_SLTP_CONFIRMATION =", {
                "stage": "broker_response_unprotected_after_repair",
                "symbol": normalized_symbol,
                "side": normalized_action,
                "entry": entry,
                "saved_sl": sl,
                "broker_sl": broker_sl_after_send,
                "displayed_sl": None,
                "tp2": broker_take_profit,
                "broker_tp": broker_tp_after_send,
                "lot_size": volume,
                "repair_result": tp_amend_result,
            })
            return {
                "ok": False,
                "broker": "ctrader",
                "mode": config["env"],
                "symbol": normalized_symbol,
                "action": normalized_action,
                "side": normalized_action,
                "entry": entry,
                "sl": sl,
                "tp1": selected_tp1,
                "tp2": tp2,
                "broker_take_profit": broker_take_profit,
                "volume": volume,
                "volume_units": order_payload.get("volume"),
                "risk": risk,
                "reason": reason,
                "status": "ACCEPTED_PROTECTION_FAILED",
                "broker_result": "ACCEPTED_PROTECTION_FAILED",
                "broker_order_sent": True,
                "critical_unprotected_position": True,
                "broker_sl_confirmed": broker_sl_confirmed,
                "broker_stop_loss_attached": broker_sl_after_send,
                "broker_tp_confirmed": broker_tp_confirmed,
                "broker_take_profit_attached": broker_tp_after_send,
                "order_id": (
                    order.get("orderId")
                    or deal.get("orderId")
                    or payload.get("orderId")
                ),
                "position_id": accepted_position_id,
                "tp_amend_result": tp_amend_result,
                "broker_position": broker_position_after_send,
                "raw": payload,
            }

        print(
            "CTRADER_ORDER_ACCEPTED_VOLUME_CHECK:",
            {
                "symbol": normalized_symbol,
                "requested_calculated_volume": volume_check.get("calculated_volume_units"),
                "payload_volume_sent": volume_check.get("volume_in_payload"),
                "broker_interpreted_volume": volume_check.get("broker_interpreted_volume"),
                "position_id": accepted_position_id,
                "broker_tp_confirmed": broker_tp_confirmed,
                "broker_sl_confirmed": broker_sl_confirmed,
                "broker_stop_loss": broker_sl_after_send,
                "broker_take_profit": broker_tp_after_send,
            }
        )
        print("CTRADER_TP_PROTECTION_AUDIT =", {
            "stage": "broker_response_verified",
            "symbol": normalized_symbol,
            "side": normalized_action,
            "entry": entry,
            "SL": sl,
            "TP1": selected_tp1,
            "TP2": tp2,
            "broker_take_profit_expected": broker_take_profit,
            "broker_take_profit_response": broker_tp_after_send,
            "broker_tp_confirmed": broker_tp_confirmed,
            "tp_amend_result": tp_amend_result,
            "tp1_managed_by_flowsignal": True,
            "tp2_sent_to_broker": True,
            "tp_missing_reason": (
                None
                if broker_tp_confirmed
                else "Broker TP2 was not present after send/amend verification"
            ),
        })
        print("CTRADER_SLTP_CONFIRMATION =", {
            "stage": "broker_response_verified",
            "symbol": normalized_symbol,
            "side": normalized_action,
            "entry": entry,
            "saved_sl": sl,
            "broker_sl": broker_sl_after_send,
            "displayed_sl": broker_sl_after_send if broker_sl_confirmed else None,
            "tp2": broker_take_profit,
            "broker_tp": broker_tp_after_send,
            "lot_size": volume,
            "repair_result": tp_amend_result,
        })

        return {
            "ok": True,
            "broker": "ctrader",
            "mode": config["env"],
            "symbol": normalized_symbol,
            "action": normalized_action,
            "side": normalized_action,
            "entry": entry,
            "sl": sl,
            "tp1": selected_tp1,
            "tp2": tp2,
            "broker_take_profit": broker_take_profit,
            "broker_sl_confirmed": broker_sl_confirmed,
            "broker_stop_loss_attached": broker_sl_after_send,
            "broker_tp_confirmed": broker_tp_confirmed,
            "broker_take_profit_attached": broker_tp_after_send,
            "tp_amend_result": tp_amend_result,
            "volume": volume,
            "volume_units": order_payload.get("volume"),
            "risk": risk,
            "reason": None,
            "status": "SENT",
            "broker_result": "ACCEPTED",
            "broker_order_sent": True,
            "order_id": (
                order.get("orderId")
                or deal.get("orderId")
                or payload.get("orderId")
            ),
            "position_id": (
                accepted_position_id
            ),
            "execution_type": payload.get("executionType"),
            "raw": payload,
        }
    except Exception as e:
        print("CTRADER MARKET ORDER ERROR:", e)
        try:
            volume_check = build_ctrader_order_payload_volume_check(
                normalized_symbol,
                volume_units,
                order_payload.get("volume") if order_payload else volume_units,
                risk=risk,
                rejection_message=str(e)
            )
            print("CTRADER_ORDER_PAYLOAD_VOLUME_CHECK:", volume_check)
            log_volume_safety_debug(volume_check)
        except Exception as log_error:
            print("CTRADER_ORDER_PAYLOAD_VOLUME_CHECK_ERROR:", log_error)
        return {
            "ok": False,
            "broker": "ctrader",
            "mode": config["env"],
            "symbol": normalized_symbol,
            "action": normalized_action,
            "side": normalized_action,
            "reason": str(e),
            "status": "UNKNOWN" if order_dispatched else "FAILED_BEFORE_SEND",
            "broker_result": classify_ctrader_failure(order_dispatched),
            "broker_order_sent": bool(order_dispatched),
            "volume_safety": volume_check if isinstance(volume_check, dict) else None,
        }
    except BaseException as e:
        if e.__class__.__name__ != "CancelledError":
            raise
        return {
            "ok": False,
            "broker": "ctrader",
            "mode": config["env"],
            "symbol": normalized_symbol,
            "action": normalized_action,
            "side": normalized_action,
            "reason": str(e) or "cTrader request cancelled",
            "status": "UNKNOWN" if order_dispatched else "FAILED_BEFORE_SEND",
            "broker_result": classify_ctrader_failure(order_dispatched),
            "broker_order_sent": bool(order_dispatched),
        }
    finally:
        try:
            sock.close()
        except Exception:
            pass

def parse_ctrader_money(value, money_digits=2):
    if value is None:
        return None

    raw_text = str(value)

    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None

    try:
        digits = int(money_digits if money_digits is not None else 2)
    except (TypeError, ValueError):
        digits = 2

    is_scaled_integer = (
        isinstance(value, int)
        or (raw_text.replace("-", "").isdigit() and "." not in raw_text)
    )

    if is_scaled_integer and digits >= 0:
        return numeric / (10 ** digits)

    return numeric

def get_ctrader_account_snapshot():
    config = get_ctrader_config()
    settings = load_ctrader_account_settings()

    if not config:
        return {
            "ok": False,
            "broker": "ctrader",
            "reason": "Missing or invalid cTrader config",
            "balance": None,
            "equity": None,
            "balance_verified": False,
            "equity_verified": False,
            "cached_balance_used": False,
            "account_list_last_refresh": settings.get("last_refresh"),
        }

    account_id = int(config["account_id"])
    saved_account = get_saved_ctrader_account(account_id) or {}
    host, port = CTRADER_JSON_ENDPOINTS[config["env"]]
    sock = open_ctrader_json_socket(host, port)

    try:
        authorize_ctrader_socket(sock, config, account_id)
        response = send_ctrader_request(
            sock,
            PAYLOAD_TRADER_REQ,
            {
                "ctidTraderAccountId": account_id,
            },
            PAYLOAD_TRADER_RES,
        )
        payload = response.get("payload", {})
        trader = payload.get("trader") or payload
        money_digits = trader.get("moneyDigits", payload.get("moneyDigits", 2))
        raw_balance = trader.get("balance") if "balance" in trader else payload.get("balance")
        raw_equity = trader.get("equity") if "equity" in trader else payload.get("equity")
        balance = parse_ctrader_money(
            raw_balance,
            money_digits
        )
        equity = parse_ctrader_money(
            raw_equity,
            money_digits
        )
        equity_source = "broker"
        equity_verified = equity is not None

        if equity is None and balance is not None:
            try:
                unrealized = fetch_ctrader_position_unrealized_pnl(
                    sock,
                    account_id,
                )
                open_net_pnl = sum(
                    float(item.get("netUnrealizedPnLParsed") or 0)
                    for item in unrealized.get("by_position_id", {}).values()
                    if isinstance(item, dict)
                )
                equity = float(balance) + open_net_pnl
                equity_source = "balance_plus_unrealized_pnl"
                equity_verified = True
            except Exception as equity_error:
                print("CTRADER_EQUITY_CALCULATION_ERROR:", str(equity_error))
                equity = balance
                equity_source = "balance_fallback"
                equity_verified = False

        snapshot_refreshed_at = datetime.now(timezone.utc).isoformat()
        audit = {
            "active_account_id": str(account_id),
            "account_number": (
                trader.get("accountNumber")
                or trader.get("account_number")
                or trader.get("traderLogin")
                or saved_account.get("account_number")
                or saved_account.get("traderLogin")
            ),
            "broker_environment": config["env"],
            "env_from_selection": get_active_ctrader_account_env(),
            "env_from_env_file": os.getenv("CTRADER_ENV"),
            "raw_balance_from_ctrader": raw_balance,
            "raw_equity_from_ctrader": raw_equity,
            "money_digits": money_digits,
            "balance_returned_by_ctrader": balance,
            "equity_returned_by_ctrader": equity,
            "equity_source": equity_source,
            "balance_verified": balance is not None,
            "equity_verified": equity_verified,
            "cached_balance_used": False,
            "cached_balance": saved_account.get("balance"),
            "cached_equity": saved_account.get("equity"),
            "account_list_last_refresh": settings.get("last_refresh"),
            "snapshot_refreshed_at": snapshot_refreshed_at,
        }
        print("CTRADER_ACCOUNT_BALANCE_AUDIT =", audit)
        settings["active_account_snapshot"] = audit
        settings["active_account_snapshot_refreshed_at"] = snapshot_refreshed_at
        save_ctrader_account_settings(settings)

        return {
            "ok": True,
            "broker": "ctrader",
            "mode": config["env"],
            "account_id": account_id,
            "account_number": audit["account_number"],
            "currency": trader.get("depositAssetId") or trader.get("currency"),
            "balance": balance,
            "equity": equity,
            "equity_source": equity_source,
            "balance_verified": balance is not None,
            "equity_verified": equity_verified,
            "cached_balance_used": False,
            "account_list_last_refresh": settings.get("last_refresh"),
            "snapshot_refreshed_at": snapshot_refreshed_at,
            "money_digits": money_digits,
            "raw": {
                key: trader.get(key)
                for key in [
                    "balance",
                    "equity",
                    "moneyDigits",
                    "depositAssetId",
                ]
                if key in trader
            },
        }
    except Exception as e:
        print("CTRADER ACCOUNT SNAPSHOT ERROR:", e)
        return {
            "ok": False,
            "broker": "ctrader",
            "mode": config["env"],
            "account_id": account_id,
            "account_number": (
                saved_account.get("account_number")
                or saved_account.get("traderLogin")
            ),
            "reason": str(e),
            "balance": None,
            "equity": None,
            "balance_verified": False,
            "equity_verified": False,
            "cached_balance_used": False,
            "cached_balance": saved_account.get("balance"),
            "cached_equity": saved_account.get("equity"),
            "account_list_last_refresh": settings.get("last_refresh"),
        }
    finally:
        try:
            sock.close()
        except Exception:
            pass

def get_symbol_risk_fallback(symbol):
    normalized = normalize_symbol(symbol)

    if normalized == "XAUUSD":
        return {
            "pip_size": 0.01,
            "pip_value_per_lot": 1.0,
            "lot_size": 100,
            "tick_size": 0.01,
            "tick_value": 1.0,
            "pip_position": 2,
            "volume_step_units": 1,
            "min_volume_units": 1,
            "max_volume_units": 100,
            "volume_step_lots": 0.01,
            "min_lot": 0.01,
            "max_lot": 1.0,
            "metadata_source": "fallback",
        }

    return {
        "pip_size": 0.0001,
        "pip_value_per_lot": 10.0,
        "lot_size": 100000,
        "tick_size": 0.00001,
        "tick_value": 1.0,
        "pip_position": 4,
        "volume_step_units": 1000,
        "min_volume_units": 1000,
        "max_volume_units": 10000000,
        "volume_step_lots": 0.01,
        "min_lot": 0.01,
        "max_lot": 100.0,
        "metadata_source": "fallback",
    }

def safe_float(value, fallback=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback

def get_pip_size_from_position(position):
    try:
        numeric = int(position)
    except (TypeError, ValueError):
        return None

    if numeric < 0:
        return None

    return 10 ** (-numeric)

def normalize_risk_pip_metadata(symbol, pip_size, pip_value, fallback):
    normalized = normalize_symbol(symbol)
    original_pip_size = pip_size
    original_pip_value = pip_value

    try:
        pip_size_value = float(pip_size)
    except (TypeError, ValueError):
        pip_size_value = fallback["pip_size"]

    try:
        pip_value_value = float(pip_value)
    except (TypeError, ValueError):
        pip_value_value = fallback["pip_value_per_lot"]

    if normalized == "XAUUSD" and pip_size_value < 0.01:
        print("CTRADER_XAUUSD_PIP_SIZE_NORMALIZED:", {
            "symbol": normalized,
            "raw_pip_size": original_pip_size,
            "raw_pip_value": original_pip_value,
            "risk_pip_size": fallback["pip_size"],
            "risk_pip_value_per_lot": fallback["pip_value_per_lot"],
            "reason": "Use NathauxFX gold risk pip = 0.01 instead of cTrader fractional tick pip"
        })

        return fallback["pip_size"], fallback["pip_value_per_lot"]

    if pip_size_value <= 0:
        pip_size_value = fallback["pip_size"]

    if pip_value_value <= 0 or pip_value_value > 1000:
        pip_value_value = fallback["pip_value_per_lot"]

    return pip_size_value, pip_value_value

def get_ctrader_symbol_risk_metadata(symbol):
    normalized_symbol = normalize_symbol(symbol)
    fallback = get_symbol_risk_fallback(normalized_symbol)
    config = get_ctrader_config()

    if not config:
        return {
            "ok": False,
            "symbol": normalized_symbol,
            "reason": "Missing or invalid cTrader config",
            **fallback,
        }

    account_id = int(config["account_id"])
    host, port = CTRADER_JSON_ENDPOINTS[config["env"]]
    sock = open_ctrader_json_socket(host, port)

    try:
        authorize_ctrader_socket(sock, config, account_id)
        symbol_details = fetch_ctrader_symbol_details(sock, account_id)
        symbol_info = resolve_ctrader_symbol(symbol_details, normalized_symbol)

        if not symbol_info:
            raise RuntimeError(f"cTrader symbol not found: {normalized_symbol}")

        raw = symbol_info.get("raw", {})
        pip_position = raw.get("pipPosition") or raw.get("pip_position")
        pip_size = (
            safe_float(raw.get("pipSize") or raw.get("pip_size"))
            or get_pip_size_from_position(pip_position)
            or fallback["pip_size"]
        )
        tick_size = (
            safe_float(raw.get("tickSize") or raw.get("tick_size"))
            or fallback["tick_size"]
        )
        pip_value = safe_float(
            raw.get("pipValue") or raw.get("pip_value")
        )
        tick_value = safe_float(
            raw.get("tickValue") or raw.get("tick_value")
        )
        lot_size = raw.get("lotSize") or fallback["lot_size"]

        try:
            lot_size = float(lot_size)
        except (TypeError, ValueError):
            lot_size = fallback["lot_size"]

        if lot_size <= 0:
            lot_size = fallback["lot_size"]

        step_units = int(
            safe_float(
                raw.get("stepVolume") or raw.get("volumeStep"),
                fallback["volume_step_units"]
            )
        )
        min_units = int(
            safe_float(raw.get("minVolume"), fallback["min_volume_units"])
        )
        max_units = int(
            safe_float(raw.get("maxVolume"), fallback["max_volume_units"])
        )

        if step_units <= 0:
            step_units = fallback["volume_step_units"]

        if min_units <= 0:
            min_units = fallback["min_volume_units"]

        if max_units <= 0:
            max_units = fallback["max_volume_units"]

        step_lots = (
            convert_ctrader_volume_to_lots(step_units, lot_size)
            or fallback["volume_step_lots"]
        )
        min_lot = (
            convert_ctrader_volume_to_lots(min_units, lot_size)
            or fallback["min_lot"]
        )
        max_lot = (
            convert_ctrader_volume_to_lots(max_units, lot_size)
            or fallback["max_lot"]
        )

        if (not pip_value or pip_value <= 0) and tick_value and tick_size:
            pip_value = tick_value * (float(pip_size) / float(tick_size))

        pip_size, pip_value = normalize_risk_pip_metadata(
            normalized_symbol,
            pip_size,
            pip_value,
            fallback
        )

        return {
            "ok": True,
            "symbol": normalized_symbol,
            "symbol_id": symbol_info.get("symbol_id"),
            "pip_size": float(pip_size),
            "pip_value_per_lot": pip_value,
            "lot_size": float(lot_size),
            "tick_size": float(tick_size),
            "tick_value": tick_value,
            "pip_position": pip_position,
            "volume_step_units": int(step_units),
            "min_volume_units": int(min_units),
            "max_volume_units": int(max_units),
            "volume_step_lots": float(step_lots),
            "min_lot": float(min_lot),
            "max_lot": float(max_lot),
            "metadata_source": "ctrader",
            "raw": {
                key: raw.get(key)
                for key in [
                    "pipSize",
                    "pipValue",
                    "pipPosition",
                    "tickSize",
                    "tickValue",
                    "stepVolume",
                    "minVolume",
                    "maxVolume",
                    "lotSize",
                ]
                if key in raw
            },
        }
    except Exception as e:
        print("CTRADER SYMBOL RISK METADATA ERROR:", e)
        return {
            "ok": False,
            "symbol": normalized_symbol,
            "reason": str(e),
            **fallback,
        }
    finally:
        try:
            sock.close()
        except Exception:
            pass


def get_open_positions():
    """
    Read-only cTrader position sync hook.
    Local debug mock positions are used first. Real broker sync only runs
    when CTRADER_* environment credentials are present and CTRADER_ENV is demo or live.
    This function never places or closes broker orders.
    """
    global LAST_CTRADER_POSITION_FETCH_ERROR

    try:
        if DEBUG_OPEN_POSITIONS is not None:
            LAST_CTRADER_POSITION_FETCH_ERROR = None
            return normalize_positions(DEBUG_OPEN_POSITIONS)

        config = get_ctrader_config()

        if not config:
            print("CTRADER CONFIG MISSING")
            LAST_CTRADER_POSITION_FETCH_ERROR = "Missing or invalid cTrader config"
            return []

        raw_positions = fetch_ctrader_open_positions(config)
        LAST_CTRADER_POSITION_FETCH_ERROR = None

        return normalize_positions(raw_positions)

    except Exception as e:
        print("CTRADER POSITIONS FETCH ERROR:", e)
        LAST_CTRADER_POSITION_FETCH_ERROR = str(e)
        return []

def get_ctrader_position_fetch_error():
    return LAST_CTRADER_POSITION_FETCH_ERROR

def get_closed_deals_for_current_week(max_rows=100):
    config = get_ctrader_config()

    if not config:
        print("CTRADER CLOSED DEALS FETCH SKIPPED: missing config")
        return []

    market_tz = ZoneInfo("America/New_York")
    now = datetime.now(timezone.utc)
    now_local = now.astimezone(market_tz)
    week_start = now_local.replace(hour=17, minute=0, second=0, microsecond=0)
    days_since_sunday = (now_local.weekday() - 6) % 7
    week_start = week_start - timedelta(days=days_since_sunday)

    if now_local.weekday() == 6 and now_local < week_start:
        week_start = week_start - timedelta(days=7)

    try:
        return fetch_ctrader_closed_deals(
            config,
            int(week_start.astimezone(timezone.utc).timestamp() * 1000),
            int(now.timestamp() * 1000),
            max_rows=max_rows,
        )
    except Exception as e:
        print("CTRADER CLOSED DEALS FETCH ERROR:", e)
        return []

def get_closed_deals_for_current_month(max_rows=500):
    config = get_ctrader_config()

    if not config:
        print("CTRADER MONTHLY CLOSED DEALS FETCH SKIPPED: missing config")
        return []

    market_tz = ZoneInfo("America/New_York")
    now = datetime.now(timezone.utc)
    now_local = now.astimezone(market_tz)
    month_start = now_local.replace(
        day=1,
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )

    try:
        return fetch_ctrader_closed_deals(
            config,
            int(month_start.astimezone(timezone.utc).timestamp() * 1000),
            int(now.timestamp() * 1000),
            max_rows=max_rows,
        )
    except Exception as e:
        print("CTRADER MONTHLY CLOSED DEALS FETCH ERROR:", e)
        return []

def normalize_ctrader_closed_deal(deal, symbol_map):
    if not isinstance(deal, dict):
        return None

    close_detail = deal.get("closePositionDetail") or {}

    if not isinstance(close_detail, dict) or not close_detail:
        return None

    symbol = (
        symbol_map.get(str(deal.get("symbolId")))
        or deal.get("symbol")
        or deal.get("symbolName")
    )
    normalized_symbol = normalize_symbol(symbol)

    if normalized_symbol not in ["EURUSD", "XAUUSD"]:
        return None

    money_digits = (
        close_detail.get("moneyDigits")
        or deal.get("moneyDigits")
        or 2
    )
    gross_profit = parse_ctrader_money(close_detail.get("grossProfit"), money_digits) or 0
    swap = parse_ctrader_money(close_detail.get("swap"), money_digits) or 0
    commission = parse_ctrader_money(close_detail.get("commission"), money_digits) or 0
    conversion_fee = parse_ctrader_money(close_detail.get("pnlConversionFee"), money_digits) or 0
    net_profit = gross_profit + swap + commission + conversion_fee
    result = "WIN" if net_profit > 0 else "LOSS" if net_profit < 0 else "BROKER_CLOSED"
    closed_at_ms = (
        deal.get("executionTimestamp")
        or deal.get("utcLastUpdateTimestamp")
        or deal.get("createTimestamp")
    )
    closed_at = (closed_at_ms / 1000) if closed_at_ms else time.time()
    side = normalize_trade_side(deal.get("tradeSide"))
    volume = deal.get("filledVolume") or deal.get("volume") or close_detail.get("closedVolume")
    tp1 = (
        deal.get("tp1")
        or deal.get("takeProfit")
        or close_detail.get("tp1")
        or close_detail.get("takeProfit")
    )
    tp2 = deal.get("tp2") or close_detail.get("tp2")

    return {
        "trade_id": f"ctrader-deal-{deal.get('dealId')}",
        "deal_id": deal.get("dealId"),
        "order_id": deal.get("orderId"),
        "position_id": deal.get("positionId"),
        "broker_position_id": deal.get("positionId"),
        "symbol": normalized_symbol,
        "side": side,
        "status": result,
        "result": result,
        "entry": close_detail.get("entryPrice"),
        "tp1": tp1,
        "tp2": tp2,
        "close_price": deal.get("executionPrice"),
        "current_price": deal.get("executionPrice"),
        "volume_units": volume,
        "volume": volume,
        "pnl": round(net_profit, 2),
        "profit": round(net_profit, 2),
        "broker_pnl": round(net_profit, 2),
        "broker_realized_profit": round(net_profit, 2),
        "broker_realized_source": "ctrader.closePositionDetail",
        "closed_at": closed_at,
        "opened_at": deal.get("createTimestamp") or closed_at_ms or closed_at,
        "source": "broker",
        "history_source": "ctrader_deal_list",
        "note": "Closed in cTrader",
        "raw": deal,
    }

def fetch_ctrader_closed_deals(config, from_timestamp, to_timestamp, max_rows=100):
    host, port = CTRADER_JSON_ENDPOINTS[config["env"]]
    account_id = int(config["account_id"])
    sock = open_ctrader_json_socket(host, port)

    try:
        authorize_ctrader_socket(sock, config, account_id)
        symbol_map = fetch_ctrader_symbol_map(sock, account_id)
        payload = {
            "ctidTraderAccountId": account_id,
            "fromTimestamp": int(from_timestamp),
            "toTimestamp": int(to_timestamp),
            "maxRows": int(max_rows),
        }
        response = send_ctrader_request(
            sock,
            PAYLOAD_DEAL_LIST_REQ,
            payload,
            PAYLOAD_DEAL_LIST_RES,
        )
        deals = response.get("payload", {}).get("deal", [])
        closed = [
            normalized
            for normalized in (
                normalize_ctrader_closed_deal(deal, symbol_map)
                for deal in deals
            )
            if normalized
        ]

        closed.sort(key=lambda item: item.get("closed_at") or 0, reverse=True)
        print("CTRADER_CLOSED_DEALS_SYNC =", {
            "from_timestamp": from_timestamp,
            "to_timestamp": to_timestamp,
            "raw_deals": len(deals),
            "closed_trades": len(closed),
            "realized_pl": round(sum(item.get("broker_realized_profit") or 0 for item in closed), 2),
        })

        return closed
    except Exception as e:
        print("CTRADER_CLOSED_DEALS_FETCH_ERROR:", e)
        return []
    finally:
        try:
            sock.close()
        except Exception:
            pass


def _ctrader_reconciliation_record(raw, record_type, account_id, symbol_map):
    """Normalize read-only position/order/deal evidence for durable matching."""
    if not isinstance(raw, dict):
        return None
    trade_data = raw.get("tradeData") or raw
    symbol_id = trade_data.get("symbolId") or raw.get("symbolId")
    symbol = symbol_map.get(str(symbol_id)) or raw.get("symbol") or raw.get("symbolName")
    side = normalize_trade_side(trade_data.get("tradeSide") or raw.get("tradeSide"))
    return {
        "record_type": record_type,
        "account_id": str(account_id),
        "symbol": normalize_symbol(symbol),
        "direction": side,
        "position_id": raw.get("positionId"),
        "order_id": raw.get("orderId"),
        "deal_id": raw.get("dealId"),
        "timestamp": (
            trade_data.get("openTimestamp")
            or raw.get("createTimestamp")
            or raw.get("executionTimestamp")
            or raw.get("utcLastUpdateTimestamp")
        ),
        "raw": raw,
    }


def fetch_ctrader_reconciliation_records(account_id, claimed_at=None):
    """Read open positions plus historical orders/deals for exactly one account.

    This function sends only account/authentication and read-only list requests.
    Any partial response or exception is reported as incomplete, never as proof
    that an order does not exist.
    """
    config = get_ctrader_config()
    if not config:
        return {
            "ok": False,
            "complete": False,
            "reason": "Missing or invalid cTrader config",
            "records": [],
        }
    requested_account = str(account_id)
    account_env = None
    if requested_account == str(config.get("account_id")):
        account_env = config.get("env")
    else:
        settings = load_ctrader_account_settings()
        account_env = next((
            get_ctrader_env_for_account_item(item)
            for item in settings.get("accounts", [])
            if str(item.get("account_id") or item.get("ctidTraderAccountId"))
            == requested_account
        ), None)
    if account_env not in CTRADER_JSON_ENDPOINTS:
        return {
            "ok": False,
            "complete": False,
            "reason": "cTrader account environment is unavailable",
            "records": [],
        }
    scoped_config = {
        **config,
        "account_id": requested_account,
        "env": account_env,
    }
    host, port = CTRADER_JSON_ENDPOINTS[scoped_config["env"]]
    sock = None
    try:
        numeric_account_id = int(account_id)
        sock = open_ctrader_json_socket(host, port)
        authorize_ctrader_socket(sock, scoped_config, numeric_account_id)
        symbol_map = fetch_ctrader_symbol_map(sock, numeric_account_id)
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        if claimed_at is None:
            from_ms = now_ms - (60 * 24 * 60 * 60 * 1000)
        else:
            parsed_claim = claimed_at
            if not isinstance(parsed_claim, datetime):
                parsed_claim = datetime.fromisoformat(str(parsed_claim).replace("Z", "+00:00"))
            if parsed_claim.tzinfo is None:
                parsed_claim = parsed_claim.replace(tzinfo=timezone.utc)
            from_ms = int(parsed_claim.timestamp() * 1000) - (5 * 60 * 1000)

        reconcile = send_ctrader_request(
            sock,
            PAYLOAD_RECONCILE_REQ,
            {"ctidTraderAccountId": numeric_account_id, "returnProtectionOrders": False},
            PAYLOAD_RECONCILE_RES,
        ).get("payload", {})
        orders_payload = send_ctrader_request(
            sock,
            PAYLOAD_ORDER_LIST_REQ,
            {
                "ctidTraderAccountId": numeric_account_id,
                "fromTimestamp": from_ms,
                "toTimestamp": now_ms,
            },
            PAYLOAD_ORDER_LIST_RES,
        ).get("payload", {})
        deals_payload = send_ctrader_request(
            sock,
            PAYLOAD_DEAL_LIST_REQ,
            {
                "ctidTraderAccountId": numeric_account_id,
                "fromTimestamp": from_ms,
                "toTimestamp": now_ms,
                "maxRows": 1000,
            },
            PAYLOAD_DEAL_LIST_RES,
        ).get("payload", {})
        if orders_payload.get("hasMore") or deals_payload.get("hasMore"):
            return {
                "ok": False,
                "complete": False,
                "reason": "cTrader reconciliation history was truncated",
                "records": [],
            }

        records = []
        for record_type, rows in (
            ("position", reconcile.get("position") or []),
            ("order", orders_payload.get("order") or []),
            ("deal", deals_payload.get("deal") or []),
        ):
            for raw in rows:
                normalized = _ctrader_reconciliation_record(
                    raw, record_type, numeric_account_id, symbol_map
                )
                if normalized:
                    records.append(normalized)
        return {"ok": True, "complete": True, "records": records}
    except Exception as exc:
        return {
            "ok": False,
            "complete": False,
            "reason": str(exc),
            "records": [],
        }
    finally:
        try:
            if sock is not None:
                sock.close()
        except Exception:
            pass

def get_ctrader_config():
    hydrate_ctrader_tokens_from_storage()
    active_account_id = get_active_ctrader_account_id()
    active_account_env = get_active_ctrader_account_env()
    config = {
        "client_id": os.getenv("CTRADER_CLIENT_ID"),
        "client_secret": os.getenv("CTRADER_CLIENT_SECRET"),
        "access_token": os.getenv("CTRADER_ACCESS_TOKEN"),
        "refresh_token": os.getenv("CTRADER_REFRESH_TOKEN"),
        "account_id": active_account_id,
        "env": active_account_env,
    }

    missing = [
        key for key in [
            "client_id",
            "client_secret",
            "access_token",
            "account_id",
        ]
        if not config.get(key)
    ]

    if missing:
        return None

    if config["env"] not in CTRADER_JSON_ENDPOINTS:
        print("CTRADER CONFIG MISSING: active account env must be demo or live")
        return None

    return config

def get_ctrader_refresh_token_status():
    hydration = hydrate_ctrader_tokens_from_storage()
    return {
        "has_refresh_token": bool(os.getenv("CTRADER_REFRESH_TOKEN")),
        "durable_token_loaded": bool(hydration.get("loaded")),
        "source": hydration.get("source"),
    }

def update_env_file_values(values):
    if not ENV_PATH.exists():
        return

    lines = ENV_PATH.read_text().splitlines()
    seen = set()
    updated_lines = []

    for line in lines:
        stripped = line.strip()

        if not stripped or stripped.startswith("#") or "=" not in line:
            updated_lines.append(line)
            continue

        key = line.split("=", 1)[0].strip()

        if key in values:
            updated_lines.append(f"{key}={values[key]}")
            seen.add(key)
        else:
            updated_lines.append(line)

    for key, value in values.items():
        if key not in seen:
            updated_lines.append(f"{key}={value}")

    ENV_PATH.write_text("\n".join(updated_lines) + "\n")

def refresh_ctrader_access_token(config):
    refresh_token = config.get("refresh_token") or os.getenv("CTRADER_REFRESH_TOKEN")

    if not refresh_token:
        print("CTRADER_TOKEN_REFRESH =", {
            "ok": False,
            "reason": "CTRADER_REFRESH_TOKEN missing",
            "access_token_updated": False,
            "refresh_token_updated": False,
        })
        raise RuntimeError("CTRADER_REFRESH_TOKEN missing; cannot refresh cTrader access token")

    try:
        response = requests.post(
            CTRADER_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": config["client_id"],
                "client_secret": config["client_secret"],
            },
            timeout=12,
        )
    except Exception as e:
        print("CTRADER_TOKEN_REFRESH =", {
            "ok": False,
            "reason": f"request failed: {e}",
            "access_token_updated": False,
            "refresh_token_updated": False,
        })
        raise

    try:
        payload = response.json()
    except Exception:
        payload = {}

    if not response.ok:
        reason = f"HTTP {response.status_code}: {payload.get('error') or payload.get('message') or 'refresh failed'}"
        print("CTRADER_TOKEN_REFRESH =", {
            "ok": False,
            "reason": reason,
            "access_token_updated": False,
            "refresh_token_updated": False,
        })
        raise RuntimeError(
            f"cTrader refresh token failed: HTTP {response.status_code} "
            f"{payload.get('error') or payload.get('message') or 'refresh failed'}"
        )

    access_token = payload.get("accessToken") or payload.get("access_token")
    new_refresh_token = payload.get("refreshToken") or payload.get("refresh_token")

    if not access_token:
        print("CTRADER_TOKEN_REFRESH =", {
            "ok": False,
            "reason": "refresh response missing accessToken",
            "access_token_updated": False,
            "refresh_token_updated": False,
        })
        raise RuntimeError("cTrader refresh token response missing accessToken")

    if not new_refresh_token:
        new_refresh_token = refresh_token

    os.environ["CTRADER_ACCESS_TOKEN"] = access_token
    os.environ["CTRADER_REFRESH_TOKEN"] = new_refresh_token
    update_env_file_values({
        "CTRADER_ACCESS_TOKEN": access_token,
        "CTRADER_REFRESH_TOKEN": new_refresh_token,
    })
    durable_saved = persist_ctrader_tokens(
        access_token,
        new_refresh_token,
        updated_by="token_refresh",
    )
    config["access_token"] = access_token
    config["refresh_token"] = new_refresh_token

    print("CTRADER_TOKEN_REFRESH =", {
        "ok": True,
        "reason": "refreshed",
        "access_token_updated": True,
        "refresh_token_updated": bool(new_refresh_token),
        "durable_token_saved": durable_saved,
    })

    return config

def is_ctrader_access_token_error(error):
    data = getattr(error, "data", {}) or {}
    payload = data.get("payload", {}) if isinstance(data, dict) else {}
    error_code = (
        payload.get("errorCode")
        or payload.get("error_code")
        or data.get("errorCode")
        or data.get("error_code")
    )

    if error_code and "ACCESS_TOKEN" in str(error_code).upper():
        return True

    text = str(error or "")

    return (
        "CH_ACCESS_TOKEN_INVALID" in text
        or "ACCESS_TOKEN_INVALID" in text
        or "access token" in text.lower()
        and "invalid" in text.lower()
    )

def fetch_ctrader_trendbars(config, symbol, period, limit):
    host, port = CTRADER_JSON_ENDPOINTS[config["env"]]
    account_id = int(config["account_id"])
    requested_symbol = normalize_symbol(symbol)

    sock = open_ctrader_json_socket(host, port)

    try:
        authorize_ctrader_socket(sock, config, account_id)

        symbol_details = fetch_ctrader_symbol_details(sock, account_id)
        symbol_info = resolve_ctrader_symbol(symbol_details, requested_symbol)

        if not symbol_info:
            raise RuntimeError(f"cTrader symbol not found: {requested_symbol}")

        symbol_id = int(symbol_info["symbol_id"])
        digits = int(symbol_info["digits"])
        print("CTRADER SYMBOL FOUND:", requested_symbol, symbol_id)

        to_timestamp = int(datetime.now(timezone.utc).timestamp() * 1000)
        period_minutes = CTRADER_TRENDBAR_PERIOD_MINUTES.get(period)

        if not period_minutes:
            raise RuntimeError(f"Unsupported cTrader trendbar period enum: {period}")

        from_timestamp = max(
            0,
            to_timestamp - int(limit) * period_minutes * 60 * 1000
        )
        trendbar_payload = {
            "ctidTraderAccountId": account_id,
            "symbolId": symbol_id,
            "period": period,
            "fromTimestamp": from_timestamp,
            "toTimestamp": to_timestamp,
            "count": int(limit),
        }

        print("CTRADER TRENDBAR REQUEST:", trendbar_payload)

        response = send_ctrader_request(
            sock,
            PAYLOAD_GET_TRENDBARS_REQ,
            trendbar_payload,
            PAYLOAD_GET_TRENDBARS_RES,
        )

        trendbars = response.get("payload", {}).get("trendbar", [])

        for trendbar in trendbars:
            if isinstance(trendbar, dict):
                trendbar["digits"] = digits

        return normalize_ctrader_candles(trendbars, requested_symbol)

    finally:
        try:
            sock.close()
        except Exception:
            pass


def fetch_ctrader_historical_candles(symbol, timeframe, start_utc, end_utc):
    """Fetch native cTrader trendbars for a bounded historical UTC range.

    This is a market-data-only helper. It intentionally does not read or write
    the live candle cache, persist candles, or touch any order/position API.
    """
    requested_symbol = normalize_symbol(symbol)
    normalized_timeframe = str(timeframe or "").strip().lower()
    period = CTRADER_TRENDBAR_PERIODS.get(normalized_timeframe)
    period_minutes = CTRADER_TRENDBAR_PERIOD_MINUTES.get(period)
    if period is None or period_minutes is None:
        raise ValueError(f"Unsupported cTrader candle timeframe: {timeframe}")

    start = pd.Timestamp(start_utc)
    end = pd.Timestamp(end_utc)
    start = start.tz_localize("UTC") if start.tzinfo is None else start.tz_convert("UTC")
    end = end.tz_localize("UTC") if end.tzinfo is None else end.tz_convert("UTC")
    if end <= start:
        raise ValueError("Historical candle end must be after start")

    config = get_ctrader_config()
    if not config:
        raise RuntimeError("Missing or invalid cTrader config for historical candle fetch")

    host, port = CTRADER_JSON_ENDPOINTS[config["env"]]
    account_id = int(config["account_id"])
    sock = open_ctrader_json_socket(host, port)
    try:
        authorize_ctrader_socket(sock, config, account_id)
        symbol_details = fetch_ctrader_symbol_details(sock, account_id)
        symbol_info = resolve_ctrader_symbol(symbol_details, requested_symbol)
        if not symbol_info:
            raise RuntimeError(f"cTrader symbol not found: {requested_symbol}")

        symbol_id = int(symbol_info["symbol_id"])
        digits = int(symbol_info["digits"])
        # cTrader can truncate a large trendbar response even when ``count`` is
        # higher. Page the read-only history request so a two-month chart does
        # not silently collapse back to only the most recent few days.
        page_candles = 900
        page_span = timedelta(minutes=period_minutes * page_candles)
        cursor = start
        frames = []
        while cursor < end:
            page_end = min(cursor + page_span, end)
            expected_count = int(
                (page_end - cursor).total_seconds() // (period_minutes * 60)
            ) + 2
            payload = {
                "ctidTraderAccountId": account_id,
                "symbolId": symbol_id,
                "period": period,
                "fromTimestamp": int(cursor.timestamp() * 1000),
                "toTimestamp": int(page_end.timestamp() * 1000),
                "count": min(max(expected_count, 1), page_candles + 2),
            }
            response = send_ctrader_request(
                sock,
                PAYLOAD_GET_TRENDBARS_REQ,
                payload,
                PAYLOAD_GET_TRENDBARS_RES,
            )
            trendbars = response.get("payload", {}).get("trendbar", [])
            for trendbar in trendbars:
                if isinstance(trendbar, dict):
                    trendbar["digits"] = digits
            frame = normalize_ctrader_candles(trendbars, requested_symbol)
            if frame is not None and not frame.empty:
                frames.append(frame)
            cursor = page_end

        if not frames:
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
        combined = pd.concat(frames)
        combined = combined[~combined.index.duplicated(keep="last")].sort_index()
        return combined[(combined.index >= start) & (combined.index <= end)]
    finally:
        try:
            sock.close()
        except Exception:
            pass

def extract_ctrader_account_id(response):
    payload = response.get("payload", {}) if isinstance(response, dict) else {}

    if not isinstance(response, dict):
        return None

    return (
        payload.get("ctidTraderAccountId")
        or payload.get("ctid_trader_account_id")
        or response.get("ctidTraderAccountId")
        or response.get("ctid_trader_account_id")
    )

def extract_ctrader_authorized_account_ids(response):
    payload = response.get("payload", {}) if isinstance(response, dict) else {}
    candidates = (
        payload.get("ctidTraderAccount")
        or payload.get("ctid_trader_account")
        or payload.get("ctidTraderAccountId")
        or payload.get("ctid_trader_account_id")
        or response.get("ctidTraderAccount")
        or response.get("ctidTraderAccountId")
        or []
    )

    if isinstance(candidates, (str, int)):
        candidates = [candidates]

    account_ids = []

    for item in candidates:
        if isinstance(item, dict):
            value = (
                item.get("ctidTraderAccountId")
                or item.get("ctid_trader_account_id")
                or item.get("accountId")
            )
        else:
            value = item

        if value is None:
            continue

        account_ids.append(str(value))

    return account_ids

def authorize_ctrader_socket(sock, config, account_id, retry_refresh=True):
    send_ctrader_request(
        sock,
        PAYLOAD_APPLICATION_AUTH_REQ,
        {
            "clientId": config["client_id"],
            "clientSecret": config["client_secret"],
        },
        PAYLOAD_APPLICATION_AUTH_RES,
    )

    print("CTRADER APP AUTH OK")

    try:
        return authorize_ctrader_account(sock, config, account_id)
    except Exception as e:
        if retry_refresh and is_ctrader_access_token_error(e):
            print("CTRADER ACCESS TOKEN INVALID: refreshing and retrying account auth once")
            refresh_ctrader_access_token(config)
            return authorize_ctrader_account(sock, config, account_id)

        raise

def authorize_ctrader_account(sock, config, account_id):
    account_list_response = send_ctrader_request(
        sock,
        PAYLOAD_GET_ACCOUNT_LIST_BY_ACCESS_TOKEN_REQ,
        {
            "accessToken": config["access_token"],
        },
        PAYLOAD_GET_ACCOUNT_LIST_BY_ACCESS_TOKEN_RES,
    )
    authorized_account_ids = extract_ctrader_authorized_account_ids(
        account_list_response
    )

    print("CTRADER_ACCOUNT_LIST_DEBUG =", {
        "env": config.get("env"),
        "requested_account_id": str(account_id),
        "account_ids": authorized_account_ids,
        "active_account_id": str(account_id),
        "authorized_account_ids": authorized_account_ids,
        "selected_account_source": get_selected_ctrader_account_source(),
        "is_active_account_authorized": str(account_id) in authorized_account_ids,
    })
    update_ctrader_account_auth_selection_debug(
        str(account_id),
        authorized_account_ids,
    )

    if not authorized_account_ids:
        print("CTRADER_ACCOUNT_AUTH_DEBUG =", {
            "ok": False,
            "account_id": str(account_id),
            "reason": "No cTrader accounts returned for access token",
        })
        raise RuntimeError("No cTrader accounts returned for access token")

    configured_account_id_found = str(account_id) in authorized_account_ids

    if not configured_account_id_found:
        if str(get_active_ctrader_account_id()) == str(account_id):
            clear_active_ctrader_account_selection(
                CTRADER_UNAUTHORIZED_ACCOUNT_MESSAGE,
                authorized_account_ids,
            )
        print("CTRADER_ACCOUNT_AUTH_DEBUG =", {
            "ok": False,
            "account_id": str(account_id),
            "reason": CTRADER_UNAUTHORIZED_ACCOUNT_MESSAGE,
            "authorized_account_ids": authorized_account_ids,
        })
        raise RuntimeError(CTRADER_UNAUTHORIZED_ACCOUNT_MESSAGE)

    account_response = send_ctrader_request(
        sock,
        PAYLOAD_ACCOUNT_AUTH_REQ,
        {
            "ctidTraderAccountId": account_id,
            "accessToken": config["access_token"],
        },
        PAYLOAD_ACCOUNT_AUTH_RES,
    )
    authorized_account_id = extract_ctrader_account_id(account_response)

    if str(authorized_account_id) != str(account_id):
        print("CTRADER_ACCOUNT_AUTH_DEBUG =", {
            "ok": False,
            "account_id": str(account_id),
            "authorized_account_id": str(authorized_account_id),
            "reason": "cTrader returned a different account id",
        })
        raise RuntimeError("cTrader account authorization failed")

    print("CTRADER ACCOUNT AUTH OK:", account_id)
    print("CTRADER_ACCOUNT_AUTH_DEBUG =", {
        "ok": True,
        "account_id": str(account_id),
        "authorized_account_ids": authorized_account_ids,
        "selected_account_source": get_selected_ctrader_account_source(),
        "is_active_account_authorized": True,
    })

    return {
        "account_response": account_response,
        "authorized_account_ids": authorized_account_ids,
        "configured_account_id_found": configured_account_id_found,
    }

def verify_ctrader_account_auth(account_id, config=None):
    account_id_text = str(account_id or "").strip()

    if not account_id_text:
        return {
            "ok": False,
            "reason": "Missing cTrader account id",
        }

    try:
        account_id_int = int(account_id_text)
    except Exception:
        return {
            "ok": False,
            "reason": "Invalid cTrader account id",
            "account_id": account_id_text,
        }

    config = dict(config or {})
    config.setdefault("client_id", os.getenv("CTRADER_CLIENT_ID"))
    config.setdefault("client_secret", os.getenv("CTRADER_CLIENT_SECRET"))
    config.setdefault("access_token", os.getenv("CTRADER_ACCESS_TOKEN"))
    config.setdefault("refresh_token", os.getenv("CTRADER_REFRESH_TOKEN"))
    config.setdefault("env", get_active_ctrader_account_env())
    config["env"] = normalize_ctrader_env(config.get("env"))
    config["account_id"] = account_id_text

    missing = [
        key for key in ["client_id", "client_secret", "access_token"]
        if not config.get(key)
    ]

    if missing:
        return {
            "ok": False,
            "reason": f"Missing cTrader authorization: {', '.join(missing)}",
            "account_id": account_id_text,
        }

    if config["env"] not in CTRADER_JSON_ENDPOINTS:
        return {
            "ok": False,
            "reason": "CTRADER_ENV must be demo or live",
            "account_id": account_id_text,
        }

    host, port = CTRADER_JSON_ENDPOINTS[config["env"]]
    sock = open_ctrader_json_socket(host, port)

    try:
        send_ctrader_request(
            sock,
            PAYLOAD_APPLICATION_AUTH_REQ,
            {
                "clientId": config["client_id"],
                "clientSecret": config["client_secret"],
            },
            PAYLOAD_APPLICATION_AUTH_RES,
        )
        auth_info = authorize_ctrader_account(sock, config, account_id_int)

        return {
            "ok": True,
            "account_id": account_id_text,
            **auth_info,
        }
    except Exception as exc:
        reason = describe_ctrader_error(exc)
        print("CTRADER_ACCOUNT_AUTH_DEBUG =", {
            "ok": False,
            "account_id": account_id_text,
            "reason": reason,
        })

        return {
            "ok": False,
            "reason": reason,
            "account_id": account_id_text,
            **get_ctrader_account_selection_debug(),
        }
    finally:
        try:
            sock.close()
        except Exception:
            pass

def extract_ctrader_account_display(
    account_id,
    response=None,
    error=None,
    env=None,
    active_account_id=None,
    account_list_item=None,
):
    payload = response.get("payload", {}) if isinstance(response, dict) else {}
    trader = payload.get("trader") if isinstance(payload.get("trader"), dict) else payload
    trader = trader if isinstance(trader, dict) else {}
    account_list_item = account_list_item if isinstance(account_list_item, dict) else {}
    money_digits = trader.get("moneyDigits", payload.get("moneyDigits", 2))
    balance = parse_ctrader_money(
        trader.get("balance") or payload.get("balance"),
        money_digits,
    )
    equity = parse_ctrader_money(
        trader.get("equity") or payload.get("equity"),
        money_digits,
    )
    account_id_text = str(account_id)
    unavailable = error is not None
    trader_login = (
        trader.get("traderLogin")
        or trader.get("login")
        or trader.get("accountId")
        or account_list_item.get("traderLogin")
    )
    account_number = (
        trader.get("accountNumber")
        or trader.get("account_number")
        or trader_login
    )
    is_live = trader.get("isLive")

    if is_live is None:
        is_live = account_list_item.get("isLive")

    account_env = (
        "live"
        if is_live is True
        else "demo"
        if is_live is False
        else normalize_ctrader_env(env)
    )

    return {
        "account_id": account_id_text,
        "ctidTraderAccountId": account_id_text,
        "account_number": account_number,
        "trader_login": trader_login,
        "traderLogin": trader_login,
        "broker_name": (
            trader.get("brokerName")
            or trader.get("broker_name")
            or trader.get("brokerTitle")
            or account_list_item.get("brokerTitleShort")
            or account_list_item.get("brokerTitle")
            or trader.get("broker")
            or "cTrader"
        ),
        "balance": balance,
        "equity": equity,
        "currency": (
            trader.get("depositAsset")
            or trader.get("depositAssetId")
            or trader.get("currency")
            or payload.get("currency")
        ),
        "mode": account_env,
        "env": account_env,
        "isLive": is_live,
        "status": (
            "active"
            if not unavailable and active_account_id and account_id_text == str(active_account_id)
            else "unavailable"
            if unavailable
            else "available"
        ),
        "unavailable": unavailable,
        "expired": unavailable,
        "reason": str(error) if error else None,
        "money_digits": money_digits,
        "raw": {
            key: trader.get(key)
            for key in [
                "accountNumber",
                "brokerName",
                "balance",
                "equity",
                "moneyDigits",
                "depositAssetId",
                "isLive",
                "traderLogin",
                "accessRights",
            ]
            if key in trader
        } or {
            key: account_list_item.get(key)
            for key in [
                "brokerTitleShort",
                "brokerTitle",
                "traderLogin",
                "isLive",
            ]
            if key in account_list_item
        },
    }


def preserve_active_account_during_transient_refresh(
    active_account_id,
    refreshed_accounts,
    cached_accounts,
):
    accounts = list(refreshed_accounts or [])
    active_id = str(active_account_id or "").strip()
    if not active_id:
        return accounts

    cached_active_account = next(
        (
            item
            for item in (cached_accounts or [])
            if isinstance(item, dict)
            and str(item.get("account_id")) == active_id
        ),
        None,
    )
    refreshed_index = next(
        (
            index
            for index, item in enumerate(accounts)
            if isinstance(item, dict)
            and str(item.get("account_id")) == active_id
        ),
        None,
    )
    if refreshed_index is not None:
        refreshed_active = accounts[refreshed_index]
        if refreshed_active.get("unavailable") and cached_active_account:
            accounts[refreshed_index] = {
                **cached_active_account,
                **refreshed_active,
                "balance": (
                    refreshed_active.get("balance")
                    if refreshed_active.get("balance") not in (None, "")
                    else cached_active_account.get("balance")
                ),
                "equity": (
                    refreshed_active.get("equity")
                    if refreshed_active.get("equity") not in (None, "")
                    else cached_active_account.get("equity")
                ),
                "status": "unavailable",
                "unavailable": True,
                "reason": "Temporary cTrader refresh failure; active selection preserved",
            }
        return accounts

    if cached_active_account:
        accounts.append({
            **cached_active_account,
            "status": "unavailable",
            "unavailable": True,
            "reason": "Temporary cTrader refresh failure; active selection preserved",
        })
    return accounts

def fetch_ctrader_accounts(refresh=True):
    hydrate_ctrader_tokens_from_storage()
    settings = load_ctrader_account_settings()
    active_account_id = get_active_ctrader_account_id()
    if not refresh:
        cached_accounts = settings.get("accounts", [])
        return {
            "ok": True,
            "active_account_id": str(active_account_id) if active_account_id else None,
            "active_account_env": settings.get("active_account_env"),
            "accounts": cached_accounts if isinstance(cached_accounts, list) else [],
            "forgotten_account_ids": settings.get("forgotten_account_ids", []),
            "last_refresh": settings.get("last_refresh"),
            "env": settings.get("active_account_env") or os.getenv("CTRADER_ENV", "demo"),
            "cached": True,
            "source": "saved_account_state",
        }
    env = normalize_ctrader_env(os.getenv("CTRADER_ENV", "demo"))
    config = {
        "client_id": os.getenv("CTRADER_CLIENT_ID"),
        "client_secret": os.getenv("CTRADER_CLIENT_SECRET"),
        "access_token": os.getenv("CTRADER_ACCESS_TOKEN"),
        "refresh_token": os.getenv("CTRADER_REFRESH_TOKEN"),
        "env": env,
        "account_id": active_account_id,
    }

    if not config.get("client_id") or not config.get("client_secret"):
        return {
            "ok": False,
            "reason": "Missing cTrader client credentials",
            **settings,
        }

    if not config.get("access_token") and config.get("refresh_token"):
        try:
            refresh_ctrader_access_token(config)
        except Exception as exc:
            return {
                "ok": False,
                "reason": str(exc),
                **settings,
            }

    if not config.get("access_token"):
        return {
            "ok": False,
            "reason": "cTrader is disconnected. Connect cTrader first.",
            **settings,
        }

    if config["env"] not in CTRADER_JSON_ENDPOINTS:
        return {
            "ok": False,
            "reason": "CTRADER_ENV must be demo or live",
            **settings,
        }

    host, port = CTRADER_JSON_ENDPOINTS[config["env"]]
    forgotten = {
        str(value)
        for value in settings.get("forgotten_account_ids", [])
        if value is not None
    }
    sock = open_ctrader_json_socket(host, port)

    try:
        send_ctrader_request(
            sock,
            PAYLOAD_APPLICATION_AUTH_REQ,
            {
                "clientId": config["client_id"],
                "clientSecret": config["client_secret"],
            },
            PAYLOAD_APPLICATION_AUTH_RES,
        )
        try:
            account_list_response = send_ctrader_request(
                sock,
                PAYLOAD_GET_ACCOUNT_LIST_BY_ACCESS_TOKEN_REQ,
                {
                    "accessToken": config["access_token"],
                },
                PAYLOAD_GET_ACCOUNT_LIST_BY_ACCESS_TOKEN_RES,
            )
        except Exception as exc:
            if not is_ctrader_access_token_error(exc):
                raise

            try:
                sock.close()
            except Exception:
                pass

            refresh_ctrader_access_token(config)
            sock = open_ctrader_json_socket(host, port)
            send_ctrader_request(
                sock,
                PAYLOAD_APPLICATION_AUTH_REQ,
                {
                    "clientId": config["client_id"],
                    "clientSecret": config["client_secret"],
                },
                PAYLOAD_APPLICATION_AUTH_RES,
            )
            account_list_response = send_ctrader_request(
                sock,
                PAYLOAD_GET_ACCOUNT_LIST_BY_ACCESS_TOKEN_REQ,
                {
                    "accessToken": config["access_token"],
                },
                PAYLOAD_GET_ACCOUNT_LIST_BY_ACCESS_TOKEN_RES,
            )
        account_list_payload = (
            account_list_response.get("payload", {})
            if isinstance(account_list_response, dict)
            else {}
        )
        raw_account_items = (
            account_list_payload.get("ctidTraderAccount")
            or account_list_payload.get("ctid_trader_account")
            or []
        )

        if not isinstance(raw_account_items, list):
            raw_account_items = []

        account_items_by_id = {}

        for item in raw_account_items:
            if not isinstance(item, dict):
                continue

            account_item_id = (
                item.get("ctidTraderAccountId")
                or item.get("ctid_trader_account_id")
                or item.get("accountId")
            )

            if account_item_id is None:
                continue

            account_items_by_id[str(account_item_id)] = item

        account_ids = [
            str(account_id)
            for account_id in extract_ctrader_authorized_account_ids(account_list_response)
            if str(account_id) not in forgotten
        ]
        update_ctrader_account_auth_selection_debug(
            active_account_id,
            account_ids,
        )
        print("CTRADER_ACCOUNT_LIST_DEBUG =", {
            "ok": True,
            "env": config["env"],
            "active_account_id": str(active_account_id) if active_account_id else None,
            "account_ids": account_ids,
            "authorized_account_ids": account_ids,
            "selected_account_source": get_selected_ctrader_account_source(),
            "is_active_account_authorized": (
                str(active_account_id) in account_ids
                if active_account_id
                else False
            ),
            "account_envs": {
                account_id: get_ctrader_env_for_account_item(
                    account_items_by_id.get(str(account_id), {})
                )
                for account_id in account_ids
            },
            "forgotten_account_ids": sorted(forgotten),
        })
        accounts = []

        for account_id in account_ids:
            account_response = None
            trader_response = None
            account_auth_error = None
            account_item = account_items_by_id.get(str(account_id), {})
            account_env = get_ctrader_env_for_account_item(account_item)
            account_sock = None

            try:
                account_host, account_port = CTRADER_JSON_ENDPOINTS[account_env]
                account_sock = open_ctrader_json_socket(account_host, account_port)
                send_ctrader_request(
                    account_sock,
                    PAYLOAD_APPLICATION_AUTH_REQ,
                    {
                        "clientId": config["client_id"],
                        "clientSecret": config["client_secret"],
                    },
                    PAYLOAD_APPLICATION_AUTH_RES,
                )
                account_response = send_ctrader_request(
                    account_sock,
                    PAYLOAD_ACCOUNT_AUTH_REQ,
                    {
                        "ctidTraderAccountId": int(account_id),
                        "accessToken": config["access_token"],
                    },
                    PAYLOAD_ACCOUNT_AUTH_RES,
                )

                authorized_account_id = extract_ctrader_account_id(account_response)

                if str(authorized_account_id) != str(account_id):
                    raise RuntimeError("cTrader returned a different account id during accountAuth")

                print("CTRADER_ACCOUNT_AUTH_DEBUG =", {
                    "ok": True,
                    "account_id": account_id,
                    "env": account_env,
                    "source": "account_list_refresh",
                })
            except Exception as exc:
                account_auth_error = exc
                print("CTRADER_ACCOUNT_AUTH_DEBUG =", {
                    "ok": False,
                    "account_id": account_id,
                    "env": account_env,
                    "source": "account_list_refresh",
                    "reason": describe_ctrader_error(exc),
                })
                accounts.append(
                    extract_ctrader_account_display(
                        account_id,
                        error=describe_ctrader_error(exc),
                        env=account_env,
                        active_account_id=active_account_id,
                        account_list_item=account_item,
                    )
                )
                try:
                    if account_sock:
                        account_sock.close()
                except Exception:
                    pass
                continue

            try:
                trader_response = send_ctrader_request(
                    account_sock,
                    PAYLOAD_TRADER_REQ,
                    {
                        "ctidTraderAccountId": int(account_id),
                    },
                    PAYLOAD_TRADER_RES,
                )
            except Exception as exc:
                print("CTRADER_ACCOUNT_AUTH_DEBUG =", {
                    "ok": True,
                    "account_id": account_id,
                    "env": account_env,
                    "source": "trader_details",
                    "details_available": False,
                    "reason": describe_ctrader_error(exc),
                })

            if account_auth_error is None:
                accounts.append(
                    extract_ctrader_account_display(
                        account_id,
                        trader_response or account_response,
                        env=account_env,
                        active_account_id=active_account_id,
                        account_list_item=account_item,
                    )
                )

            try:
                if account_sock:
                    account_sock.close()
            except Exception:
                pass

        active_available = any(
            str(item.get("account_id")) == str(active_account_id)
            and not item.get("unavailable")
            for item in accounts
        )

        if active_account_id and not active_available:
            accounts = preserve_active_account_during_transient_refresh(
                active_account_id,
                accounts,
                settings.get("accounts", []),
            )
            print("ACTIVE_ACCOUNT_SELECTED_DEBUG =", {
                "ok": False,
                "account_id": str(active_account_id),
                "authorized_account_ids": account_ids,
                "selected_account_source": get_selected_ctrader_account_source(),
                "is_active_account_authorized": False,
                "reason": "active selection preserved while cTrader account details are unavailable",
            })

        settings["active_account_id"] = str(active_account_id) if active_account_id else None
        settings["active_account_env"] = (
            next(
                (
                    get_ctrader_env_for_account_item(item)
                    for item in accounts
                    if str(item.get("account_id")) == str(active_account_id)
                ),
                None,
            )
            if active_account_id
            else None
        )
        settings["accounts"] = accounts
        settings["last_refresh"] = datetime.now(timezone.utc).isoformat()
        save_ctrader_account_settings(settings)
        selection_debug = update_ctrader_account_auth_selection_debug(
            settings["active_account_id"],
            account_ids,
        )

        return {
            "ok": True,
            "active_account_id": settings["active_account_id"],
            "active_account_env": settings["active_account_env"],
            "active_account_id_authorized": selection_debug["is_active_account_authorized"],
            "active_account_id_source": selection_debug["selected_account_source"],
            "authorized_account_ids": account_ids,
            "active_account_id_debug": selection_debug["active_account_id"],
            "selected_account_source": selection_debug["selected_account_source"],
            "is_active_account_authorized": selection_debug["is_active_account_authorized"],
            "accounts": accounts,
            "forgotten_account_ids": sorted(forgotten),
            "last_refresh": settings["last_refresh"],
            "env": settings["active_account_env"] or config["env"],
        }
    except Exception as exc:
        return {
            "ok": False,
            "reason": str(exc),
            **settings,
        }
    finally:
        try:
            sock.close()
        except Exception:
            pass

def fetch_ctrader_symbol_details(sock, account_id):
    response = send_ctrader_request(
        sock,
        PAYLOAD_SYMBOLS_LIST_REQ,
        {
            "ctidTraderAccountId": account_id,
            "includeArchivedSymbols": False,
        },
        PAYLOAD_SYMBOLS_LIST_RES,
    )

    symbols = response.get("payload", {}).get("symbol", [])
    details = []

    for symbol in symbols:
        if not isinstance(symbol, dict) or symbol.get("symbolId") is None:
            continue

        name = (
            symbol.get("symbolName")
            or symbol.get("name")
            or symbol.get("displayName")
            or ""
        )
        digits = symbol.get("digits")

        if digits is None:
            digits = get_default_symbol_digits(name)

        details.append({
            "symbol_id": symbol.get("symbolId"),
            "name": name,
            "normalized_name": normalize_ctrader_symbol_name(name),
            "digits": digits,
            "raw": symbol,
        })

    return details

def resolve_ctrader_symbol(symbol_details, requested_symbol):
    requested_name = normalize_ctrader_symbol_name(requested_symbol)

    for symbol in symbol_details:
        if symbol.get("normalized_name") == requested_name and symbol.get("digits") is not None:
            return symbol

    if requested_name == "XAUUSD":
        broker_aliases = ["XAUUSD", "GOLD", "GOLDSPOT", "XAU"]

        for symbol in symbol_details:
            normalized_name = symbol.get("normalized_name") or ""

            if symbol.get("digits") is None:
                continue

            if any(alias in normalized_name for alias in broker_aliases):
                print("CTRADER XAUUSD SYMBOL ALIAS MATCH:", {
                    "requested": requested_symbol,
                    "matched_name": symbol.get("name"),
                    "normalized_name": normalized_name,
                    "symbol_id": symbol.get("symbol_id"),
                })
                return symbol

    print("CTRADER SYMBOL RESOLVE FAILED:", {
        "requested": requested_symbol,
        "requested_name": requested_name,
        "available_sample": [
            {
                "name": item.get("name"),
                "normalized_name": item.get("normalized_name"),
                "symbol_id": item.get("symbol_id"),
            }
            for item in symbol_details[:20]
        ],
    })

    return None

def get_ctrader_diagnostics():
    active_account_id = get_active_ctrader_account_id()
    active_account_env = get_active_ctrader_account_env()
    selection_debug = get_ctrader_account_selection_debug()
    env_config = {
        "client_id": os.getenv("CTRADER_CLIENT_ID"),
        "client_secret": os.getenv("CTRADER_CLIENT_SECRET"),
        "access_token": os.getenv("CTRADER_ACCESS_TOKEN"),
        "refresh_token": os.getenv("CTRADER_REFRESH_TOKEN"),
        "account_id": active_account_id,
        "env": active_account_env,
    }
    missing = [
        key for key in [
            "client_id",
            "client_secret",
            "access_token",
            "account_id",
        ]
        if not env_config.get(key)
    ]
    diagnostics = {
        "ok": False,
        "env": env_config["env"],
        "has_client_id": bool(env_config["client_id"]),
        "has_client_secret": bool(env_config["client_secret"]),
        "has_access_token": bool(env_config["access_token"]),
        "has_refresh_token": bool(env_config["refresh_token"]),
        "has_account_id": bool(env_config["account_id"]),
        "account_id": env_config["account_id"],
        "connection_state": get_connection_state(),
        "account_auth_ok": False,
        "authorized_account_ids": [],
        "active_account_id": selection_debug.get("active_account_id") or active_account_id,
        "selected_account_source": selection_debug.get("selected_account_source"),
        "is_active_account_authorized": selection_debug.get("is_active_account_authorized"),
        "configured_account_id_found": False,
        "can_fetch_positions": False,
        "can_fetch_candles": False,
        "candle_error": None,
        "missing": missing,
        "error": None,
    }

    config = get_ctrader_config()

    if missing:
        diagnostics["error"] = f"Missing cTrader env values: {', '.join(missing)}"
        return diagnostics

    if not config:
        diagnostics["error"] = "Invalid cTrader config. CTRADER_ENV must be demo or live."
        return diagnostics

    try:
        host, port = CTRADER_JSON_ENDPOINTS[config["env"]]
        account_id = int(config["account_id"])
        sock = open_ctrader_json_socket(host, port)

        try:
            auth_state = authorize_ctrader_socket(sock, config, account_id)
            diagnostics["account_auth_ok"] = True
            diagnostics["authorized_account_ids"] = auth_state.get(
                "authorized_account_ids",
                []
            )
            diagnostics.update(get_ctrader_account_selection_debug())
            diagnostics["configured_account_id_found"] = auth_state.get(
                "configured_account_id_found",
                False
            )
        finally:
            try:
                sock.close()
            except Exception:
                pass

    except Exception as e:
        diagnostics["error"] = str(e)
        return diagnostics

    try:
        if DEBUG_OPEN_POSITIONS is not None:
            get_open_positions()
        else:
            normalize_positions(fetch_ctrader_open_positions(config))

        diagnostics["can_fetch_positions"] = True
    except Exception as e:
        diagnostics["error"] = str(e)

    try:
        candles = get_ctrader_market_data("EURUSD", "5min", limit=10)
        diagnostics["can_fetch_candles"] = not candles.empty
        diagnostics["candle_error"] = None if diagnostics["can_fetch_candles"] else LAST_CTRADER_CANDLE_ERROR
    except Exception as e:
        diagnostics["can_fetch_candles"] = False
        diagnostics["candle_error"] = str(e)

    diagnostics["ok"] = (
        diagnostics["can_fetch_positions"]
        and diagnostics["can_fetch_candles"]
    )

    return diagnostics

def fetch_ctrader_open_positions(config):
    host, port = CTRADER_JSON_ENDPOINTS[config["env"]]
    account_id = int(config["account_id"])

    sock = open_ctrader_json_socket(host, port)

    try:
        authorize_ctrader_socket(sock, config, account_id)

        symbol_map = fetch_ctrader_symbol_map(sock, account_id)
        trader_response = send_ctrader_request(
            sock,
            PAYLOAD_TRADER_REQ,
            {
                "ctidTraderAccountId": account_id,
            },
            PAYLOAD_TRADER_RES,
        )

        reconcile = send_ctrader_request(
            sock,
            PAYLOAD_RECONCILE_REQ,
            {
                "ctidTraderAccountId": account_id,
                "returnProtectionOrders": False,
            },
            PAYLOAD_RECONCILE_RES,
        )

        positions = reconcile.get("payload", {}).get("position", [])
        unrealized_pnl_response = {}

        try:
            unrealized_pnl_response = fetch_ctrader_position_unrealized_pnl(
                sock,
                account_id,
            )
        except Exception as e:
            print("CTRADER_UNREALIZED_PNL_FETCH_ERROR:", str(e))

        unrealized_pnl_by_position_id = unrealized_pnl_response.get(
            "by_position_id",
            {},
        )
        normalized = [
            normalize_ctrader_position(
                position,
                symbol_map,
                unrealized_pnl_by_position_id,
            )
            for position in positions
        ]

        for position in positions:
            trade_data = position.get("tradeData", {})
            symbol = symbol_map.get(str(trade_data.get("symbolId")))

            if normalize_symbol(symbol) != "EURUSD":
                continue

            position_id = position.get("positionId")
            unrealized_pnl = unrealized_pnl_by_position_id.get(str(position_id))
            print("CTRADER_POSITION_FULL_KEYS =", {
                "position_id": position_id,
                "symbol": symbol,
                "all_position_keys": list(position.keys()),
                "nested_keys": collect_nested_keys(position),
                "trade_data_keys": list(trade_data.keys()),
                "account_keys": collect_nested_keys(trader_response.get("payload", {})),
                "reconcile_keys": collect_nested_keys(reconcile.get("payload", {})),
                "unrealized_pnl_keys": collect_nested_keys(unrealized_pnl or {}),
                "unrealized_pnl_object": unrealized_pnl,
                "account_object": trader_response.get("payload", {}),
            })

        print("CTRADER POSITIONS FETCH OK:", normalized)

        return normalized

    finally:
        try:
            sock.close()
        except Exception:
            pass

def modify_position_sltp(position_id, stop_loss_price=None, take_profit_price=None):
    config = get_ctrader_config()

    if not config:
        return {
            "ok": False,
            "reason": "Missing or invalid cTrader config",
            "position_id": position_id,
            "stop_loss": stop_loss_price,
            "take_profit": take_profit_price,
        }

    def confirm_by_readback(reason):
        """Resolve an ambiguous amend response from broker-authoritative state."""
        try:
            positions = fetch_ctrader_open_positions() or []
        except Exception as readback_error:
            return {
                "ok": False,
                "reason": f"{reason}; readback failed: {readback_error}",
                "position_id": position_id_value,
                "stop_loss": request_payload.get("stopLoss"),
                "take_profit": request_payload.get("takeProfit"),
            }

        position = next(
            (
                row for row in positions
                if str(row.get("position_id") or row.get("positionId"))
                == str(position_id_value)
            ),
            None,
        )
        if not position:
            return {
                "ok": False,
                "reason": f"{reason}; position not found during readback",
                "position_id": position_id_value,
                "stop_loss": request_payload.get("stopLoss"),
                "take_profit": request_payload.get("takeProfit"),
            }

        actual_sl = position.get("stop_loss") or position.get("stopLoss")
        actual_tp = position.get("take_profit") or position.get("takeProfit")

        def matches(requested, actual):
            if requested is None:
                return True
            try:
                requested_value = float(requested)
                actual_value = float(actual)
            except (TypeError, ValueError):
                return False
            return abs(requested_value - actual_value) <= max(
                1e-8,
                abs(requested_value) * 1e-9,
            )

        confirmed = (
            matches(request_payload.get("stopLoss"), actual_sl)
            and matches(request_payload.get("takeProfit"), actual_tp)
        )
        return {
            "ok": confirmed,
            "reason": (
                "Broker amendment confirmed by position readback"
                if confirmed else reason
            ),
            "position_id": position_id_value,
            "stop_loss": actual_sl,
            "take_profit": actual_tp,
            "confirmed_by_readback": confirmed,
        }

    try:
        position_id_value = int(position_id)
    except (TypeError, ValueError):
        return {
            "ok": False,
            "reason": "Invalid position id",
            "position_id": position_id,
            "stop_loss": stop_loss_price,
            "take_profit": take_profit_price,
        }

    request_payload = {
        "ctidTraderAccountId": int(config["account_id"]),
        "positionId": position_id_value,
    }

    if stop_loss_price is not None:
        try:
            request_payload["stopLoss"] = float(stop_loss_price)
        except (TypeError, ValueError):
            return {
                "ok": False,
                "reason": "Invalid stop loss",
                "position_id": position_id,
                "stop_loss": stop_loss_price,
                "take_profit": take_profit_price,
            }

    if take_profit_price is not None:
        try:
            request_payload["takeProfit"] = float(take_profit_price)
        except (TypeError, ValueError):
            return {
                "ok": False,
                "reason": "Invalid take profit",
                "position_id": position_id,
                "stop_loss": stop_loss_price,
                "take_profit": take_profit_price,
            }

    if "stopLoss" not in request_payload and "takeProfit" not in request_payload:
        return {
            "ok": False,
            "reason": "No SL or TP supplied",
            "position_id": position_id_value,
            "stop_loss": stop_loss_price,
            "take_profit": take_profit_price,
        }

    account_id = int(config["account_id"])
    host, port = CTRADER_JSON_ENDPOINTS[config["env"]]
    sock = open_ctrader_json_socket(host, port)

    try:
        authorize_ctrader_socket(sock, config, account_id)
        print("CTRADER MODIFY POSITION SLTP REQUEST:", request_payload)
        response = send_ctrader_request(
            sock,
            PAYLOAD_AMEND_POSITION_SLTP_REQ,
            request_payload,
            PAYLOAD_EXECUTION_EVENT,
        )
        payload = response.get("payload", {})

        if payload.get("errorCode"):
            readback = confirm_by_readback(
                payload.get("description") or payload.get("errorCode")
            )
            if readback.get("ok"):
                return readback
            return {
                "ok": False,
                "reason": payload.get("description") or payload.get("errorCode"),
                "position_id": position_id_value,
                "stop_loss": request_payload.get("stopLoss"),
                "take_profit": request_payload.get("takeProfit"),
                "raw": payload,
            }

        return {
            "ok": True,
            "position_id": position_id_value,
            "stop_loss": request_payload.get("stopLoss"),
            "take_profit": request_payload.get("takeProfit"),
            "raw": payload,
        }
    except Exception as e:
        print("CTRADER MODIFY POSITION SLTP ERROR:", e)
        return confirm_by_readback(str(e))
    finally:
        try:
            sock.close()
        except Exception:
            pass

def modify_position_stop_loss(position_id, protected_sl_price, take_profit_price=None):
    return modify_position_sltp(
        position_id,
        stop_loss_price=protected_sl_price,
        take_profit_price=take_profit_price,
    )

def fetch_ctrader_symbol_map(sock, account_id):
    try:
        response = send_ctrader_request(
            sock,
            PAYLOAD_SYMBOLS_LIST_REQ,
            {
                "ctidTraderAccountId": account_id,
                "includeArchivedSymbols": False,
            },
            PAYLOAD_SYMBOLS_LIST_RES,
        )

        symbols = response.get("payload", {}).get("symbol", [])

        return {
            str(symbol.get("symbolId")): (
                symbol.get("symbolName")
                or symbol.get("name")
                or symbol.get("displayName")
            )
            for symbol in symbols
            if symbol.get("symbolId") is not None
        }

    except Exception as e:
        print("CTRADER SYMBOL MAP FETCH ERROR:", e)
        return {}

def first_present(*values):
    for value in values:
        if value is not None:
            return value

    return None

def collect_nested_keys(value, prefix=""):
    keys = []

    if isinstance(value, dict):
        for key, nested_value in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            keys.append(path)
            keys.extend(collect_nested_keys(nested_value, path))
    elif isinstance(value, list):
        for index, nested_value in enumerate(value[:3]):
            path = f"{prefix}[{index}]" if prefix else f"[{index}]"
            keys.append(path)
            keys.extend(collect_nested_keys(nested_value, path))

    return keys

def fetch_ctrader_position_unrealized_pnl(sock, account_id):
    response = send_ctrader_request(
        sock,
        PAYLOAD_GET_POSITION_UNREALIZED_PNL_REQ,
        {
            "ctidTraderAccountId": account_id,
        },
        PAYLOAD_GET_POSITION_UNREALIZED_PNL_RES,
    )
    payload = response.get("payload", {})
    money_digits = payload.get("moneyDigits", 2)
    values = payload.get("positionUnrealizedPnL") or []
    by_position_id = {}

    for item in values:
        if not isinstance(item, dict):
            continue

        position_id = item.get("positionId") or item.get("position_id")

        if position_id is None:
            continue

        by_position_id[str(position_id)] = {
            **item,
            "moneyDigits": money_digits,
            "grossUnrealizedPnLParsed": parse_ctrader_money(
                item.get("grossUnrealizedPnL"),
                money_digits,
            ),
            "netUnrealizedPnLParsed": parse_ctrader_money(
                item.get("netUnrealizedPnL"),
                money_digits,
            ),
        }

    print("CTRADER_UNREALIZED_PNL_RESPONSE:", {
        "moneyDigits": money_digits,
        "positionUnrealizedPnL": values,
        "parsed_by_position_id": by_position_id,
    })

    return {
        "raw": response,
        "money_digits": money_digits,
        "by_position_id": by_position_id,
    }

def normalize_ctrader_position(position, symbol_map, unrealized_pnl_by_position_id=None):
    trade_data = position.get("tradeData", {})
    symbol_id = trade_data.get("symbolId")
    side = trade_data.get("tradeSide")
    position_id = (
        position.get("positionId")
        or position.get("position_id")
        or position.get("id")
    )
    unrealized_pnl = None

    if unrealized_pnl_by_position_id:
        unrealized_pnl = unrealized_pnl_by_position_id.get(str(position_id))

    raw_pnl = first_present(
        unrealized_pnl.get("netUnrealizedPnLParsed") if unrealized_pnl else None,
        position.get("netProfit"),
        position.get("profit"),
        position.get("grossProfit"),
        position.get("moneyProfit"),
        position.get("pnl"),
        position.get("unrealizedNetProfit"),
        position.get("unrealizedGrossProfit"),
    )
    pnl = parse_ctrader_money(
        raw_pnl,
        position.get("moneyDigits", 2)
    )
    current_price = first_present(
        position.get("currentPrice"),
        position.get("marketPrice"),
        position.get("closePrice"),
        position.get("price"),
    )

    return {
        "position_id": position_id,
        "symbol": (
            symbol_map.get(str(symbol_id))
            or position.get("symbol")
            or position.get("symbolName")
            or str(symbol_id or "")
        ),
        "side": normalize_trade_side(side),
        "volume": trade_data.get("volume") or position.get("volume"),
        "entry": position.get("price") or position.get("entry"),
        "stop_loss": first_present(
            position.get("stopLoss"),
            position.get("sl"),
            position.get("stop_loss"),
            trade_data.get("stopLoss"),
        ),
        "take_profit": first_present(
            position.get("takeProfit"),
            position.get("tp"),
            position.get("take_profit"),
            trade_data.get("takeProfit"),
        ),
        "current_price": current_price,
        "opened_at": (
            trade_data.get("openTimestamp")
            or position.get("opened_at")
        ),
        "pnl": pnl,
        "profit": pnl,
        "netUnrealizedPnL": (
            unrealized_pnl.get("netUnrealizedPnLParsed")
            if unrealized_pnl else None
        ),
        "grossUnrealizedPnL": (
            unrealized_pnl.get("grossUnrealizedPnLParsed")
            if unrealized_pnl else None
        ),
        "raw_unrealized_pnl": unrealized_pnl,
        "raw": position,
    }

def normalize_trade_side(side):
    if side == 1 or str(side).upper() == "BUY":
        return "BUY"
    if side == 2 or str(side).upper() == "SELL":
        return "SELL"
    return str(side or "").upper()

def open_ctrader_json_socket(host, port):
    import certifi

    raw = socket.create_connection((host, port), timeout=8)

    context = ssl.create_default_context(
        cafile=certifi.where()
    )

    sock = context.wrap_socket(
        raw,
        server_hostname=host
    )
    sock.settimeout(8)

    key = base64.b64encode(os.urandom(16)).decode("ascii")
    request = (
        "GET / HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    )

    sock.sendall(request.encode("ascii"))
    response = b""

    while b"\r\n\r\n" not in response:
        chunk = sock.recv(4096)
        if not chunk:
            break
        response += chunk

    if b" 101 " not in response.split(b"\r\n", 1)[0]:
        raise RuntimeError(
            f"cTrader WebSocket handshake failed: {response[:120]!r}"
        )

    return sock

def send_ctrader_request(sock, payload_type, payload, expected_payload_type):
    client_msg_id = str(uuid.uuid4())
    message = {
        "clientMsgId": client_msg_id,
        "payloadType": payload_type,
        "payload": payload,
    }

    websocket_send_text(sock, json.dumps(message))

    while True:
        incoming = websocket_recv_text(sock)

        if not incoming:
            continue

        data = json.loads(incoming)
        incoming_type = data.get("payloadType")

        if incoming_type == PAYLOAD_ERROR_RES:
            raise CTraderApiError("cTrader error response", data)

        if incoming_type == PAYLOAD_ORDER_ERROR_EVENT:
            raise CTraderApiError("cTrader order error response", data)

        if incoming_type == expected_payload_type:
            return data

def websocket_send_frame(sock, opcode, payload=b""):
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    header = bytearray([0x80 | (int(opcode) & 0x0F)])
    length = len(payload)

    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack("!Q", length))

    mask = os.urandom(4)
    masked_payload = bytes(
        byte ^ mask[index % 4]
        for index, byte in enumerate(payload)
    )

    sock.sendall(bytes(header) + mask + masked_payload)


def websocket_send_text(sock, text):
    websocket_send_frame(sock, 0x1, text)


def send_ctrader_heartbeat(sock):
    websocket_send_text(sock, json.dumps({
        "clientMsgId": str(uuid.uuid4()),
        "payloadType": CTRADER_HEARTBEAT_PAYLOAD_TYPE,
        "payload": {},
    }))


def websocket_recv_text(sock):
    while True:
        first = recv_exact(sock, 2)
        opcode = first[0] & 0x0F
        masked = bool(first[1] & 0x80)
        length = first[1] & 0x7F

        if length == 126:
            length = struct.unpack("!H", recv_exact(sock, 2))[0]
        elif length == 127:
            length = struct.unpack("!Q", recv_exact(sock, 8))[0]

        mask = recv_exact(sock, 4) if masked else None
        payload = recv_exact(sock, length) if length else b""

        if mask:
            payload = bytes(
                byte ^ mask[index % 4]
                for index, byte in enumerate(payload)
            )

        if opcode == 0x8:
            raise RuntimeError("cTrader WebSocket closed")
        if opcode == 0x9:
            # cTrader periodically pings persistent spot subscriptions. Reply
            # with the identical payload or the server closes an otherwise
            # healthy authenticated stream.
            websocket_send_frame(sock, 0xA, payload)
            continue
        if opcode == 0xA:
            continue
        if opcode != 0x1:
            return ""

        return payload.decode("utf-8")

def recv_exact(sock, length):
    data = b""

    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            raise RuntimeError("cTrader WebSocket connection closed")
        data += chunk

    return data

def normalize_positions(raw_positions):
    if not isinstance(raw_positions, list):
        return []

    positions = []

    for position in raw_positions:
        if not isinstance(position, dict):
            continue

        symbol = (
            position.get("symbol")
            or position.get("symbolName")
            or position.get("instrument")
        )
        side = (
            position.get("side")
            or position.get("tradeSide")
            or position.get("direction")
        )

        if not symbol or not side:
            continue

        positions.append({
            "position_id": (
                position.get("position_id")
                or position.get("positionId")
                or position.get("id")
            ),
            "symbol": normalize_symbol(symbol),
            "side": str(side).upper(),
            "volume": (
                position.get("volume")
                or position.get("quantity")
                or position.get("lotSize")
            ),
            "entry": (
                position.get("entry")
                or position.get("entry_price")
                or position.get("entryPrice")
            ),
            "stop_loss": first_present(
                position.get("stop_loss"),
                position.get("stopLoss"),
                position.get("sl"),
            ),
            "take_profit": first_present(
                position.get("take_profit"),
                position.get("takeProfit"),
                position.get("tp"),
                position.get("tp2"),
            ),
            "opened_at": (
                position.get("opened_at")
                or position.get("openTime")
                or position.get("createdAt")
            ),
            "current_price": first_present(
                position.get("current_price"),
                position.get("currentPrice"),
                position.get("marketPrice"),
                position.get("closePrice"),
                position.get("price"),
            ),
            "pnl": first_present(
                position.get("netProfit"),
                position.get("pnl"),
                position.get("profit"),
                position.get("grossProfit"),
                position.get("moneyProfit"),
                position.get("unrealizedNetProfit"),
                position.get("unrealizedGrossProfit"),
            ),
            "profit": first_present(
                position.get("netProfit"),
                position.get("profit"),
                position.get("pnl"),
                position.get("grossProfit"),
                position.get("moneyProfit"),
                position.get("unrealizedNetProfit"),
                position.get("unrealizedGrossProfit"),
            ),
            "raw": position,
        })

    return positions

def remove_debug_open_position(symbol):
    global DEBUG_OPEN_POSITIONS

    execution_symbol = normalize_symbol(symbol)

    if DEBUG_OPEN_POSITIONS is None:
        return []

    if not isinstance(DEBUG_OPEN_POSITIONS, list):
        DEBUG_OPEN_POSITIONS = []
        return []

    filtered_positions = []

    for position in DEBUG_OPEN_POSITIONS:
        if not isinstance(position, dict):
            continue

        position_symbol = (
            position.get("symbol")
            or position.get("symbolName")
            or position.get("instrument")
        )

        if normalize_symbol(position_symbol) == execution_symbol:
            continue

        filtered_positions.append(position)

    DEBUG_OPEN_POSITIONS = filtered_positions

    print(
        "CTRADER DEBUG POSITION REMOVED:",
        execution_symbol,
        DEBUG_OPEN_POSITIONS
    )

    return normalize_positions(DEBUG_OPEN_POSITIONS)

def set_debug_open_positions(positions):
    global DEBUG_OPEN_POSITIONS

    DEBUG_OPEN_POSITIONS = positions
    print("CTRADER DEBUG POSITIONS SET:", DEBUG_OPEN_POSITIONS)

    return normalize_positions(DEBUG_OPEN_POSITIONS)


def close_position(position_id, volume=None):
    config = get_ctrader_config()

    if not config:
        return {
            "ok": False,
            "reason": "Missing or invalid cTrader config",
            "position_id": position_id,
        }

    try:
        position_id_value = int(position_id)
    except (TypeError, ValueError):
        return {
            "ok": False,
            "reason": "Invalid position id",
            "position_id": position_id,
        }

    if volume is None:
        matching_position = next(
            (
                position
                for position in get_open_positions()
                if str(
                    position.get("position_id")
                    or position.get("positionId")
                    or position.get("id")
                ) == str(position_id_value)
            ),
            None,
        )
        volume = (
            (matching_position or {}).get("volume")
            or (matching_position or {}).get("volumeInUnits")
            or ((matching_position or {}).get("tradeData") or {}).get("volume")
        )

    try:
        volume_value = int(float(volume))
    except (TypeError, ValueError):
        return {
            "ok": False,
            "reason": "Missing or invalid position volume",
            "position_id": position_id_value,
        }

    if volume_value <= 0:
        return {
            "ok": False,
            "reason": "Position volume must be positive",
            "position_id": position_id_value,
        }

    account_id = int(config["account_id"])
    host, port = CTRADER_JSON_ENDPOINTS[config["env"]]
    request_payload = {
        "ctidTraderAccountId": account_id,
        "positionId": position_id_value,
        "volume": volume_value,
    }
    sock = open_ctrader_json_socket(host, port)

    try:
        authorize_ctrader_socket(sock, config, account_id)
        print("CTRADER CLOSE POSITION REQUEST:", request_payload)
        response = send_ctrader_request(
            sock,
            PAYLOAD_CLOSE_POSITION_REQ,
            request_payload,
            PAYLOAD_EXECUTION_EVENT,
        )
        payload = response.get("payload", {})

        if payload.get("errorCode"):
            return {
                "ok": False,
                "reason": payload.get("description") or payload.get("errorCode"),
                "position_id": position_id_value,
                "volume": volume_value,
                "raw": payload,
            }

        return {
            "ok": True,
            "position_id": position_id_value,
            "volume": volume_value,
            "raw": payload,
        }
    except Exception as exc:
        print("CTRADER CLOSE POSITION ERROR:", exc)
        return {
            "ok": False,
            "reason": str(exc),
            "position_id": position_id_value,
            "volume": volume_value,
        }
    finally:
        try:
            sock.close()
        except Exception:
            pass
