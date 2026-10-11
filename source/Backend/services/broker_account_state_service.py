"""Durable storage for the selected cTrader account."""

import json
import threading
from datetime import datetime, timezone

from db import SessionLocal
from models import RuntimeSetting


SETTING_NAME = "ctrader_active_account"
_LOCK = threading.RLock()

def load_active_account_selection(session_factory=None):
    factory = session_factory or SessionLocal
    try:
        with factory() as session:
            row = session.get(RuntimeSetting, SETTING_NAME)
            if row is None or not row.setting_value:
                return {}
            payload = json.loads(row.setting_value)
            revision = row.updated_at.isoformat() if row.updated_at else None
    except Exception as exc:
        print("CTRADER_ACTIVE_ACCOUNT_DURABLE_LOAD_ERROR =", str(exc))
        return {}

    if not isinstance(payload, dict):
        return {}
    account_id = str(payload.get("account_id") or "").strip()
    environment = str(payload.get("env") or "").strip().lower()
    return {
        "active_account_id": account_id or None,
        "active_account_env": environment if environment in {"demo", "live"} else None,
        "selection_revision": revision,
    }


def save_active_account_selection(
    account_id,
    environment,
    session_factory=None,
    updated_by="ctrader_connector",
):
    payload = json.dumps({
        "account_id": str(account_id or "").strip() or None,
        "env": str(environment or "").strip().lower() or None,
    }, separators=(",", ":"))
    factory = session_factory or SessionLocal
    try:
        with _LOCK:
            with factory() as session:
                row = session.get(RuntimeSetting, SETTING_NAME)
                if row is None:
                    row = RuntimeSetting(setting_name=SETTING_NAME)
                    session.add(row)
                row.setting_value = payload
                row.updated_at = datetime.now(timezone.utc)
                row.updated_by = str(updated_by)
                session.commit()
    except Exception as exc:
        print("CTRADER_ACTIVE_ACCOUNT_DURABLE_SAVE_ERROR =", str(exc))
        return False
    return True
