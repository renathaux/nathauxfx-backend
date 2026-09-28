"""Account-scoped post-entry management for Strategy Studio LIVE trades.

Only lifecycles belonging to the pinned/selected cTrader account may be touched.
TP2 remains broker-side. TP1 handling is idempotent through durable lifecycle
fields and ambiguous broker outcomes fail closed.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

from ctrader_connector import (
    close_position,
    get_ctrader_symbol_risk_metadata,
    modify_position_stop_loss,
)
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
    snapshot = row.execution_snapshot or {}
    side = str(row.direction).upper()
    entry = _float(snapshot.get("entry"))
    stop = _float(snapshot.get("initial_sl"))
    tp2 = _float(snapshot.get("tp2"))
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
    protection_trigger_method = str(
        tp1.get("protection_trigger_method") or "CANDLE_CLOSE"
    ).upper()
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
        "protection_trigger_method": protection_trigger_method,
        "step_levels": step_levels,
    }


def _target_hit(side, price, target, position=None):
    if target is None:
        return False
    if price is not None and (
        (side == "BUY" and price >= target)
        or (side == "SELL" and price <= target)
    ):
        return True

    # Broker sync maintains trusted post-entry wick extremes. Use them as a
    # catch-up proof so a fast TP1 touch is not lost between 15s management polls.
    favorable_extreme = _float(
        (position or {}).get("trusted_tp1_high") if side == "BUY" else None,
        (position or {}).get("current_high") if side == "BUY" else None,
        (position or {}).get("trusted_tp1_low") if side == "SELL" else None,
        (position or {}).get("current_low") if side == "SELL" else None,
    )
    if favorable_extreme is None:
        return False
    return (
        favorable_extreme >= target
        if side == "BUY"
        else favorable_extreme <= target
    )


def _closed_price(symbol, closed_prices, *, after=None):
    payload = (closed_prices or {}).get(str(symbol)) or {}
    if not isinstance(payload, dict):
        return None if after is not None else _float(payload)

    if after is not None:
        raw_closed_at = payload.get("closed_at")
        if raw_closed_at in (None, ""):
            return None
        try:
            closed_at = datetime.fromisoformat(str(raw_closed_at).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if closed_at.tzinfo is None:
            closed_at = closed_at.replace(tzinfo=timezone.utc)
        if after.tzinfo is None:
            after = after.replace(tzinfo=timezone.utc)
        if closed_at <= after:
            return None

    return _float(payload.get("close"), payload.get("price"))


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
    total = _float(
        row.initial_volume_units,
        (position or {}).get("volume_units"),
        (position or {}).get("volume"),
    )
    if total is None or total <= 0 or close_percent is None or not (0 < close_percent <= 100):
        return None

    target = float(total) * float(close_percent) / 100.0
    metadata = (position or {}).get("symbol_metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    if not metadata:
        try:
            metadata = get_ctrader_symbol_risk_metadata(str(row.symbol)) or {}
        except Exception:
            metadata = {}

    step = _float(
        (position or {}).get("volume_step_units"),
        metadata.get("volume_step_units"),
    )
    minimum = _float(
        (position or {}).get("min_volume_units"),
        metadata.get("min_volume_units"),
    )

    if step is not None and step > 0:
        units = int(math.floor(target / step) * step)
    else:
        units = int(round(target))

    minimum_units = int(math.ceil(minimum)) if minimum is not None and minimum > 0 else 1
    if units < minimum_units:
        # Do not silently close more than the configured partial percentage
        # just to satisfy a broker minimum.
        return None
    units = min(units, int(total))
    if units <= 0:
        return None
    if close_percent < 100 and units >= int(total):
        return None
    return units


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


def managed_owner_for_account(account_identity, open_positions, *, session_factory=None) -> str | None:
    """Resolve the unique Studio owner of an existing open broker position.

    This is intentionally independent of the LIVE handoff gate so turning off
    future Studio entries cannot abandon management of an already-open Studio
    position.
    """
    factory = _factory(session_factory)
    scope = _scope(account_identity)
    account_id = _account_id(account_identity)
    positions = _position_map(open_positions)
    if not positions:
        return None

    with factory() as session:
        rows = session.query(StrategySetupLifecycle).filter(
            StrategySetupLifecycle.account_id == account_id,
            StrategySetupLifecycle.account_scope == scope,
            StrategySetupLifecycle.status.in_(_OPEN_STATUSES),
            StrategySetupLifecycle.broker_position_id.is_not(None),
        ).all()
        owners = {
            str(row.owner_id)
            for row in rows
            if str(row.broker_position_id) in positions
        }
    if not owners:
        return None
    if len(owners) != 1:
        raise RuntimeError("STRATEGY_STUDIO_MANAGEMENT_OWNER_AMBIGUOUS")
    return next(iter(owners))


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


def _state(row, **updates):
    row.management_state = {**(row.management_state or {}), **updates}
    return row.management_state


def _confirm_observation(row, position, now):
    state = row.management_state or {}
    current_sl = _float(position.get("sl"), position.get("stop_loss"), position.get("stopLoss"))
    target = _float(state.get("target_protected_sl"))
    if target is not None and current_sl is not None and not _is_more_protective(row.direction, current_sl, target):
        _state(row, protection_state="CONFIRMED", broker_confirmed_sl=current_sl)
        row.protection_applied_at = now
    remaining = _float(position.get("volume_units"), position.get("volume"))
    before = _float(state.get("tp1_volume_before"))
    requested = _float(state.get("tp1_requested_volume"))
    if state.get("tp1_partial_close_requested") and not state.get("tp1_partial_close_confirmed"):
        if remaining is not None and before is not None and requested is not None and remaining <= before - requested:
            row.tp1_completed_at = now
            _state(row, tp1_partial_close_confirmed=True, tp1_closed_volume=before - remaining, tp1_state="PARTIAL_CLOSED", management_error=None)
            if row.status == "RECONCILIATION_REQUIRED":
                row.status = "CONSUMED"


def _manage(owner_id, account_identity, open_positions, prices, *, resume, closed_prices=None, session_factory=None):
    factory = _factory(session_factory)
    scope, account_id = _scope(account_identity), _account_id(account_identity)
    positions = _position_map(open_positions)
    now = datetime.now(timezone.utc)
    actions, terminalized = [], 0
    with factory() as session:
        all_rows = _rows_for_owner(session, owner_id)
        ids = [row.setup_id for row in all_rows if str(row.account_id) == account_id and row.account_scope == scope]
        if not ids and all_rows:
            return {"owner_id": str(owner_id), "account_scope": scope, "status": "INACTIVE_ACCOUNT_NOT_MANAGED", "actions": [], "terminalized": 0}
        for setup_id in ids:
            # Re-read under a database lock, including after an earlier row committed.
            row = session.query(StrategySetupLifecycle).filter_by(setup_id=setup_id).populate_existing().with_for_update().one()
            position = positions.get(str(row.broker_position_id))
            if position is None:
                terminalized += _terminalize_missing([row], positions, now)
                session.commit()
                continue
            was_suspended = row.management_suspended_at is not None
            if resume:
                row.management_suspended_at = None
            elif was_suspended:
                session.rollback()
                continue
            # Legacy lifecycle proof permits freezing its first observed levels, never
            # attaching an unowned broker trade to a currently selected strategy.
            if row.execution_snapshot is None:
                row.execution_snapshot = {"entry": _float(position.get("entry"), position.get("entry_price")), "initial_sl": _float(position.get("sl"), position.get("stop_loss")), "tp2": _float(position.get("tp2"), position.get("take_profit")), "strategy_id": row.strategy_id, "strategy_name": row.strategy_id, "legacy_snapshot": True}
            if row.initial_volume_units is None:
                row.initial_volume_units = _float(position.get("volume_units"), position.get("volume"))
            _confirm_observation(row, position, now)
            _state(row, last_management_timestamp=now.isoformat())
            row.updated_at = now
            levels = _levels(row, position)
            if not levels["enabled"]:
                session.commit()
                continue
            state = row.management_state or {}
            price = _current_price(row.symbol, levels["side"], position, prices)
            if row.tp1_completed_at is None:
                if state.get("tp1_partial_close_requested") or row.tp1_requested_at is not None or row.status == "RECONCILIATION_REQUIRED":
                    session.commit()
                    continue
                if not _target_hit(levels["side"], price, levels["target"], position):
                    session.commit()
                    continue
                volume = _partial_volume(row, position, levels["close_percent"])
                if volume is None:
                    _state(row, management_error="INVALID_PARTIAL_CLOSE_VOLUME")
                    session.commit()
                    actions.append({"action": "TP1_PARTIAL_CLOSE", "status": "BLOCKED", "reason": "INVALID_PARTIAL_CLOSE_VOLUME"})
                    continue
                claimed = session.query(StrategySetupLifecycle).filter(
                    StrategySetupLifecycle.setup_id == setup_id,
                    StrategySetupLifecycle.tp1_requested_at.is_(None),
                ).update({StrategySetupLifecycle.tp1_requested_at: now}, synchronize_session=False)
                if claimed != 1:
                    session.rollback()
                    continue
                _state(row, tp1_triggered=True, tp1_partial_close_requested=True, tp1_requested_volume=volume, tp1_volume_before=_float(position.get("volume_units"), position.get("volume")), tp1_state="PENDING")
                # Commit before sending: crashes/timeouts must never repeat a close.
                session.commit()
                try:
                    result = close_position(str(row.broker_position_id), volume=volume)
                except Exception as exc:
                    result = {"ok": False, "reason": str(exc)}
                row = session.query(StrategySetupLifecycle).filter_by(setup_id=setup_id).populate_existing().with_for_update().one()
                if not isinstance(result, dict) or not result.get("ok"):
                    row.status = "RECONCILIATION_REQUIRED"
                    _state(row, tp1_state="RECONCILIATION_REQUIRED", management_error=(result or {}).get("reason") or "PARTIAL_CLOSE_UNCONFIRMED")
                    actions.append({"action": "TP1_PARTIAL_CLOSE", "status": "RECONCILIATION_REQUIRED"})
                else:
                    # Broker ACK is persisted; next position sync confirms actual volume.
                    actions.append({"action": "TP1_CATCHUP_PARTIAL_CLOSE" if resume and was_suspended else "TP1_PARTIAL_CLOSE", "status": "PENDING", "volume": volume, "protection_deferred": True})
                session.commit()
                continue
            _state(row, tp1_state="PARTIAL_CLOSED", tp1_partial_close_confirmed=True)
            state = row.management_state or {}
            if state.get("protection_state") == "PENDING":
                session.commit()
                continue
            step = None
            if levels["protection_mode"] == "TP2_STEPS":
                protection_price = _closed_price(row.symbol, closed_prices, after=row.tp1_completed_at) if levels["protection_trigger_method"] == "CANDLE_CLOSE" else price
                step = _step_for_price(levels, protection_price)
                desired = step.get("protected") if step else None
                index = levels["step_levels"].index(step) if step else None
                if index is not None and index < state.get("protection_step_index", -1):
                    desired = None
            else:
                desired, index = levels["protected"], 0
            current_sl = _float(position.get("sl"), position.get("stop_loss"))
            previous_target = _float(state.get("target_protected_sl"))
            if desired is None or not _is_more_protective(row.direction, current_sl, desired) or (previous_target is not None and desired != previous_target and not _is_more_protective(row.direction, previous_target, desired)):
                session.commit()
                continue
            _state(row, protection_state="PENDING", protection_requested=True, protection_step_index=index, protection_trigger_percent=step.get("trigger_percent") if step else None, protection_secure_percent=step.get("secure_percent") if step else None, target_protected_sl=desired, management_error=None)
            session.commit()
            try:
                result = modify_position_stop_loss(str(row.broker_position_id), desired, take_profit_price=levels["tp2"])
            except Exception as exc:
                result = {"ok": False, "reason": str(exc)}
            row = session.query(StrategySetupLifecycle).filter_by(setup_id=setup_id).populate_existing().with_for_update().one()
            if isinstance(result, dict) and result.get("confirmed_by_readback"):
                _confirm_observation(row, {"sl": result.get("stop_loss")}, now)
            elif not isinstance(result, dict) or not result.get("ok"):
                _state(row, protection_state="FAILED", management_error=(result or {}).get("reason") or "STOP_AMEND_UNCONFIRMED")
            actions.append({"action": "TP2_STEP_PROTECTION", "status": (row.management_state or {}).get("protection_state"), "protected_sl": desired, "trigger_percent": step.get("trigger_percent") if step else None, "secure_percent": step.get("secure_percent") if step else None, "protection_trigger_method": levels["protection_trigger_method"]})
            session.commit()
    return {"owner_id": str(owner_id), "account_scope": scope, "status": "OK", "actions": actions, "terminalized": terminalized}


def resume_account_management(owner_id, account_identity, open_positions, prices, *, closed_prices=None, session_factory=None) -> dict:
    return _manage(
        owner_id, account_identity, open_positions, prices,
        resume=True, closed_prices=closed_prices, session_factory=session_factory,
    )


def manage_selected_account_positions(owner_id, account_identity, open_positions, prices, *, closed_prices=None, session_factory=None) -> dict:
    return _manage(
        owner_id, account_identity, open_positions, prices,
        resume=False, closed_prices=closed_prices, session_factory=session_factory,
    )


def managed_position_ids(owner_id, account_identity, open_positions, *, session_factory=None) -> set[str]:
    """Return open broker position ids owned by Strategy Studio in this account scope."""
    factory = _factory(session_factory)
    scope = _scope(account_identity)
    account_id = _account_id(account_identity)
    positions = _position_map(open_positions)
    if not positions:
        return set()
    with factory() as session:
        rows = session.query(StrategySetupLifecycle).filter(
            StrategySetupLifecycle.owner_id == str(owner_id),
            StrategySetupLifecycle.account_id == account_id,
            StrategySetupLifecycle.account_scope == scope,
            StrategySetupLifecycle.status.in_(_OPEN_STATUSES),
            StrategySetupLifecycle.broker_position_id.is_not(None),
        ).all()
        return {
            str(row.broker_position_id)
            for row in rows
            if str(row.broker_position_id) in positions
        }


def managed_position_states(owner_id, account_identity, open_positions, *, session_factory=None):
    """Read-only presentation from durable ownership; never adopts manual trades."""
    positions, result = _position_map(open_positions), {}
    with _factory(session_factory)() as session:
        rows = session.query(StrategySetupLifecycle).filter(
            StrategySetupLifecycle.owner_id == str(owner_id),
            StrategySetupLifecycle.account_id == _account_id(account_identity),
            StrategySetupLifecycle.account_scope == _scope(account_identity),
            StrategySetupLifecycle.status.in_(_OPEN_STATUSES),
        ).all()
        for row in rows:
            pid = str(row.broker_position_id)
            if pid not in positions:
                continue
            position, snapshot, state = positions[pid], row.execution_snapshot or {}, row.management_state or {}
            actual_sl = _float(position.get("sl"), position.get("stop_loss"))
            target = _float(state.get("target_protected_sl"))
            if state.get("protection_state") == "CONFIRMED" and (actual_sl is None or _is_more_protective(row.direction, actual_sl, target)):
                state = {**state, "protection_state": "FAILED", "broker_confirmed_sl": None, "management_error": "BROKER_STOP_BELOW_TARGET"}
            levels = _levels(row, position)
            index, steps = state.get("protection_step_index", -1), levels["step_levels"]
            next_step = steps[index + 1] if index + 1 < len(steps) else None
            result[pid] = {
                **state, "execution_source": "STRATEGY_STUDIO", "management_mode": "STUDIO_MANAGED",
                "strategy_id": row.strategy_id, "strategy_name": snapshot.get("strategy_name") or row.strategy_id,
                "execution_snapshot": snapshot, "entry": levels["entry"], "initial_sl": snapshot.get("initial_sl"),
                "tp1": levels["target"], "tp2": levels["tp2"],
                "tp1_trigger_percent": ((row.definition_snapshot or {}).get("tp1", {}).get("target_r") or 0) * 100 if levels["target_basis"] == "TP2_DISTANCE" else None,
                "tp1_partial_close_percent": levels["close_percent"],
                "tp1_partial_close_requested": bool(state.get("tp1_partial_close_requested")),
                "tp1_partial_close_confirmed": bool(state.get("tp1_partial_close_confirmed") or row.tp1_completed_at),
                "protection_requested": bool(state.get("protection_requested")), "tp1_definition": (row.definition_snapshot or {}).get("tp1"),
                "tp1_state": state.get("tp1_state") or ("PARTIAL_CLOSED" if row.tp1_completed_at else "NOT_HIT"),
                "protection_state": state.get("protection_state", "NOT_REQUESTED"),
                **{key: state.get(key) for key in ("protection_step_index", "protection_trigger_percent", "protection_secure_percent", "target_protected_sl", "broker_confirmed_sl", "management_error")},
                "broker_sl": _float(position.get("sl"), position.get("stop_loss")),
                "next_protection_trigger": next_step.get("trigger") if next_step else None,
                "management_status": "SUSPENDED" if row.management_suspended_at else row.status,
                "protection_trigger_method": levels["protection_trigger_method"],
                "protection_method": levels["protection_mode"], "protection_ladder": steps,
            }
    return result
