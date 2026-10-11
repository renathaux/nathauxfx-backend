"""Runtime guard for FlowSignal's trade-signal lifecycle.

The lifecycle normally runs unchanged. If a panel plan contains a circular or
pathologically deep diagnostic object, Python's deepcopy can raise
RecursionError. That display/diagnostic failure must never take the market-data
engine offline or bypass execution safety.
"""

from __future__ import annotations

import time
from typing import Any


_INSTALLED = False
_PRIMITIVES = (str, int, float, bool, type(None))


def _acyclic_clone(value: Any, *, depth: int = 0, seen: set[int] | None = None) -> Any:
    """Create a bounded JSON-like clone without following cycles forever."""
    if isinstance(value, _PRIMITIVES):
        return value

    if depth >= 14:
        return None

    if seen is None:
        seen = set()

    identity = id(value)
    if identity in seen:
        return None

    if isinstance(value, dict):
        seen.add(identity)
        try:
            return {
                str(key): _acyclic_clone(item, depth=depth + 1, seen=seen)
                for key, item in value.items()
                if key != "current_setup_state"
            }
        finally:
            seen.discard(identity)

    if isinstance(value, (list, tuple, set)):
        seen.add(identity)
        try:
            return [
                _acyclic_clone(item, depth=depth + 1, seen=seen)
                for item in value
            ]
        finally:
            seen.discard(identity)

    try:
        return value.isoformat()
    except Exception:
        try:
            return str(value)
        except Exception:
            return None


def clone_panel_for_transport(value: Any) -> Any:
    """Cycle-safe clone for browser transport/cache reads."""
    return _acyclic_clone(value)


def _sanitize_symbol_plans(panel_data: Any) -> bool:
    if not isinstance(panel_data, dict):
        return False

    changed = False
    for symbol in ("EURUSD", "XAUUSD"):
        plan = panel_data.get(symbol)
        if not isinstance(plan, dict):
            continue

        sanitized = _acyclic_clone(plan)
        if isinstance(sanitized, dict):
            plan.clear()
            plan.update(sanitized)
            changed = True

    return changed


def _fail_closed(panel_data: Any, error: BaseException) -> Any:
    """Keep data visible but make execution impossible for this failed cycle."""
    if not isinstance(panel_data, dict):
        return panel_data

    for symbol in ("EURUSD", "XAUUSD"):
        plan = panel_data.get(symbol)
        if not isinstance(plan, dict):
            continue

        previous_decision = str(
            plan.get("strategy_decision") or plan.get("signal") or "WAIT"
        ).upper()
        plan.update({
            "strategy_decision": previous_decision,
            "signal": "WAIT",
            "final_signal": "WAIT",
            "display_signal": "WAIT",
            "signal_display_state": "WAIT",
            "fresh_entry_available": False,
            "execution_allowed": False,
            "execution_status": "BLOCKED",
            "execution_block_reason": "LIFECYCLE_RECURSION_GUARD",
            "blocked_by": "trade_signal_lifecycle_guard",
            "blocked_reason": "Lifecycle diagnostics were reset safely; waiting for the next clean strategy cycle.",
            "lifecycle_guard": {
                "active": True,
                "error_type": type(error).__name__,
                "timestamp": time.time(),
            },
        })

    return panel_data


def install_trade_signal_lifecycle_guard() -> bool:
    """Patch api.apply_trade_signal_lifecycle after api.py finishes importing."""
    global _INSTALLED
    if _INSTALLED:
        return True

    import api

    original = getattr(api, "apply_trade_signal_lifecycle", None)
    if not callable(original):
        print("TRADE_SIGNAL_LIFECYCLE_GUARD_NOT_INSTALLED = missing lifecycle function")
        return False

    if getattr(original, "_flowsignal_recursion_guard", False):
        _INSTALLED = True
        return True

    def guarded(panel_data):
        try:
            return original(panel_data)
        except RecursionError as first_error:
            print("TRADE_SIGNAL_LIFECYCLE_RECURSION_RECOVER =", {
                "stage": "sanitize_and_retry",
                "error": str(first_error),
            })
            _sanitize_symbol_plans(panel_data)
            try:
                recovered = original(panel_data)
                print("TRADE_SIGNAL_LIFECYCLE_RECURSION_RECOVERED =", {"ok": True})
                return recovered
            except RecursionError as second_error:
                print("TRADE_SIGNAL_LIFECYCLE_RECURSION_FAIL_CLOSED =", {
                    "error": str(second_error),
                })
                return _fail_closed(panel_data, second_error)

    guarded._flowsignal_recursion_guard = True
    guarded._flowsignal_original = original
    api.apply_trade_signal_lifecycle = guarded
    _INSTALLED = True
    print("TRADE_SIGNAL_LIFECYCLE_GUARD_INSTALLED =", {"ok": True})
    return True
