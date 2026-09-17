"""Account-scoped post-entry management for Strategy Studio LIVE trades.

Only lifecycles belonging to the pinned/selected cTrader account may be touched.
TP2 remains broker-side. TP1 handling is idempotent through durable lifecycle
fields and ambiguous broker outcomes fail closed.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

from ctrader_connector import close_position, modify_position_stop_loss
from db import SessionLocal
from models import StrategySetupLifecycle


_OPEN_STATUSES = {"CONSUMED", "SUBMITTING", "RECONCILIATION_REQUIRED"}


def _factory(session_factory=None):
    return session_factory or SessionLocal


def _scope(account_identity) -> str:
    if account_identity is None or not getattr(account_identity, "scope", None):
        raise ValueError("account identity is required")
    return str(account_identity.scope)


def _account_id(account_identity) -> str:
    value = str(getattr(account_identity, "account_id", "") or "")
    if not value:
        raise ValueError("account identity is required")
    return value


def _position_id(position):
    if not isinstance(position, dict):
        return None
    value = position.get("position_id") or position.get("broker_position_id") or position.get("id")
    return str(value) if value not in (None, "") else None


def _position_map(open_positions):
    result = {}
    for position in open_positions or []:
        pid = _position_id(position)
        if pid:
            result[pid] = position
    return result


def _float(*values):
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            return number
    return None


def _side(row, position):
    return str((position or {}).get("side") or row.direction or "").upper()


def _current_price(symbol, side, position, prices):
    quote = (prices or {}).get(symbol) or {}
    preferred = quote.get("bid") if side == "BUY" else quote.get("ask") if side == "SELL" else None
    return _float(preferred, (position or {}).get("current_price"), (position or {}).get("currentPrice"), (position or {}).get("price"))


def _levels(row, position):
    definition = row.definition_snapshot if isinstance(row.definition_snapshot, dict) else {}
    tp1 = definition.get("tp1") if isinstance(definition.get("tp1"), dict) else {}
    side = _side(row, position)
    entry = _float((position or {}).get("entry"), (position or {}).get("entry_price"), (position or {}).get("openPrice"))
    stop = _float((position or {}).get("sl"), (position or {}).get("stop_loss"), (position or {}).get("stopLoss"))
    tp2 = _float((position or {}).get("tp2"), (position or {}).get("take_profit"), (position or {}).get("takeProfit"))
    try:
        target_r = float(tp1.get("target_r"))
    except (TypeError, ValueError):
        target_r = None
    try:
        protection_r = float(tp1.get("protection_r"))
    except (TypeError, ValueError):
        protection_r = None
    try:
        close_percent = float(tp1.get("close_percent"))
    except (TypeError, ValueError):
        close_percent = None

    target = protected = None
    if entry is not None and stop is not None:
        risk_distance = abs(entry - stop)
        if target_r is not None and math.isfinite(target_r):
            target = entry + risk_distance * target_r if side == "BUY" else entry - risk_distance * target_r
        if protection_r is not None and math.isfinite(protection_r):
            protected = entry + risk_distance * protection_r if side == "BUY" else entry - risk_distance * protection_r
    return {
        "enabled": bool(tp1.get("enabled")),
        "target": target,
        "protected": protected,
        "close_percent": close_percent,
        "tp2": tp2,
        "side": side,
    }


def _target_hit(side, price, target):
    if price is None or target is None:
        return False
    return (side == "BUY" and price >= target) or (side == "SELL" and price <= target)


def _partial_volume(row, position, close_percent):
    total = _float(row.initial_volume_units, (position or {}).get("volume_units"), (position or {}).get("volume"))
    if total is None or total <= 0 or close_percent is None or not (0 < close_percent <= 100):
        return None
    return max(1, int(round(total * close_percent / 100.0)))


def _is_ambiguous(result):
    payload = result if isinstance(result, dict) else {}
    category = str(payload.get("broker_result") or payload.get("status") or "").upper()
    return bool(payload.get("broker_order_sent")) and category in {"AMBIGUOUS", "UNKNOWN", "RECONCILIATION_REQUIRED"}


def _rows_for_owner(session, owner_id):
    return session.query(StrategySetupLifecycle).filter(
        StrategySetupLifecycle.owner_id == str(owner_id),
        StrategySetupLifecycle.status.in_(_OPEN_STATUSES),
        StrategySetupLifecycle.broker_position_id.is_not(None),
    ).all()


def account_has_managed_position(owner_id, account_identity, open_positions, *, session_factory=None) -> bool:
    """Return True only for an open broker position owned by this Studio/account scope."""
    factory = _factory(session_factory)
    scope = _scope(account_identity)
    account_id = _account_id(account_identity)
    positions = _position_map(open_positions)
    if not positions:
        return False
    with factory() as session:
        row = session.query(StrategySetupLifecycle).filter(
            StrategySetupLifecycle.owner_id == str(owner_id),
            StrategySetupLifecycle.account_id == account_id,
            StrategySetupLifecycle.account_scope == scope,
            StrategySetupLifecycle.status.in_(_OPEN_STATUSES),
            StrategySetupLifecycle.broker_position_id.is_not(None),
        ).first()
        if row is None:
            return False
        return str(row.broker_position_id) in positions


def suspend_account_management(owner_id, account_identity, open_positions, *, session_factory=None) -> dict:
    factory = _factory(session_factory)
    scope = _scope(account_identity)
    account_id = _account_id(account_identity)
    positions = _position_map(open_positions)
    now = datetime.now(timezone.utc)
    suspended = 0
    with factory() as session:
        rows = session.query(StrategySetupLifecycle).filter(
            StrategySetupLifecycle.owner_id == str(owner_id),
            StrategySetupLifecycle.account_id == account_id,
            StrategySetupLifecycle.account_scope == scope,
            StrategySetupLifecycle.status.in_(_OPEN_STATUSES),
            StrategySetupLifecycle.broker_position_id.is_not(None),
        ).with_for_update().all()
        for row in rows:
            if str(row.broker_position_id) not in positions:
                continue
            if row.management_suspended_at is None:
                row.management_suspended_at = now
                row.updated_at = now
                suspended += 1
        session.commit()
    return {"owner_id": str(owner_id), "account_scope": scope, "suspended": suspended, "actions": []}


def _terminalize_missing(rows, positions, now):
    count = 0
    for row in rows:
        if str(row.broker_position_id) in positions:
            continue
        if str(row.status).upper() == "RECONCILIATION_REQUIRED":
            continue
        row.status = "CLOSED"
        row.management_suspended_at = None
        row.updated_at = now
        count += 1
    return count


def _partial_close(row, position, levels, now, *, catchup):
    volume = _partial_volume(row, position, levels["close_percent"])
    if volume is None:
        return {"action": "TP1_PARTIAL_CLOSE", "status": "BLOCKED", "reason": "INVALID_PARTIAL_CLOSE_VOLUME"}
    result = close_position(str(row.broker_position_id), volume=volume)
    if not isinstance(result, dict) or not result.get("ok"):
        if _is_ambiguous(result):
            row.status = "RECONCILIATION_REQUIRED"
            row.updated_at = now
            return {
                "action": "TP1_CATCHUP_PARTIAL_CLOSE" if catchup else "TP1_PARTIAL_CLOSE_AND_PROTECT",
                "status": "RECONCILIATION_REQUIRED",
                "broker_result": result,
            }
        return {
            "action": "TP1_CATCHUP_PARTIAL_CLOSE" if catchup else "TP1_PARTIAL_CLOSE_AND_PROTECT",
            "status": "FAILED",
            "broker_result": result,
        }

    row.tp1_completed_at = now
    row.updated_at = now
    if catchup:
        return {"action": "TP1_CATCHUP_PARTIAL_CLOSE", "status": "COMPLETED", "volume": volume}

    protected = levels.get("protected")
    if protected is None:
        return {"action": "TP1_PARTIAL_CLOSE_AND_PROTECT", "status": "PROTECTION_FAILED", "volume": volume}
    protection = modify_position_stop_loss(
        str(row.broker_position_id),
        protected,
        take_profit_price=levels.get("tp2"),
    )
    if not isinstance(protection, dict) or not protection.get("ok"):
        if _is_ambiguous(protection):
            row.status = "RECONCILIATION_REQUIRED"
            row.updated_at = now
            return {
                "action": "TP1_PARTIAL_CLOSE_AND_PROTECT",
                "status": "RECONCILIATION_REQUIRED",
                "volume": volume,
                "broker_result": protection,
            }
        return {
            "action": "TP1_PARTIAL_CLOSE_AND_PROTECT",
            "status": "PROTECTION_FAILED",
            "volume": volume,
            "broker_result": protection,
        }
    row.protection_applied_at = now
    row.updated_at = now
    return {
        "action": "TP1_PARTIAL_CLOSE_AND_PROTECT",
        "status": "COMPLETED",
        "volume": volume,
        "protected_sl": protected,
    }


def _manage(owner_id, account_identity, open_positions, prices, *, resume, session_factory=None):
    factory = _factory(session_factory)
    scope = _scope(account_identity)
    account_id = _account_id(account_identity)
    positions = _position_map(open_positions)
    now = datetime.now(timezone.utc)
    actions = []
    terminalized = 0

    with factory() as session:
        all_owner_rows = _rows_for_owner(session, owner_id)
        rows = [
            row for row in all_owner_rows
            if str(row.account_id) == account_id and str(row.account_scope) == scope
        ]
        if not rows and all_owner_rows:
            session.rollback()
            return {
                "owner_id": str(owner_id),
                "account_scope": scope,
                "status": "INACTIVE_ACCOUNT_NOT_MANAGED",
                "actions": [],
                "terminalized": 0,
            }

        terminalized = _terminalize_missing(rows, positions, now)
        for row in rows:
            position = positions.get(str(row.broker_position_id))
            if position is None or str(row.status).upper() not in _OPEN_STATUSES:
                continue

            was_suspended = row.management_suspended_at is not None
            if resume:
                row.management_suspended_at = None
                row.updated_at = now
            elif was_suspended:
                continue

            definition = row.definition_snapshot if isinstance(row.definition_snapshot, dict) else {}
            tp1_definition = definition.get("tp1") if isinstance(definition.get("tp1"), dict) else {}
            if not tp1_definition.get("enabled") or row.tp1_completed_at is not None:
                continue

            levels = _levels(row, position)
            price = _current_price(str(row.symbol), levels["side"], position, prices)
            if not _target_hit(levels["side"], price, levels["target"]):
                continue
            action = _partial_close(row, position, levels, now, catchup=bool(resume and was_suspended))
            actions.append(action)

        session.commit()

    return {
        "owner_id": str(owner_id),
        "account_scope": scope,
        "status": "OK",
        "actions": actions,
        "terminalized": terminalized,
    }


def resume_account_management(owner_id, account_identity, open_positions, prices, *, session_factory=None) -> dict:
    return _manage(
        owner_id, account_identity, open_positions, prices,
        resume=True, session_factory=session_factory,
    )


def manage_selected_account_positions(owner_id, account_identity, open_positions, prices, *, session_factory=None) -> dict:
    return _manage(
        owner_id, account_identity, open_positions, prices,
        resume=False, session_factory=session_factory,
    )
