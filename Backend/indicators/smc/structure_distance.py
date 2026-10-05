"""Distance diagnostics for visual BOS/CHoCH structure events."""
from __future__ import annotations

import math


class StructureDistanceGate:
    """Keep the last accepted event level without post-filtering event output."""

    def __init__(self, *, timeframe=None, point_size=None):
        self.timeframe = str(timeframe or "").strip().lower()
        try:
            parsed_point_size = float(point_size)
        except (TypeError, ValueError):
            parsed_point_size = 0.0
        self.point_size = parsed_point_size if math.isfinite(parsed_point_size) and parsed_point_size > 0 else None
        self.minimum_distance = 0.0
        self.last_accepted_level = None
        self.previous_level = None
        self.last_distance_points = None

    def accept(self, candidate_level):
        try:
            candidate = float(candidate_level)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(candidate):
            return False
        self.previous_level = self.last_accepted_level
        self.last_distance_points = (
            abs(candidate - self.previous_level) / self.point_size
            if self.previous_level is not None and self.point_size is not None
            else None
        )
        self.last_accepted_level = candidate
        return True

    def config(self):
        return {
            "timeframe": self.timeframe or None,
            "point_size": self.point_size,
            "minimum_structure_points": 0,
            "last_accepted_structure_level": self.last_accepted_level,
            "previous_structure_event_level": self.previous_level,
            "structure_distance_points": self.last_distance_points,
        }
