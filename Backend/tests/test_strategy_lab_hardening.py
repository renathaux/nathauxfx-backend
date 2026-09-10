from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db import Base
from models import IndicatorCandle
from routes import strategy_lab as lab_route
from services.strategy_lab import production_math as lab
from services.strategy_lab.replay_engine import run_replay
from services.strategy_settings_service import defaults
from strategies import strict_trader as production

# Keep references to the generic EURUSD production helpers. Importing api in
# other test modules installs process-wide XAUUSD adapters on strict_trader.
PRODUCTION_BOS_BUFFER = production.bos_buffer
PRODUCTION_TREND_FILTER = production.trend_filter
PRODUCTION_CONSOLIDATION = production.classify_consolidation
PRODUCTION_VALID_SWINGS = production.detect_valid_swings
PRODUCTION_BUILD_RISK = production.build_risk_levels


def historical_fixture(rows=160):
    index = pd.date_range("2026-08-20T00:00:00Z", periods=rows, freq="15min")
    close = [1.1600 + ((position % 24) - 12) * .00017 + position * .000003 for position in range(rows)]
    return pd.DataFrame({
        "Open": [value - .00004 for value in close],
        "High": [value + .00032 for value in close],
        "Low": [value - .00031 for value in close],
        "Close": close,
    }, index=index)


@pytest.mark.parametrize("cutoff", ["2026-08-20T18:00:00Z", "2026-08-21T06:00:00Z", "2026-08-21T18:00:00Z"])
def test_future_candles_do_not_change_decisions_at_or_before_cutoff(cutoff):
    fifteen = historical_fixture()
    five = historical_fixture(480).reindex(pd.date_range("2026-08-20T00:00:00Z", periods=480, freq="5min")).interpolate()
    boundary = pd.Timestamp(cutoff)
    through_cutoff = run_replay(
        "EURUSD", "baseline_v1", "2026-08-20T00:00:00Z", cutoff,
        frames=(fifteen.loc[fifteen.index <= boundary], five.loc[five.index + pd.Timedelta(minutes=5) <= boundary]),
        settings=defaults(),
    )
    with_future = run_replay(
        "EURUSD", "baseline_v1", "2026-08-20T00:00:00Z", "2026-08-21T23:59:59Z",
        frames=(fifteen, five), settings=defaults(),
    )
    earlier = [row for row in with_future["event_trace"] if pd.Timestamp(row["event_time"]) + pd.Timedelta(minutes=15) <= boundary]
    assert through_cutoff["event_trace"] == earlier


def test_lab_math_remains_differentially_equal_to_production(monkeypatch):
    candles = historical_fixture()
    settings = defaults()
    monkeypatch.setattr(production.shared, "get_tp1_ratio_of_tp2", lambda: .8)

    for stop in (40, 80, 120, 159):
        prefix = candles.iloc[:stop]
        assert lab.bos_buffer(prefix, settings["bos_buffer_points"]) == PRODUCTION_BOS_BUFFER(prefix, "EURUSD", settings["bos_buffer_points"])
        lab_trend, prod_trend = lab.trend_filter(prefix), PRODUCTION_TREND_FILTER(prefix, "EURUSD")
        for key in ("trend", "buy_allowed", "sell_allowed", "ema_fast", "ema_slow", "close"):
            assert lab_trend[key] == prod_trend[key]
        assert lab.classify_consolidation(prefix)["is_consolidation"] == PRODUCTION_CONSOLIDATION(prefix, "EURUSD")["is_consolidation"]
        assert [(s["type"], s["price"], s["index"]) for s in lab.detect_valid_swings(prefix)] == [
            (s["type"], s["price"], s["index"]) for s in PRODUCTION_VALID_SWINGS(prefix, "EURUSD")
        ]

    setup = candles.index[-1]
    invalidation = {"type": "HIGH", "price": 1.1630, "swing_time": candles.index[-8].isoformat(),
                    "confirmation_time": candles.index[-6].isoformat(), "source": "historical_fixture"}
    entry = 1.1600
    lab_levels = lab.build_risk_levels(candles, "SELL", entry, setup, settings, invalidation, .8)
    production_levels = PRODUCTION_BUILD_RISK(
        candles, "SELL", entry, "EURUSD", setup_break_time=setup,
        execution_settings=settings, event_invalidation_swing=invalidation,
    )
    for key in ("entry", "stop_loss", "tp1", "tp2", "protected_sl_price",
                "risk_reward_ratio", "tp_structure_source", "tp_structure_used",
                "sl_structure_source"):
        assert lab_levels[key] == production_levels[key], f"Strategy Lab drifted for {key}"


def test_replay_endpoint_requires_admin_and_reads_indicator_candles_only(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    start = datetime(2026, 8, 20, tzinfo=timezone.utc)
    with sessions.begin() as session:
        for timeframe, minutes, count in (("15m", 15, 120), ("5m", 5, 360)):
            for position in range(count):
                price = 1.16 + position * .000001
                session.add(IndicatorCandle(
                    symbol="EURUSD", timeframe=timeframe,
                    candle_timestamp=start + timedelta(minutes=minutes * position),
                    open_price=price, high_price=price + .0002, low_price=price - .0002,
                    close_price=price + .00001, created_at=start,
                ))
    app = FastAPI()
    app.include_router(lab_route.router)
    client = TestClient(app)
    payload = {"symbol": "EURUSD", "strategy": "baseline_v1",
               "start": "2026-08-20T00:00:00Z", "end": "2026-08-21T00:00:00Z"}
    assert client.post("/strategy-lab/replay", json=payload).status_code == 403

    monkeypatch.setattr(lab_route, "_require_strategy_lab_admin", lambda _request: {"role": "admin"})
    real_run = run_replay
    monkeypatch.setattr(lab_route, "run_replay", lambda symbol, strategy, replay_start, replay_end: real_run(
        symbol, strategy, replay_start, replay_end, session_factory=sessions, settings=defaults()))
    statements = []
    event.listen(engine, "before_cursor_execute", lambda _c, _u, statement, *_args: statements.append(statement.strip().upper()))
    before = {table: engine.connect().exec_driver_sql(f'SELECT COUNT(*) FROM "{table}"').scalar_one()
              for table in ("indicator_candles", "indicator_events", "indicator_event_lifecycle")}
    statements.clear()
    response = client.post("/strategy-lab/replay", json=payload)
    assert response.status_code == 200
    assert response.json()["diagnostics"]["analysis_only"] is True
    assert statements and all(statement.startswith("SELECT") for statement in statements)
    after = {table: engine.connect().exec_driver_sql(f'SELECT COUNT(*) FROM "{table}"').scalar_one()
             for table in before}
    assert after == before
