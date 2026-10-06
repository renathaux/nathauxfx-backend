"""Shared forex weekend market-hours guard used to suppress idle polling."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo


_NEW_YORK = ZoneInfo("America/New_York")
_WEEKEND_BOUNDARY_MINUTES = 17 * 60


def _aware(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current


def forex_weekend_closed(now: datetime | None = None) -> bool:
    """True from Friday 17:00 through Sunday 17:00 America/New_York.

    The New York timezone makes the boundary follow DST automatically.
    """
    local = _aware(now).astimezone(_NEW_YORK)
    weekday = local.weekday()  # Monday=0 ... Sunday=6
    minutes = local.hour * 60 + local.minute
    return (
        weekday == 5
        or (weekday == 4 and minutes >= _WEEKEND_BOUNDARY_MINUTES)
        or (weekday == 6 and minutes < _WEEKEND_BOUNDARY_MINUTES)
    )
