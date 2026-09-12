"""V3B-safe immutable execution audit.

The retired V1 lifecycle observer used to feed this module.  Production V3B
still benefits from one low-volume pre-submit audit row per actual order
attempt, so that audit is retained without any dependency on V1 diagnostics or
V1 lifecycle tables.  Nothing here permits, blocks, sizes, submits, modifies,
or closes a trade.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import os
import uuid

from sqlalchemy import select

from db import SessionLocal
from models import ForexExecutionSnapshot


SESSION_ID = os.getenv("RENDER_INSTANCE_ID") or f"boot-{uuid.uuid4()}"
DEPLOYMENT_SHA = (
    os.getenv("RENDER_GIT_COMMIT")
    or os.getenv("GIT_COMMIT")
    or os.getenv("SOURCE_VERSION")
    or "unknown"
)


def _json_safe(value):
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "item"):
        try:
            return _json_safe(value.item())
        except Exception:
            pass
    return str(value)


def persist_lifecycle_evaluation_safely(*_args, **_kwargs):
    """Compatibility no-op: the production V1 lifecycle observer is retired."""
    return {"ok": True, "disabled": True, "reason": "PRODUCTION_V1_RETIRED"}


def persist_execution_snapshot_safely(*, symbol, direction, trade_payload, plan,
                                      quote, risk_size, gate_results):
    """Persist one concise pre-submit audit row directly from the V3B payload."""
    db = None
    try:
        attempted_at = datetime.now(timezone.utc)
        payload = copy.deepcopy(trade_payload or {})
        plan_copy = copy.deepcopy(plan or {})
        quote_copy = copy.deepcopy(quote or {})
        risk_copy = copy.deepcopy(risk_size or {})
        gates = copy.deepcopy(gate_results or {})
        setup_identity = payload.get("setup_identity") or {}
        confirmation = payload.get("confirmation_5m") or {}
        normalized_symbol = str(symbol or payload.get("symbol") or "").upper().replace("/", "")
        normalized_direction = str(direction or payload.get("side") or payload.get("action") or "").upper()
        snapshot_id = str(uuid.uuid4())
        broker_quote = (
            quote_copy.get("ask")
            if normalized_direction == "BUY"
            else quote_copy.get("bid")
        )
        event_id = (
            payload.get("source_indicator_event_id")
            or setup_identity.get("indicator_event_id")
        )
        confirmation_id = (
            payload.get("m5_confirmation_id")
            or setup_identity.get("m5_confirmation_id")
            or confirmation.get("confirmation_id")
        )
        account_id = (
            payload.get("account_id")
            or os.getenv("ACTIVE_CTRADER_ACCOUNT_ID")
            or os.getenv("CTRADER_ACCOUNT_ID")
        )
        broker_environment = (
            payload.get("broker_environment")
            or os.getenv("ACTIVE_CTRADER_ACCOUNT_ENV")
            or payload.get("mode")
        )
        client_order_id = payload.get("client_order_id") or payload.get("signal_setup_id")

        snapshot = _json_safe({
            "snapshot_id": snapshot_id,
            "snapshot_version": 2,
            "immutable_pre_submit": True,
            "production_sha": DEPLOYMENT_SHA,
            "backend_session_id": SESSION_ID,
            "strategy_execution_profile": payload.get("strategy_execution_profile"),
            "live_strategy_model": payload.get("live_strategy_model"),
            "symbol": normalized_symbol,
            "account_id": account_id,
            "broker_environment": broker_environment,
            "direction": normalized_direction,
            "event_id": event_id,
            "confirmation_id": confirmation_id,
            "signal_setup_id": payload.get("signal_setup_id"),
            "setup_identity": setup_identity,
            "indicator_event_identity": payload.get("indicator_event_identity") or {},
            "m5_confirmation_identity": payload.get("m5_confirmation_identity") or {},
            "entry": payload.get("entry"),
            "sl": payload.get("sl"),
            "tp1": payload.get("tp1"),
            "tp2": payload.get("tp2"),
            "protected_sl_price": payload.get("protected_sl_price"),
            "broker_quote_at_submission": broker_quote,
            "risk_percent": risk_copy.get("risk_percent"),
            "risk_amount": risk_copy.get("risk_amount"),
            "volume": payload.get("volume_units") or payload.get("volume"),
            "gate_results": gates,
            "plan_summary": {
                "strategy_setup_type": plan_copy.get("strategy_setup_type"),
                "strategy_setup_complete": plan_copy.get("strategy_setup_complete"),
                "signal": plan_copy.get("signal") or plan_copy.get("final_signal"),
            },
            "order_attempted_at": attempted_at.isoformat(),
            "client_order_id": client_order_id,
        })

        db = SessionLocal()
        db.add(ForexExecutionSnapshot(
            snapshot_id=snapshot_id,
            snapshot_version=2,
            production_sha=DEPLOYMENT_SHA,
            backend_session_id=SESSION_ID,
            symbol=normalized_symbol,
            account_id=account_id,
            broker_environment=broker_environment,
            direction=normalized_direction,
            event_id=str(event_id) if event_id else None,
            confirmation_id=str(confirmation_id) if confirmation_id else None,
            order_attempted_at=attempted_at,
            client_order_id=str(client_order_id) if client_order_id else None,
            broker_order_id=None,
            position_id=None,
            broker_response_at=None,
            snapshot_json=snapshot,
            created_at=attempted_at,
        ))
        db.commit()
        return copy.deepcopy(snapshot)
    except Exception:
        if db is not None:
            db.rollback()
        return None
    finally:
        if db is not None:
            db.close()


def record_execution_response_safely(symbol, broker_result):
    """Attach broker IDs to the latest V3B audit row; never affects execution."""
    db = None
    try:
        db = SessionLocal()
        row = db.execute(
            select(ForexExecutionSnapshot)
            .where(ForexExecutionSnapshot.symbol == str(symbol).upper().replace("/", ""))
            .order_by(
                ForexExecutionSnapshot.order_attempted_at.desc(),
                ForexExecutionSnapshot.id.desc(),
            )
            .limit(1)
        ).scalar_one_or_none()
        if row is None:
            return False
        result = copy.deepcopy(broker_result or {})
        row.broker_order_id = result.get("order_id") or result.get("broker_order_id")
        row.position_id = result.get("position_id") or result.get("broker_position_id")
        row.broker_response_at = datetime.now(timezone.utc)
        db.commit()
        return True
    except Exception:
        if db is not None:
            db.rollback()
        return False
    finally:
        if db is not None:
            db.close()
