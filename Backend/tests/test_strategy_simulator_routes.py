from contextlib import contextmanager
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from routes import strategy_simulator as route


def definition():
    return {
        "schema_version": 1,
        "symbols": ["EURUSD"],
        "trading_timeframe": "5m",
        "trend": {"timeframe": None, "methods": []},
        "structure": {
            "trigger": "BOS_CHOCH",
            "break_validation": [],
            "minimum_body_percent": None,
            "minimum_distance_pips": None,
        },
        "confirmation": {"rules": [], "minimum_body_percent": None},
        "entry": {"method": "BOS_CHOCH_CLOSE"},
        "stop_loss": {
            "method": "FIXED_DISTANCE",
            "buffer_pips": None,
            "fixed_distance": 20,
        },
        "tp1": {
            "enabled": False,
            "target_r": None,
            "close_percent": None,
            "protection_r": None,
        },
        "tp2": {"method": "FIXED_R", "value": 2},
        "risk": {"method": "PERCENT_BALANCE", "value": 1},
    }


def candles():
    return [
        {
            "timestamp": "2026-09-01T00:00:00Z",
            "open": 1.10, "high": 1.11, "low": 1.09, "close": 1.105,
            "volume": 0,
        },
        {
            "timestamp": "2026-09-01T00:05:00Z",
            "open": 1.105, "high": 1.12, "low": 1.10, "close": 1.115,
            "volume": 0,
        },
    ]


def payload(**overrides):
    base = dict(
        strategy_id="strat_test",
        strategy_name="Test",
        strategy_definition=definition(),
        symbol="EURUSD",
        start="2026-09-01T00:00:00Z",
        end="2026-09-02T00:00:00Z",
        mode="FAST",
        risk_override=None,
        candles_5m=candles(),
    )
    base.update(overrides)
    return route.SimulationRequest(**base)


def test_route_source_has_no_neon_history_or_broker_mutation_calls():
    source = inspect.getsource(route)
    forbidden = [
        "load_market_bundle",
        "IndicatorCandle",
        "get_strategy(",
        "place_market_order",
        "modify_position_sltp",
        "modify_position_stop_loss",
        "close_position",
        "save_durable_auto_trade",
        "activate_strategy(",
        "deactivate_strategy(",
    ]
    for token in forbidden:
        assert token not in source


def test_settings_aggregator_mounts_strategy_simulator_router():
    settings_source = (
        Path(__file__).resolve().parents[1] / "routes" / "settings.py"
    ).read_text(encoding="utf-8")
    assert (
        "from routes.strategy_simulator import router as strategy_simulator_router"
        in settings_source
    )
    assert "router.include_router(strategy_simulator_router)" in settings_source


def test_symbol_must_be_allowed_by_client_strategy(monkeypatch):
    monkeypatch.setattr(route, "_actor", lambda request: {"email": "x@example.com"})
    with pytest.raises(HTTPException) as exc:
        route.strategy_simulation_run(payload(symbol="XAUUSD"), SimpleNamespace())
    assert exc.value.status_code == 400
    assert "symbol" in str(exc.value.detail).lower()


def test_route_uses_static_candles_and_positive_balance(monkeypatch):
    calls = {}
    identity = SimpleNamespace(scope="CTRADER:DEMO:47810571")

    @contextmanager
    def fake_pinned():
        calls["pinned"] = True
        yield identity

    monkeypatch.setattr(route, "_actor", lambda request: {"email": "x@example.com"})
    monkeypatch.setattr(route, "pinned_account", fake_pinned)
    monkeypatch.setattr(
        route, "get_ctrader_account_snapshot", lambda: {"balance": 9895.11}
    )

    def fake_bundle(rows, start, end):
        calls["rows"] = rows
        calls["range"] = (start, end)
        return {"5m": object(), "15m": object(), "1h": object(), "4h": object()}

    monkeypatch.setattr(route, "build_static_market_bundle", fake_bundle)

    def fake_run(
        strategy_definition, bundle, symbol, balance,
        *, risk_override=None, include_replay=False,
        evaluation_start=None, evaluation_end=None,
    ):
        calls["definition"] = strategy_definition
        calls["balance"] = balance
        calls["replay"] = include_replay
        calls["evaluation_start"] = evaluation_start
        calls["evaluation_end"] = evaluation_end
        return {
            "metrics": {"starting_balance": balance},
            "trades": [],
            "equity_curve": [],
            "replay": [],
        }

    monkeypatch.setattr(route, "run_simulation", fake_run)
    result = route.strategy_simulation_run(
        payload(mode="REPLAY"), SimpleNamespace()
    )

    assert result["ok"] is True
    assert result["history_source"] == "STATIC_REPLAY_JSON"
    assert result["neon_candle_reads"] is False
    assert calls["pinned"] is True
    assert len(calls["rows"]) == 2
    assert calls["balance"] == pytest.approx(9895.11)
    assert calls["replay"] is True
    assert calls["evaluation_start"] == payload().start
    assert calls["evaluation_end"] == payload().end


def test_missing_static_history_is_rejected_before_simulation(monkeypatch):
    monkeypatch.setattr(route, "_actor", lambda request: {"email": "x@example.com"})
    with pytest.raises(HTTPException) as exc:
        route.strategy_simulation_run(
            payload(candles_5m=[]),
            SimpleNamespace(),
        )
    assert exc.value.status_code == 409
    assert "STATIC_SIMULATION_HISTORY_REQUIRED" in str(exc.value.detail)


def test_nonpositive_balance_is_rejected(monkeypatch):
    identity = SimpleNamespace(scope="CTRADER:DEMO:47810571")

    @contextmanager
    def fake_pinned():
        yield identity

    monkeypatch.setattr(route, "_actor", lambda request: {"email": "x@example.com"})
    monkeypatch.setattr(route, "pinned_account", fake_pinned)
    monkeypatch.setattr(route, "get_ctrader_account_snapshot", lambda: {"balance": 0})
    with pytest.raises(HTTPException) as exc:
        route.strategy_simulation_run(payload(), SimpleNamespace())
    assert exc.value.status_code == 409
    assert "balance" in str(exc.value.detail).lower()


def test_manual_history_database_endpoint_is_retired(monkeypatch):
    monkeypatch.setattr(route, "_actor", lambda request: {"email": "x@example.com"})
    request = route.ManualHistoryRequest(
        symbol="EURUSD",
        timeframe="5m",
        start="2026-09-01T00:00:00Z",
        end="2026-09-01T01:00:00Z",
    )
    with pytest.raises(HTTPException) as exc:
        route.manual_replay_history(request, SimpleNamespace())
    assert exc.value.status_code == 410
    assert "STATIC_JSON" in str(exc.value.detail)
