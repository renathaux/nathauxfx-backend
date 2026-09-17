from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import routes.strategy_studio as route_module


VALID = {
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
    "stop_loss": {"method": "LAST_SWING", "buffer_pips": None, "fixed_distance": None},
    "tp1": {"enabled": False, "target_r": None, "close_percent": None, "protection_r": None},
    "tp2": {"method": "FIXED_R", "value": 2.0},
    "risk": {"method": "PERCENT_BALANCE", "value": 1.0},
}


def app_client():
    app = FastAPI()
    app.include_router(route_module.router)
    return TestClient(app)


def test_unauthenticated_list_is_rejected(monkeypatch):
    def deny(_request):
        raise HTTPException(status_code=401, detail="AUTH_REQUIRED")

    monkeypatch.setattr(route_module, "current_user", deny)
    monkeypatch.setattr(route_module, "_legacy_actor", lambda *_args, **_kwargs: None)
    response = app_client().get("/strategy-studio/strategies")
    assert response.status_code in {401, 403}


def test_validate_returns_field_errors_without_saving(monkeypatch):
    actor = SimpleNamespace(id="u1", email="u1@example.com")
    monkeypatch.setattr(route_module, "current_user_with_csrf", lambda _request: actor)
    called = {"create": 0}
    monkeypatch.setattr(
        route_module,
        "create_strategy",
        lambda *args, **kwargs: called.__setitem__("create", 1),
    )
    response = app_client().post(
        "/strategy-studio/validate",
        json={"name": "", "definition": {}},
    )
    assert response.status_code == 200
    assert response.json()["valid"] is False
    assert response.json()["errors"]
    assert called["create"] == 0


def test_create_never_enables_live_handoff(monkeypatch):
    actor = SimpleNamespace(id="u1", email="u1@example.com")
    monkeypatch.setattr(route_module, "current_user_with_csrf", lambda _request: actor)
    monkeypatch.setattr(
        route_module,
        "create_strategy",
        lambda owner, name, definition: {
            "strategy_id": "strat_1",
            "name": name,
            "definition": definition,
            "state": "INACTIVE",
            "summary": "5m BOS/CHOCH",
            "locked": False,
            "live_handoff_enabled": False,
        },
    )
    response = app_client().post(
        "/strategy-studio/strategies",
        json={"name": "My Strategy", "definition": VALID},
    )
    assert response.status_code == 201
    assert response.json()["strategy"]["live_handoff_enabled"] is False


def test_owner_key_prefers_stable_user_id():
    assert route_module.owner_key(SimpleNamespace(id="42", email="x@example.com")) == "user:42"
    assert route_module.owner_key({"email": "OWNER@EXAMPLE.COM"}) == "owner:owner@example.com"


def test_route_source_has_no_live_execution_imports():
    source = Path(route_module.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "place_market_order",
        "claim_submission",
        "live_auto_trade_enabled",
        "save_active_strategy_settings",
    ):
        assert forbidden not in source
