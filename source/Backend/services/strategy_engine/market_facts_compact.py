"""Compatibility alias for shared pure fact storage."""
import sys
from live_integrity import market_facts_compact as _implementation
sys.modules[__name__] = _implementation
