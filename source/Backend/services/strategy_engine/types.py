"""Compatibility alias: shared pure implementation, unchanged public interface."""
import sys
from live_integrity import types as _implementation
sys.modules[__name__] = _implementation
