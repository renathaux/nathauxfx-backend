"""Pure execution-profile rules for the frozen V3B LIVE candidate.

This module does not import the broker connector, mutate account state, or send
orders.  It defines the profile-specific rules that the existing LIVE core must
use when it is eventually wired for V3B.  V1 remains the default path.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone


V3B_EXECUTION_PROFILE = "V3B_M5_FROZEN"
V3B_SETUP_TIMEFRAME = "5m"
V3B_TARGET_RR = 1.90
V3B_PROTECTION_TRIGGER_FRACTION = 0.70
V3B_PROTECTED_STOP_FRACTION = 0.60

_UNAVAILABLE_LIFECYCLE = {
    "CONSUMED",
    "EXPIRED",
    "INVALIDATED",
    "SUBMITTING",
    "RECONCILIATION_REQUIRED",
}


def _utc(value):
    if value in {None, "", "--"}:
        return None
    try:
        if isinstance(value, (int, float)):
            numeric = float(value)
            if numeric > 1e12:
                numeric /= 1000.0
            return datetime.fromtimestamp(numeric, tz=timezone.utc)
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def is_v3b_execution_profile(value):
    """Recognize V3B only from an explicit execution-profile stamp."""
    payload = value if isinstance(value, dict) else {}
    identity = payload.get("setup_identity")
    identity = identity if isinstance(identity, dict) else {}
    profile = (
        payload.get("strategy_execution_profile")
        or identity.get("strategy_execution_profile")
    )
    return str(profile or "").strip().upper() == V3B_EXECUTION_PROFILE


def stamp_v3b_identity(identity):
    stamped = copy.deepcopy(identity or {})
    stamped["strategy_execution_profile"] = V3B_EXECUTION_PROFILE
    stamped["setup_timeframe"] = V3B_SETUP_TIMEFRAME
    return stamped


def validate_frozen_management_contract(payload):
    """Fail closed if the broker payload drifts from the frozen V3B geometry."""
    payload = payload if isinstance(payload, dict) else {}
    checks = {}
    try:
        entry = float(payload.get("entry"))
        sl = float(payload.get("sl"))
        trigger = float(
            payload.get("protection_trigger_price")
            if payload.get("protection_trigger_price") is not None
            else payload.get("tp1")
        )
        tp2 = float(payload.get("tp2"))
        protected = float(payload.get("protected_sl_price"))
        side = str(payload.get("side") or payload.get("action") or "").upper()
        risk = abs(entry - sl)
        reward = abs(tp2 - entry)
        trigger_path = abs(trigger - entry)
        protected_path = abs(protected - entry)
        directional = (
            side == "BUY" and sl < entry < protected <= trigger < tp2
        ) or (
            side == "SELL" and sl > entry > protected >= trigger > tp2
        )
        checks = {
            "profile": is_v3b_execution_profile(payload),
            "directional_levels": directional,
            "target_rr": risk > 0 and abs((reward / risk) - V3B_TARGET_RR) <= 1e-6,
            "trigger_fraction": reward > 0 and abs(
                (trigger_path / reward) - V3B_PROTECTION_TRIGGER_FRACTION
            ) <= 1e-6,
            "protected_fraction": reward > 0 and abs(
                (protected_path / reward) - V3B_PROTECTED_STOP_FRACTION
            ) <= 1e-6,
            "no_partial_close": payload.get(
                "no_partial_close_at_protection_trigger"
            ) is True,
        }
    except (TypeError, ValueError, ZeroDivisionError):
        checks = {"numeric_levels": False}

    return {
        "ok": bool(checks) and all(checks.values()),
        "reason": (
            None
            if checks and all(checks.values())
            else "WAIT_V3B_FROZEN_MANAGEMENT_CONTRACT"
        ),
        "details": {"checks": checks},
    }


def validate_v3b_locked_entry_state(
    symbol,
    side,
    trade_payload,
    broker_positions,
    *,
    active_trade=None,
    now=None,
    last_closed_at=0,
    cooldown_seconds=0,
    setup_id_builder=None,
    lifecycle=None,
):
    """Profile-specific replacement for the V1 15m/EMA locked entry gate.

    V3B intentionally has no 15m EMA or consolidation requirement.  This gate
    preserves execution safety: exact durable identity, closed 5m ordering,
    post-close freshness/cooldown, lifecycle availability, and one-position
    protection.
    """
    payload = trade_payload if isinstance(trade_payload, dict) else {}
    normalized_symbol = str(symbol or "").upper().replace("/", "")
    normalized_side = str(side or "").upper()
    identity = payload.get("setup_identity")
    identity = identity if isinstance(identity, dict) else {}
    now_dt = _utc(now if now is not None else datetime.now(timezone.utc))
    last_close_dt = _utc(last_closed_at) if last_closed_at else None
    bos_close = _utc(
        payload.get("five_m_break_close_time")
        or identity.get("bos_close_timestamp")
    )
    confirmation_close = _utc(
        payload.get("five_m_closed_candle_time")
        or identity.get("confirmation_timestamp")
    )

    details = {
        "symbol": normalized_symbol,
        "side": normalized_side,
        "strategy_execution_profile": V3B_EXECUTION_PROFILE,
        "setup_timeframe": identity.get("setup_timeframe"),
        "five_m_break_close_time": bos_close.isoformat() if bos_close else None,
        "five_m_confirmation_close_time": (
            confirmation_close.isoformat() if confirmation_close else None
        ),
        "previous_position_closed_at": (
            last_close_dt.isoformat() if last_close_dt else None
        ),
        "v1_15m_ema_bypassed": True,
        "v1_consolidation_bypassed": True,
        "v3b_profile_rules_only": True,
    }

    if not is_v3b_execution_profile(payload):
        return {"ok": False, "reason": "WAIT_V3B_EXECUTION_PROFILE", "details": details}
    if normalized_side not in {"BUY", "SELL"}:
        return {"ok": False, "reason": "WAIT_V3B_SIDE", "details": details}
    if str(identity.get("setup_timeframe") or "").lower() != V3B_SETUP_TIMEFRAME:
        return {"ok": False, "reason": "WAIT_V3B_5M_IDENTITY", "details": details}
    if str(identity.get("symbol") or "").upper().replace("/", "") != normalized_symbol:
        return {"ok": False, "reason": "WAIT_V3B_IDENTITY_SYMBOL", "details": details}
    if str(identity.get("direction") or "").upper() != normalized_side:
        return {"ok": False, "reason": "WAIT_V3B_IDENTITY_DIRECTION", "details": details}

    required_identity = {
        "swing_type",
        "swing_timestamp",
        "swing_price",
        "bos_candle_timestamp",
        "bos_level",
        "confirmation_timestamp",
        "indicator_event_id",
        "m5_confirmation_id",
    }
    missing = sorted(
        key for key in required_identity if identity.get(key) in {None, ""}
    )
    if missing:
        details["missing_identity_fields"] = missing
        return {"ok": False, "reason": "WAIT_V3B_DURABLE_IDENTITY", "details": details}

    if active_trade:
        return {"ok": False, "reason": "active position exists", "details": details}
    if any(
        str((position or {}).get("symbol") or "").upper().replace("/", "")
        == normalized_symbol
        for position in (broker_positions or [])
        if isinstance(position, dict)
    ):
        return {"ok": False, "reason": "broker position exists", "details": details}

    if bos_close is None or confirmation_close is None:
        return {"ok": False, "reason": "missing closed 5m timestamps", "details": details}
    if now_dt is None or bos_close > now_dt or confirmation_close > now_dt:
        return {"ok": False, "reason": "setup contains a future candle close", "details": details}
    if confirmation_close <= bos_close:
        return {
            "ok": False,
            "reason": "second 5m confirmation did not close after 5m BOS close",
            "details": details,
        }

    if last_close_dt is not None:
        if bos_close <= last_close_dt or confirmation_close <= last_close_dt:
            return {
                "ok": False,
                "reason": "5m V3B setup predates position close",
                "details": details,
            }
        remaining = float(cooldown_seconds or 0) - (
            now_dt - last_close_dt
        ).total_seconds()
        if remaining > 0:
            details["post_close_cooldown_remaining_seconds"] = round(remaining, 2)
            return {"ok": False, "reason": "post-close cooldown active", "details": details}

    lifecycle = lifecycle if isinstance(lifecycle, dict) else {}
    details["indicator_event_lifecycle"] = copy.deepcopy(lifecycle)
    if lifecycle.get("status") in _UNAVAILABLE_LIFECYCLE:
        return {
            "ok": False,
            "reason": f"indicator event unavailable: {lifecycle.get('status')}",
            "details": details,
        }

    setup_id = payload.get("signal_setup_id")
    if not setup_id:
        return {"ok": False, "reason": "missing setup fingerprint", "details": details}
    if callable(setup_id_builder):
        expected = setup_id_builder(payload, normalized_side)
        details["recalculated_signal_setup_id"] = expected
        if expected != setup_id:
            return {"ok": False, "reason": "setup fingerprint changed", "details": details}

    management = validate_frozen_management_contract(payload)
    details["frozen_management"] = copy.deepcopy(management)
    if not management.get("ok"):
        return {
            "ok": False,
            "reason": management.get("reason"),
            "details": details,
        }

    return {"ok": True, "reason": None, "details": details}


def stamp_active_trade_with_v3b_profile(trade, payload):
    """Persist the exact profile fields needed by trigger/protection management."""
    target = trade if isinstance(trade, dict) else {}
    source = payload if isinstance(payload, dict) else {}
    target.update({
        "strategy_execution_profile": V3B_EXECUTION_PROFILE,
        "live_strategy_model": source.get("live_strategy_model"),
        "protection_trigger_price": source.get("protection_trigger_price"),
        "protected_sl_price": source.get("protected_sl_price"),
        "protection_trigger_tp2_fraction": V3B_PROTECTION_TRIGGER_FRACTION,
        "protected_stop_tp2_fraction": V3B_PROTECTED_STOP_FRACTION,
        "no_partial_close_at_protection_trigger": True,
        "v3b_frozen_target_rr": V3B_TARGET_RR,
        "setup_identity": stamp_v3b_identity(source.get("setup_identity") or {}),
    })
    if source.get("protection_trigger_price") is not None:
        target["tp1"] = source.get("protection_trigger_price")
    return target
