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

    target_basis = str(tp1.get("target_basis") or "SL_DISTANCE").upper()
    protection_mode = str(tp1.get("protection_mode") or "FIXED").upper()
    raw_steps = tp1.get("protection_steps") if isinstance(tp1.get("protection_steps"), list) else []

    target = protected = None
    step_levels = []
    if entry is not None:
        if target_r is not None and math.isfinite(target_r):
            if target_basis == "TP2_DISTANCE" and tp2 is not None:
                target = entry + (tp2 - entry) * target_r
            elif stop is not None:
                risk_distance = abs(entry - stop)
                target = entry + risk_distance * target_r if side == "BUY" else entry - risk_distance * target_r

        if protection_mode == "FIXED" and protection_r is not None and math.isfinite(protection_r):
            if target_basis == "TP2_DISTANCE" and tp2 is not None:
                protected = entry + (tp2 - entry) * protection_r
            elif stop is not None:
                risk_distance = abs(entry - stop)
                protected = entry + risk_distance * protection_r if side == "BUY" else entry - risk_distance * protection_r

        if protection_mode == "TP2_STEPS" and tp2 is not None:
            path = tp2 - entry
            for item in raw_steps:
                if not isinstance(item, dict):
                    continue
                trigger_percent = _float(item.get("trigger_percent"))
                secure_percent = _float(item.get("secure_percent"))
                if trigger_percent is None or secure_percent is None:
                    continue
                step_levels.append({
                    "trigger_percent": trigger_percent,
                    "secure_percent": secure_percent,
                    "trigger": entry + path * trigger_percent / 100.0,
                    "protected": entry + path * secure_percent / 100.0,
                })
            step_levels.sort(key=lambda item: item["trigger_percent"])

    return {
        "enabled": bool(tp1.get("enabled")),
        "target": target,
        "protected": protected,
        "close_percent": close_percent,
        "tp2": tp2,
        "side": side,
        "entry": entry,
        "current_sl": stop,
        "target_basis": target_basis,
        "protection_mode": protection_mode,
        "step_levels": step_levels,
    }


def _target_hit(side, price, target):
    if price is None or target is None:
        return False
    return (side == "BUY" and price >= target) or (side == "SELL" and price <= target)


def _step_for_price(levels, price):
    if price is None or levels.get("protection_mode") != "TP2_STEPS":
        return None
    entry = _float(levels.get("entry"))
    tp2 = _float(levels.get("tp2"))
    if entry is None or tp2 is None or tp2 == entry:
        return None

    progress_percent = (float(price) - entry) / (tp2 - entry) * 100.0
    reached = [
        item for item in (levels.get("step_levels") or [])
        if progress_percent + 1e-9 >= float(item.get("trigger_percent") or 0.0)
    ]
    if not reached:
        return None
    return max(reached, key=lambda item: float(item.get("trigger_percent") or 0.0))


def _is_more_protective(side, current_sl, desired_sl):
    if desired_sl is None:
        return False
    if current_sl is None:
        return True
    return desired_sl > current_sl if side == "BUY" else desired_sl < current_sl


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


def _partial_close(row, position, levels, price, now, *, catchup):
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

    step = None
    if levels.get("protection_mode") == "TP2_STEPS":
        step = _step_for_price(levels, price)
        protected = step.get("protected") if step else None
    else:
        protected = levels.get("protected")

    if protected is None:
        return {
            "action": "TP1_PARTIAL_CLOSE_AND_PROTECT",
            "status": "PROTECTION_FAILED",
            "volume": volume,
            "reason": "PROTECTION_LEVEL_UNAVAILABLE",
        }
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
        "trigger_percent": step.get("trigger_percent") if step else None,
        "secure_percent": step.get("secure_percent") if step else None,
    }


def _advance_step_protection(row, position, levels, price, now):
    step = _step_for_price(levels, price)
    if step is None:
        return None
    desired = _float(step.get("protected"))
    current_sl = _float(
        (position or {}).get("sl"),
        (position or {}).get("stop_loss"),
        (position or {}).get("stopLoss"),
    )
    if not _is_more_protective(levels.get("side"), current_sl, desired):
        return None

    result = modify_position_stop_loss(
        str(row.broker_position_id),
        desired,
        take_profit_price=levels.get("tp2"),
    )
    if not isinstance(result, dict) or not result.get("ok"):
        if _is_ambiguous(result):
            row.status = "RECONCILIATION_REQUIRED"
            row.updated_at = now
            return {
                "action": "TP2_STEP_PROTECTION",
                "status": "RECONCILIATION_REQUIRED",
                "trigger_percent": step.get("trigger_percent"),
                "secure_percent": step.get("secure_percent"),
                "broker_result": result,
            }
        return {
            "action": "TP2_STEP_PROTECTION",
            "status": "FAILED",
            "trigger_percent": step.get("trigger_percent"),
            "secure_percent": step.get("secure_percent"),
            "broker_result": result,
        }

    row.protection_applied_at = now
    row.updated_at = now
    return {
        "action": "TP2_STEP_PROTECTION",
        "status": "COMPLETED",
        "trigger_percent": step.get("trigger_percent"),
        "secure_percent": step.get("secure_percent"),
        "protected_sl": desired,
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
            if not tp1_definition.get("enabled"):
                continue

            levels = _levels(row, position)
            price = _current_price(str(row.symbol), levels["side"], position, prices)

            if row.tp1_completed_at is None:
                if not _target_hit(levels["side"], price, levels["target"]):
                    continue
                action = _partial_close(
                    row, position, levels, price, now,
                    catchup=bool(resume and was_suspended),
                )
                actions.append(action)
                continue

            if levels.get("protection_mode") == "TP2_STEPS":
                action = _advance_step_protection(
                    row, position, levels, price, now
                )
                if action is not None:
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
