"""Double-gated broker handoff adapter for the frozen V3B LIVE candidate.

This module is intentionally dependency-injected: it never imports cTrader or the
broker execution core.  A runtime caller must explicitly provide the existing
LIVE executor, and the handoff remains blocked unless all three conditions are
true:

1. V3B strategy evaluation is explicitly enabled.
2. The separate V3B broker-handoff kill switch is explicitly enabled.
3. The normal durable LIVE Auto preference is already enabled.

Keeping the broker-handoff switch separate from strategy evaluation allows the
V3B candidate to be observed/validated without making an order path reachable.
The module also stamps the frozen management contract onto the payload so a
future runtime integration cannot silently fall back to legacy TP1/protection
math.
"""
from __future__ import annotations

import copy
import os

from services.live_v3b_service import (
    LIVE_V3B_MODEL,
    build_live_v3b_execution_payload,
    live_v3b_enabled,
)


V3B_EXECUTION_PROFILE = "V3B_M5_FROZEN"
V3B_BROKER_HANDOFF_ENV = "V3B_LIVE_BROKER_HANDOFF_ENABLED"
V3B_TARGET_RR = 1.90
V3B_PROTECTION_TRIGGER_FRACTION = 0.70
V3B_PROTECTED_STOP_FRACTION = 0.60
_TRUE_VALUES = {"1", "true", "yes", "on"}


def v3b_broker_handoff_enabled(environ=None):
    """Return True only for an explicit broker-handoff opt in."""
    source = os.environ if environ is None else environ
    return str(source.get(V3B_BROKER_HANDOFF_ENV, "")).strip().lower() in _TRUE_VALUES


def _blocked(reason, *, payload=None, details=None):
    return {
        "ok": False,
        "submitted": False,
        "reason": reason,
        "payload": copy.deepcopy(payload),
        "details": copy.deepcopy(details or {}),
        "execution_profile": V3B_EXECUTION_PROFILE,
    }


def _matches(value, expected, tolerance=1e-9):
    try:
        return abs(float(value) - float(expected)) <= tolerance
    except (TypeError, ValueError):
        return False


def build_v3b_broker_core_payload(candidate):
    """Build and freeze the exact payload contract expected by the LIVE core.

    No broker action happens here.  The returned payload deliberately carries
    both the trigger price and the protected-stop price because the legacy LIVE
    core historically derives those values from different percentages.
    """
    handoff = build_live_v3b_execution_payload(candidate)
    if not handoff.get("ok"):
        return _blocked(
            handoff.get("reason") or "WAIT_V3B_LIVE_PAYLOAD",
            details=handoff.get("details"),
        )

    payload = copy.deepcopy(handoff.get("payload") or {})
    frozen_checks = {
        "risk_reward_ratio": _matches(
            payload.get("risk_reward_ratio"),
            V3B_TARGET_RR,
        ),
        "protection_trigger_fraction": _matches(
            candidate.get("protection_trigger_tp2_fraction"),
            V3B_PROTECTION_TRIGGER_FRACTION,
        ),
        "protected_stop_fraction": _matches(
            candidate.get("protected_stop_tp2_fraction"),
            V3B_PROTECTED_STOP_FRACTION,
        ),
        "no_partial_close": candidate.get(
            "no_partial_close_at_protection_trigger"
        ) is True,
        "protected_stop_present": candidate.get("protected_sl_price") not in {
            None,
            "",
        },
        "trigger_price_present": candidate.get("tp1") not in {None, ""},
    }
    if not all(frozen_checks.values()):
        return _blocked(
            "WAIT_V3B_FROZEN_MANAGEMENT_CONTRACT",
            payload=payload,
            details={"frozen_checks": frozen_checks},
        )

    payload.update({
        "live_strategy_model": LIVE_V3B_MODEL,
        "strategy_execution_profile": V3B_EXECUTION_PROFILE,
        "protection_trigger_price": candidate.get("tp1"),
        "protected_sl_price": candidate.get("protected_sl_price"),
        "protection_trigger_tp2_fraction": V3B_PROTECTION_TRIGGER_FRACTION,
        "protected_stop_tp2_fraction": V3B_PROTECTED_STOP_FRACTION,
        "no_partial_close_at_protection_trigger": True,
        "v3b_frozen_target_rr": V3B_TARGET_RR,
    })
    return {
        "ok": True,
        "submitted": False,
        "reason": None,
        "payload": payload,
        "execution_profile": V3B_EXECUTION_PROFILE,
    }


def dispatch_v3b_to_live_core(
    candidate,
    *,
    executor,
    live_auto_enabled,
    strategy_enabled=None,
    broker_handoff_enabled=None,
):
    """Call an injected LIVE executor only after every independent gate passes.

    The normal LIVE Auto switch remains authoritative.  This adapter never
    toggles it and never changes either V3B environment switch.
    """
    strategy_on = (
        live_v3b_enabled()
        if strategy_enabled is None
        else bool(strategy_enabled)
    )
    handoff_on = (
        v3b_broker_handoff_enabled()
        if broker_handoff_enabled is None
        else bool(broker_handoff_enabled)
    )

    if not strategy_on:
        return _blocked("WAIT_V3B_LIVE_DISABLED")
    if not handoff_on:
        return _blocked("WAIT_V3B_BROKER_HANDOFF_DISABLED")
    if not bool(live_auto_enabled):
        return _blocked("LIVE_AUTO_OFF")

    prepared = build_v3b_broker_core_payload(candidate)
    if not prepared.get("ok"):
        return prepared
    if not callable(executor):
        return _blocked(
            "WAIT_V3B_LIVE_EXECUTOR_UNAVAILABLE",
            payload=prepared.get("payload"),
        )

    # Exceptions are intentionally not swallowed here.  The existing LIVE core
    # owns durable submission/reconciliation semantics and must see failures.
    result = executor(prepared["payload"], source="auto")
    return {
        "ok": bool(isinstance(result, dict) and result.get("ok")),
        "submitted": True,
        "reason": (
            None
            if isinstance(result, dict) and result.get("ok")
            else (result or {}).get("reason")
            if isinstance(result, dict)
            else "LIVE_EXECUTOR_INVALID_RESPONSE"
        ),
        "payload": prepared["payload"],
        "execution_result": result,
        "execution_profile": V3B_EXECUTION_PROFILE,
    }
