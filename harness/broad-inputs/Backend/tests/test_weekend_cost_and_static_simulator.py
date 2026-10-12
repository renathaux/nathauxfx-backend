from datetime import datetime, timezone

import pandas as pd

from fundamentals import ingestion
from services.market_hours import forex_weekend_closed
from services.strategy_simulator_static_data import build_static_market_bundle


def utc(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def test_forex_weekend_guard_tracks_new_york_boundary():
    assert forex_weekend_closed(utc("2026-09-18T20:59:00Z")) is False
    assert forex_weekend_closed(utc("2026-09-18T21:00:00Z")) is True
    assert forex_weekend_closed(utc("2026-09-19T12:00:00Z")) is True
    assert forex_weekend_closed(utc("2026-09-20T20:59:00Z")) is True
    assert forex_weekend_closed(utc("2026-09-20T21:00:00Z")) is False


def test_weekend_ingestion_returns_before_database_health_read():
    def forbidden_health(**kwargs):
        raise AssertionError("weekend ingestion must not touch provider health / Neon")

    result = ingestion.run_fundamental_ingestion_if_due(
        now=utc("2026-09-19T12:00:00Z"),
        health_reader=forbidden_health,
    )
    assert result["status"] == "MARKET_CLOSED_WEEKEND"


def test_static_simulator_bundle_aggregates_without_database():
    rows = []
    start = pd.Timestamp("2026-09-01T00:00:00Z")
    for index in range(12):
        stamp = start + pd.Timedelta(minutes=5 * index)
        base = 1.10 + index * 0.001
        rows.append({
            "timestamp": stamp.isoformat(),
            "open": base,
            "high": base + 0.002,
            "low": base - 0.002,
            "close": base + 0.001,
            "volume": 0,
        })

    bundle = build_static_market_bundle(
        rows,
        "2026-09-01T00:00:00Z",
        "2026-09-01T01:00:00Z",
    )
    assert len(bundle["5m"]) == 12
    assert len(bundle["15m"]) == 4
    assert len(bundle["1h"]) == 1
    assert bundle["4h"].empty


def test_static_simulator_bundle_keeps_pre_start_warmup_candles():
    rows = []
    history_start = pd.Timestamp("2026-09-01T00:00:00Z")
    for index in range(12):
        stamp = history_start + pd.Timedelta(minutes=5 * index)
        rows.append({
            "timestamp": stamp.isoformat(),
            "open": 1.10,
            "high": 1.11,
            "low": 1.09,
            "close": 1.10,
            "volume": 0,
        })

    bundle = build_static_market_bundle(
        rows,
        "2026-09-01T00:30:00Z",
        "2026-09-01T01:00:00Z",
    )

    assert bundle["5m"].index[0] == history_start
    assert len(bundle["5m"]) == 12
