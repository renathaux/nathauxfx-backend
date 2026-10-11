"""Compatibility tombstone for the removed production Strategy V2 shadow.

Strategy V2 was retired in September 2026. Its evaluator, simulation state,
Neon persistence, metrics, and broker-result linking were deliberately removed.
These two no-op functions remain temporarily only because legacy V1-era runtime
modules import them by name. They perform no database access, strategy work,
order work, or state mutation.
"""


def evaluate_cycle_safely(*_args, **_kwargs):
    """Retired V2 hook: intentionally does nothing."""
    return {
        "ok": True,
        "removed": True,
        "shadow_only": True,
        "reason": "STRATEGY_V2_REMOVED",
    }


def link_v1_execution_safely(*_args, **_kwargs):
    """Retired V2 broker-observation hook: intentionally does nothing."""
    return False
