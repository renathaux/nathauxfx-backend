"""Compatibility tombstone for the retired production V1 diagnostics stack.

V3B is the production strategy. These names remain temporarily because large
legacy panel/execution modules still import them, but none of the functions in
this module read or write Neon and none can affect a trading decision.
"""
from __future__ import annotations

import hashlib
import json


RETIRED_REASON = "PRODUCTION_V1_RETIRED"


def meaningful_state(snapshot):
    """Return a deterministic compatibility state without touching storage."""
    value = snapshot if isinstance(snapshot, dict) else {"value": str(snapshot)}
    return value


def meaningful_state_fingerprint(snapshot):
    """Keep the old throttle deterministic even though its DB writer is gone."""
    try:
        payload = json.dumps(
            meaningful_state(snapshot),
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    except Exception:
        payload = repr(snapshot)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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
