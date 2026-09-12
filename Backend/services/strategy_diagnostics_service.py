"""Compatibility tombstone for the retired production V1 diagnostics stack.

V3B is the production strategy.  These names remain temporarily because large
legacy panel/execution modules still import them, but none of the functions in
this module read or write Neon and none can affect a trading decision.
"""
from __future__ import annotations

import hashlib


RETIRED_REASON = "PRODUCTION_V1_RETIRED"


def meaningful_state(_snapshot):
    """Return a stable empty observer state for compatibility only."""
    return {"retired": True, "reason": RETIRED_REASON}


def meaningful_state_fingerprint(_snapshot):
    """Stable fingerprint used by the old optional observer throttle."""
    return hashlib.sha256(RETIRED_REASON.encode("utf-8")).hexdigest()


def persist_lifecycle_evaluation_safely(*_args, **_kwargs):
    """Retired V1 lifecycle persistence: deliberately performs no DB access."""
    return {"ok": True, "disabled": True, "reason": RETIRED_REASON}


def persist_cycle_safely(*_args, **_kwargs):
    """Retired V1 cycle persistence: deliberately performs no DB access."""
    return {"ok": True, "disabled": True, "reason": RETIRED_REASON}


def record_execution_gate_safely(*_args, **_kwargs):
    """Legacy V1 diagnostic hook retained as a harmless no-op."""
    return False


def update_execution_outcome_safely(*_args, **_kwargs):
    """Legacy V1 diagnostic hook retained as a harmless no-op."""
    return False


def query_cycles(*_args, **_kwargs):
    """V1 diagnostic history API is retired."""
    return []
