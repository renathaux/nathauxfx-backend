"""Behavior-preserving execution risk checks and append-only audit records."""
from __future__ import annotations

from datetime import datetime, timezone
import math

from db import SessionLocal
from models import ExecutionRiskAudit


def _number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def executable_entry(side, tick):
    side = str(side or "").upper()
    if not isinstance(tick, dict):
        return None
    return _number(tick.get("ask") if side == "BUY" else tick.get("bid") if side == "SELL" else None)


def validate_pre_submit(
    symbol,
    side,
    planned_entry,
    sl,
    tp2,
    current_volume_units,
    tick,
    risk_size_calculator,
    rr_validator,
):
    actual_entry = executable_entry(side, tick)
    if actual_entry is None:
        return {"ok": False, "reason": "WAIT_EXECUTABLE_PRICE_UNAVAILABLE"}
    locked_risk = risk_size_calculator(symbol, actual_entry, sl)
    locked_rr = rr_validator(symbol, side, actual_entry, sl, tp2)
    if not locked_risk.get("ok"):
        return {
            "ok": False,
            "reason": "WAIT_EXECUTABLE_RISK_INVALID",
            "executable_entry": actual_entry,
            "locked_risk": locked_risk,
            "locked_rr": locked_rr,
        }
    if not locked_rr.get("ok"):
        return {
            "ok": False,
            "reason": "WAIT_EXECUTABLE_RR_INVALID",
            "executable_entry": actual_entry,
            "locked_risk": locked_risk,
            "locked_rr": locked_rr,
        }
    current_units = _number(current_volume_units)
    allowed_units = _number(locked_risk.get("volume_units"))
    if current_units is None or allowed_units is None or allowed_units <= 0:
        return {"ok": False, "reason": "WAIT_EXECUTABLE_VOLUME_UNPROVABLE"}
    final_units = min(current_units, allowed_units)
    if final_units <= 0:
        return {"ok": False, "reason": "WAIT_EXECUTABLE_VOLUME_INVALID"}
    current_lot = _number(locked_risk.get("lot_size"))
    final_lot = current_lot
    if allowed_units and current_lot is not None:
        final_lot = current_lot * final_units / allowed_units
    return {
        "ok": True,
        "reason": "EXECUTABLE_RISK_VALIDATED",
        "planned_entry": _number(planned_entry),
        "executable_entry": actual_entry,
        "initial_volume_units": current_units,
        "volume_units": final_units,
        "lot_size": final_lot,
        "size_reduced": final_units < current_units,
        "locked_risk": locked_risk,
        "locked_rr": locked_rr,
    }


def validate_sl_amendment(entry, old_sl, new_sl, current_volume_units, allowed_risk_size):
    entry = _number(entry)
    old_sl = _number(old_sl)
    new_sl = _number(new_sl)
    current_units = _number(current_volume_units)
    allowed_units = _number((allowed_risk_size or {}).get("volume_units"))
    if None in {entry, old_sl, new_sl, current_units, allowed_units}:
        return {"ok": False, "reason": "SL_CHANGE_RISK_UNPROVABLE"}
    old_distance = abs(entry - old_sl)
    new_distance = abs(entry - new_sl)
    if new_distance <= old_distance:
        return {
            "ok": True,
            "reason": "SL_CHANGE_DOES_NOT_INCREASE_RISK",
            "old_distance": old_distance,
            "new_distance": new_distance,
        }
    if current_units > allowed_units + 1e-9:
        return {
            "ok": False,
            "reason": "RISK_EXCEEDED_AFTER_SL_CHANGE",
            "old_distance": old_distance,
            "new_distance": new_distance,
            "current_volume_units": current_units,
            "allowed_volume_units": allowed_units,
        }
    return {
        "ok": True,
        "reason": "SL_CHANGE_WITHIN_APPROVED_RISK",
        "old_distance": old_distance,
        "new_distance": new_distance,
        "current_volume_units": current_units,
        "allowed_volume_units": allowed_units,
    }


def persist_execution_risk_audit_safely(**values):
    try:
        db = SessionLocal()
        row = ExecutionRiskAudit(
            timestamp=values.get("timestamp") or datetime.now(timezone.utc),
            symbol=str(values.get("symbol") or "").upper(),
            event_type=str(values.get("event_type") or "UNKNOWN"),
            source=str(values.get("source") or "application"),
            broker_position_id=(str(values.get("broker_position_id")) if values.get("broker_position_id") is not None else None),
            old_entry=_number(values.get("old_entry")), new_entry=_number(values.get("new_entry")),
            old_sl=_number(values.get("old_sl")), new_sl=_number(values.get("new_sl")),
            volume_units=_number(values.get("volume_units")),
            approved_risk_amount=_number(values.get("approved_risk_amount")),
            resulting_risk_amount=_number(values.get("resulting_risk_amount")),
            approved_risk_percent=_number(values.get("approved_risk_percent")),
            resulting_risk_percent=_number(values.get("resulting_risk_percent")),
            status=str(values.get("status") or "RECORDED"),
            details_json=values.get("details") or {},
        )
        db.add(row)
        db.commit()
        return True
    except Exception as exc:
        print("EXECUTION_RISK_AUDIT_WARNING =", {
            "symbol": values.get("symbol"), "event_type": values.get("event_type"),
            "error_type": type(exc).__name__, "error": str(exc),
        })
        return False
    finally:
        if "db" in locals():
            db.close()
