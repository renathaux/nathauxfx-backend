"""Read-only hosted symbol diagnostic. Never import/start strategy execution."""
import json
import os
import re
from threading import Lock

_IN_FLIGHT = Lock()


def _selected_account():
    # The normal selection loader logs raw DB errors and falls back to legacy
    # state. This diagnostic instead fails closed on a direct, read-only query.
    from db import SessionLocal
    from models import RuntimeSetting
    with SessionLocal() as session:
        row = session.get(RuntimeSetting, "ctrader_active_account")
        if row is None:
            raise ValueError("Selected account unavailable")
        selection = json.loads(row.setting_value)
        account = selection.get("account_id")
        environment = selection.get("env")
        if isinstance(account, bool) or not re.fullmatch(r"[0-9]+", str(account)):
            raise ValueError("Selected account unavailable")
        if not 0 < int(account) < 2**63 or environment not in {"demo", "live"}:
            raise ValueError("Selected account unavailable")
        return str(account), environment, row.updated_at


def _credentials():
    # Existing encrypted token store: reads only; errors log exception TYPE only.
    # No hydration, refresh, persistence or account activation is invoked.
    from services.ctrader_token_service import load_tokens
    stored = load_tokens()
    values = {
        "client_id": os.getenv("CTRADER_CLIENT_ID"),
        "client_secret": os.getenv("CTRADER_CLIENT_SECRET"),
        "access_token": stored.get("access_token") or os.getenv("CTRADER_ACCESS_TOKEN"),
    }
    if not all(isinstance(value, str) and value for value in values.values()):
        raise ValueError("Credentials unavailable")
    return values


def _fields(record, schema):
    if not isinstance(record, dict):
        raise ValueError("Invalid market-hours record")
    projected = {}
    for name, kind in schema.items():
        value = record.get(name)
        valid = value is None
        if kind == "integer":
            valid |= type(value) is int or (isinstance(value, str) and re.fullmatch(r"-?[0-9]+", value) is not None)
        elif kind == "boolean":
            valid |= type(value) is bool
        else:
            valid |= isinstance(value, str) and len(value) <= 256 and not any(ord(char) < 32 for char in value)
        if not valid:
            raise ValueError("Invalid market-hours field")
        projected[name] = value
    return projected


def _project(symbol, name):
    result = _fields(symbol, {"symbolId": "integer", "scheduleTimeZone": "string"})
    result["symbolName"] = name
    for field, schema in {
        "schedule": {"startSecond": "integer", "endSecond": "integer"},
        "holiday": {"holidayId": "integer", "name": "string", "scheduleTimeZone": "string",
                    "holidayDate": "integer", "isRecurring": "boolean",
                    "startSecond": "integer", "endSecond": "integer"},
    }.items():
        items = symbol.get(field)
        if items is not None and not isinstance(items, list):
            raise ValueError("Invalid market-hours list")
        result[field] = None if items is None else [_fields(item, schema) for item in items]
    return result


def collect_symbol_market_hours():
    """Exactly app auth, selected-account auth, symbols list and symbol-by-ID.

    Uses existing hosted credentials on an exclusively owned socket, NOT the
    live stream socket. No token refresh/retry or account fallback is permitted.
    Missing fields remain null; this retrieves metadata, not historical proof.
    """
    if not _IN_FLIGHT.acquire(blocking=False):
        raise RuntimeError("Diagnostic already in progress")
    sock = None
    try:
        import ctrader_connector as connector
        identity = _selected_account()
        account_id, environment, _revision = identity
        credentials = _credentials()
        sock = connector.open_ctrader_json_socket(*connector.CTRADER_JSON_ENDPOINTS[environment], close_on_error=True)
        connector.send_ctrader_request(
            sock, connector.PAYLOAD_APPLICATION_AUTH_REQ,
            {"clientId": credentials["client_id"], "clientSecret": credentials["client_secret"]},
            connector.PAYLOAD_APPLICATION_AUTH_RES,
        )
        response = connector.send_ctrader_request(
            sock, connector.PAYLOAD_ACCOUNT_AUTH_REQ,
            {"ctidTraderAccountId": int(account_id), "accessToken": credentials["access_token"]},
            connector.PAYLOAD_ACCOUNT_AUTH_RES,
        )
        if str(response.get("payload", {}).get("ctidTraderAccountId")) != account_id:
            raise ValueError("Account mismatch")
        response = connector.send_ctrader_request(
            sock, connector.PAYLOAD_SYMBOLS_LIST_REQ,
            {"ctidTraderAccountId": int(account_id), "includeArchivedSymbols": False},
            connector.PAYLOAD_SYMBOLS_LIST_RES,
        )
        payload = response.get("payload", {})
        if str(payload.get("ctidTraderAccountId")) != account_id:
            raise ValueError("Account mismatch")
        selected = []
        for name in ("EURUSD", "XAUUSD"):
            matches = [item for item in payload.get("symbol", [])
                       if isinstance(item, dict) and item.get("symbolName") == name]
            if len(matches) != 1:
                raise ValueError("Symbol unavailable or ambiguous")
            selected.append(matches[0])
        full = connector.fetch_ctrader_full_symbols(sock, int(account_id), [item["symbolId"] for item in selected])
        by_id = {str(item["symbolId"]): item for item in full["symbol"]}
        result = {"symbols": [_project(by_id[str(item["symbolId"])], item["symbolName"]) for item in selected]}
        # Also reject a credential echoed INSIDE an otherwise allowed text field.
        serialized = json.dumps(result)
        if any(json.dumps(credentials[key])[1:-1] in serialized for key in ("client_secret", "access_token")):
            raise ValueError("Unsafe upstream metadata")
        if identity != _selected_account():
            raise ValueError("Account selection changed")
        return result
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass  # Never log socket exceptions or credential-bearing locals.
        _IN_FLIGHT.release()
