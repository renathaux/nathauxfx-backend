"""Small deterministic US publication-day calendar for daily Treasury data."""
from __future__ import annotations

from datetime import date, timedelta


def _observed_fixed(day):
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def _nth_weekday(year, month, weekday, occurrence):
    current = date(year, month, 1)
    current += timedelta(days=(weekday - current.weekday()) % 7)
    return current + timedelta(days=7 * (occurrence - 1))


def _last_weekday(year, month, weekday):
    current = date(year + (month == 12), 1 if month == 12 else month + 1, 1) - timedelta(days=1)
    return current - timedelta(days=(current.weekday() - weekday) % 7)


def us_treasury_holidays(year):
    holidays = {
        _observed_fixed(date(year, 1, 1)),
        _nth_weekday(year, 1, 0, 3),       # Martin Luther King Jr. Day
        _nth_weekday(year, 2, 0, 3),       # Washington's Birthday
        _last_weekday(year, 5, 0),         # Memorial Day
        _observed_fixed(date(year, 6, 19)),
        _observed_fixed(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),       # Labor Day
        _nth_weekday(year, 10, 0, 2),      # Columbus Day/federal closure
        _observed_fixed(date(year, 11, 11)),
        _nth_weekday(year, 11, 3, 4),      # Thanksgiving
        _observed_fixed(date(year, 12, 25)),
    }
    # A New Year's Day observed in the prior December belongs to this calendar.
    holidays.add(_observed_fixed(date(year + 1, 1, 1)))
    return holidays


def is_treasury_market_day(value):
    return value.weekday() < 5 and value not in us_treasury_holidays(value.year)


def market_days_elapsed(observation_date, current_date):
    if current_date <= observation_date:
        return 0
    count = 0
    cursor = observation_date + timedelta(days=1)
    while cursor <= current_date:
        if is_treasury_market_day(cursor):
            count += 1
        cursor += timedelta(days=1)
    return count


def missing_market_dates(date_from, date_to, observed_dates):
    observed = set(observed_dates)
    missing = []
    cursor = date_from
    while cursor <= date_to:
        if is_treasury_market_day(cursor) and cursor not in observed:
            missing.append(cursor)
        cursor += timedelta(days=1)
    return missing
