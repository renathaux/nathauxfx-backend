"""Process-local Neon traffic guards for non-trading observability.

These guards may reduce duplicate observer persistence only. They must never
change a strategy result, execution gate, LIVE/PAPER state, broker call, or
trade lifecycle decision.
"""
from __future__ import annotations

from functools import wraps
import os
import threading
import time


DEFAULT_LIFECYCLE_HEARTBEAT_SECONDS = 300
MIN_LIFECYCLE_HEARTBEAT_SECONDS = 60
MAX_LIFECYCLE_HEARTBEAT_SECONDS = 900


def _configured_heartbeat_seconds(environment=None):
    environment = os.environ if environment is None else environment
    raw = environment.get(
        "FOREX_LIFECYCLE_OBSERVER_HEARTBEAT_SECONDS",
        str(DEFAULT_LIFECYCLE_HEARTBEAT_SECONDS),
    )
    try:
        seconds = int(raw)
    except (TypeError, ValueError):
        seconds = DEFAULT_LIFECYCLE_HEARTBEAT_SECONDS
    return max(
        MIN_LIFECYCLE_HEARTBEAT_SECONDS,
        min(MAX_LIFECYCLE_HEARTBEAT_SECONDS, seconds),
    )


def install_neon_lifecycle_observer_throttle():
    """Collapse unchanged lifecycle observer DB writes to one heartbeat.

    State changes are still persisted immediately. The first observation after
    every process restart is also persisted immediately, so restart recovery is
    unchanged. This wrapper is deliberately attached only to the diagnostic
    observer function; trading code never consults its cache.
    """
    from . import strategy_diagnostics_service as diagnostics

    if getattr(diagnostics, "_NEON_LIFECYCLE_THROTTLE_INSTALLED", False):
        return True

    original = diagnostics.persist_lifecycle_evaluation_safely
    state = {}
    lock = threading.RLock()
    stats = {
        "db_calls": 0,
        "skipped_unchanged": 0,
        "state_change_calls": 0,
        "heartbeat_calls": 0,
    }

    diagnostics._NEON_LIFECYCLE_THROTTLE_ORIGINAL = original
    diagnostics._NEON_LIFECYCLE_THROTTLE_STATE = state
    diagnostics._NEON_LIFECYCLE_THROTTLE_STATS = stats

    def reset_for_tests():
        with lock:
            state.clear()
            for key in stats:
                stats[key] = 0

    diagnostics._reset_neon_lifecycle_throttle_for_tests = reset_for_tests

    @wraps(original)
    def throttled(snapshot, source_state=None):
        # Any fingerprinting problem fails open to the original observer. This
        # optimization must never make observability less reliable because of
        # malformed diagnostic input.
        try:
            frozen = snapshot or {}
            source = source_state if isinstance(source_state, dict) else {}
            symbol = str(frozen.get("symbol") or "").upper()
            account_scope = str(
                source.get("account_scope")
                or source.get("account_id")
                or os.getenv("ACTIVE_CTRADER_ACCOUNT_ID")
                or os.getenv("CTRADER_ACCOUNT_ID")
                or "FOREX_DEFAULT"
            )
            fingerprint = diagnostics.meaningful_state_fingerprint(frozen)
            key = (symbol, account_scope)
            heartbeat_seconds = _configured_heartbeat_seconds()
            now_monotonic = time.monotonic()
        except Exception:
            return diagnostics._NEON_LIFECYCLE_THROTTLE_ORIGINAL(
                snapshot,
                source_state,
            )

        # Keep the lock through the observer call. This path is best-effort
        # diagnostics only, and serializing it prevents two simultaneous panel
        # refreshes from issuing the same Neon read/insert pair.
        with lock:
            previous = state.get(key)
            same_state = bool(
                previous
                and previous.get("fingerprint") == fingerprint
            )
            elapsed = (
                now_monotonic - float(previous.get("written_at", 0))
                if previous
                else None
            )
            if (
                same_state
                and elapsed is not None
                and elapsed < heartbeat_seconds
            ):
                stats["skipped_unchanged"] += 1
                return previous.get("result")

            if previous is None or not same_state:
                reason = "STATE_CHANGE"
                stats["state_change_calls"] += 1
            else:
                reason = "HEARTBEAT"
                stats["heartbeat_calls"] += 1

            result = diagnostics._NEON_LIFECYCLE_THROTTLE_ORIGINAL(
                snapshot,
                source_state,
            )
            stats["db_calls"] += 1

            # Do not cache a failed best-effort write. A later cycle should be
            # allowed to retry Neon immediately.
            if result is not None:
                state[key] = {
                    "fingerprint": fingerprint,
                    "written_at": time.monotonic(),
                    "result": result,
                    "reason": reason,
                }
            return result

    diagnostics.persist_lifecycle_evaluation_safely = throttled
    diagnostics._NEON_LIFECYCLE_THROTTLE_INSTALLED = True
    return True
