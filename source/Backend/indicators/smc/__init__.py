"""FlowSignal Smart Money Concepts indicator package.

The exported structure engine is the direct closed-candle port of the
TradingView "SMC Structures and FVG" structure state machine. Fair Value Gap
logic remains intentionally excluded.

This package is analysis-only. It must not import broker, execution,
LIVE/PAPER, risk, cooldown, or order-management modules.
"""

# Keep one authority for chart + strategy, and keep that authority aligned with
# the TradingView indicator the user is comparing against. The newer
# conservative engine.py intentionally changed CHoCH/BOS behavior by protecting
# external structure; that made the trace diverge from the source indicator.
from .legacy_engine import analyze_structure, detect_confirmed_swings

__all__ = ["analyze_structure", "detect_confirmed_swings"]
