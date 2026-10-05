"""Analysis-only historical strategy replay subsystem.

This package intentionally has no imports from broker, PAPER/LIVE, lifecycle,
watch, or execution modules.
"""

from .replay_engine import run_replay

__all__ = ["run_replay"]
