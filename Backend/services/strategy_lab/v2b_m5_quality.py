"""Strategy Lab V2B: looser M5 confirmation quality (45% body / 35% wick)."""
from .baseline_v1 import candidates, resolve_trade
from .m5_quality_variant import evaluate_event as _evaluate_event

MIN_BODY_RATIO = 0.45
MAX_CLOSE_SIDE_WICK_RATIO = 0.35


def evaluate_event(event, timestamp, prefix15, frame5, side, leg, settings, end, *, previous_close=None):
    return _evaluate_event(
        event,
        timestamp,
        prefix15,
        frame5,
        side,
        leg,
        settings,
        end,
        previous_close=previous_close,
        minimum_body_ratio=MIN_BODY_RATIO,
        maximum_close_side_wick_ratio=MAX_CLOSE_SIDE_WICK_RATIO,
    )
