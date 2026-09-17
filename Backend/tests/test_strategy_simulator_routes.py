from contextlib import contextmanager
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from routes import strategy_simulator as route
from services.strategy_studio_service import StrategyStudioNotFound


def saved_strategy():
    return {
        "strategy_id": "strat_test",
        "name": "Test",
        "definition": {
            "schema_version": 1,
            "symbols": ["EURUSD"],
            "trading_timeframe": "5m",
            "trend": {"timeframe": None, "methods": []},
            "structure": {"trigger": "BOS_CHOCH", "break_validation": [], "minimum_body_percent": None, "minimum_distance_pips": None},
            "confirmation": {"rules": [], "minimum_body_percent": None},
            "entry": {"method": "BOS_CHOCH_CLOSE"},
            "stop_loss": {"method": "FIXED_DISTANCE", "buffer_pips": None, "fixed_distance": 20},
            "tp1": {"enabled": False, "target_r": None, "close_percent": None, "protection_r": None},
            "tp2": {"method": "FIXED_R", "value": 2},
            "risk": {"method": "PERCENT_BALANCE", "value": 1},
        },
    }


def payload(**overrides):
    base = dict(
        strategy_id="strat_test",
        symbol="EURUSD",
        start="2026-09-01T00:00:00Z",
        end="2026-09-02T00:00:00Z",
        mode="FAST",
        risk_override=None,
    )
    base.update(overrides)
    return route.SimulationRequest(**base)


def test_route_source_has_no_broker_or_live_mutation_calls():
    source = inspect.getsource(route)
    forbidden = [
        "place_market_order", "modify_position_sltp", "modify_position_stop_loss",
        "close_position", "save_durable_auto_trade", "set_active_ctrader_account",
        "activate_strategy(", "deactivate_strategy(",
    ]
    for token in forbidden:
        assert token not in source


def test_settings_aggregator_mounts_strategy_simulator_router():
    settings_source = (Path(__file__).resolve().parents[1] / "routes" / "settings.py").read_text(encoding="utf-8")
    assert "from routes.strategy_simulator import router as strategy_simulator_router" in settings_source
    assert "router.include_router(strategy_simulator_router)" in settings_source


def test_foreign_or_missing_strategy_returns_404(monkeypatch):
    monkeypatch.setattr(route, "_actor", lambda request: {"email": "x@example.com"})
    monkeypatch.setattr(route, "get_strategy", lambda owner, strategy_id: (_ for _ in ()).throw(StrategyStudioNotFound("strategy not found")))
    with pytest.raises(HTTPException) as exc:
        route.strategy_simulation_run(payload(), SimpleNamespace())
    assert exc.value.status_code == 404


def test_symbol_must_be_allowed_by_saved_strategy(monkeypatch):
    monkeypatch.setattr(route, "_actor", lambda request: {"email": "x@example.com"})
    monkeypatch.setattr(route, "get_strategy", lambda owner, strategy_id: saved_strategy())
    with pytest.raises(HTTPException) as exc:
        route.strategy_simulation_run(payload(symbol="XAUUSD"), SimpleNamespace())
    assert exc.value.status_code == 400
    assert "symbol" in str(exc.value.detail).lower()


def test_route_pins_scope_uses_positive_balance_and_replay_mode(monkeypatch):
    calls = {}
    identity = SimpleNamespace(scope="CTRADER:DEMO:47810571")

    @contextmanager
    def fake_pinned():
        calls["pinned"] = True
        yield identity

    monkeypatch.setattr(route, "_actor", lambda request: {"email": "x@example.com"})
    monkeypatch.setattr(route, "get_strategy", lambda owner, strategy_id: saved_strategy())
    monkeypatch.setattr(route, "pinned_account", fake_pinned)
    monkeypatch.setattr(route, "get_ctrader_account_snapshot", lambda: {"balance": 9895.11})

    def fake_load(symbol, start, end, *, stream_scope, session_factory=None):
        calls["scope"] = stream_scope
        calls["range"] = (start, end)
        return {"5m": object(), "15m": object(), "1h": object(), "4h": object()}

    monkeypatch.setattr(route, "load_market_bundle", fake_load)

    def fake_run(definition, bundle, symbol, balance, *, risk_override=None, include_replay=False):
        calls["balance"] = balance
        calls["replay"] = include_replay
        calls["override"] = risk_override
        return {"metrics": {"starting_balance": balance}, "trades": [], "equity_curve": [], "replay": []}

    monkeypatch.setattr(route, "run_simulation", fake_run)
    result = route.strategy_simulation_run(payload(mode="REPLAY"), SimpleNamespace())
    assert result["ok"] is True
    assert calls["pinned"] is True
    assert calls["scope"] == "CTRADER:DEMO:47810571"
    assert calls["balance"] == pytest.approx(9895.11)
    assert calls["replay"] is True
    assert result["account_scope"] == "CTRADER:DEMO:47810571"


def test_nonpositive_or_missing_balance_is_rejected(monkeypatch):
    identity = SimpleNamespace(scope="CTRADER:DEMO:47810571")

    @contextmanager
    def fake_pinned():
        yield identity

    monkeypatch.setattr(route, "_actor", lambda request: {"email": "x@example.com"})
    monkeypatch.setattr(route, "get_strategy", lambda owner, strategy_id: saved_strategy())
    monkeypatch.setattr(route, "pinned_account", fake_pinned)
    monkeypatch.setattr(route, "get_ctrader_account_snapshot", lambda: {"balance": 0})
    with pytest.raises(HTTPException) as exc:
        route.strategy_simulation_run(payload(), SimpleNamespace())
    assert exc.value.status_code == 409
    assert "balance" in str(exc.value.detail).lower()
