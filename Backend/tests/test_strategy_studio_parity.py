from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.strategy_studio as route_module
from ctrader_account_context import AccountIdentity
from services import strategy_studio_parity as parity
from services.strategy_engine import market_facts
from services.strategy_lab import v3b_m5_frozen_candidate as eur_v3b
from services.strategy_lab import v3b_xauusd_frozen_candidate as gold_v3b


def _frame(rows):
    return pd.DataFrame(
        rows,
        columns=["Open", "High", "Low", "Close"],
        index=pd.to_datetime([
            "2026-09-14T14:35:00Z",
            "2026-09-14T14:40:00Z",
        ]),
    )


def _patch_structure(monkeypatch, module, event):
    payload = {"events": [] if event is None else [event], "swings": []}
    monkeypatch.setattr(module, "analyze_structure", lambda *args, **kwargs: payload)
    monkeypatch.setattr(market_facts, "analyze_structure", lambda *args, **kwargs: payload)


def _client():
    app = FastAPI()
    app.include_router(route_module.router)
    return TestClient(app)


def test_v3b_entry_parity_definition_keeps_legacy_management_out_of_studio_schema():
    definition = parity.v3b_entry_parity_definition("EURUSD")
    assert definition["trading_timeframe"] == "5m"
    assert definition["structure"]["trigger"] == "BOS_CHOCH"
    assert definition["structure"]["break_validation"] == ["CLOSE_BEYOND", "MIN_BODY_PERCENT"]
    assert definition["structure"]["minimum_body_percent"] == pytest.approx(50.0)
    assert definition["confirmation"]["rules"] == ["NEXT_SAME_DIRECTION", "SECOND_CLOSE_BEYOND"]
    assert definition["entry"]["method"] == "CONFIRMATION_CLOSE"
    assert definition["stop_loss"] == {"method": "LAST_SWING", "buffer_pips": 0.0, "fixed_distance": None}
    assert definition["tp1"]["enabled"] is False
    assert definition["tp2"] == {"method": "FIXED_R", "value": 1.90}


def test_eurusd_buy_entry_path_matches_frozen_v3b(monkeypatch):
    frame = _frame([
        (1.10000, 1.10200, 1.09950, 1.10180),
        (1.10170, 1.10250, 1.10160, 1.10220),
    ])
    event = {
        "timestamp": frame.index[0].isoformat(),
        "event_type": "BOS",
        "direction": "BULLISH",
        "broken_level": 1.10150,
        "close": 1.10180,
        "event_invalidation_swing": {"type": "LOW", "price": 1.09900},
    }
    _patch_structure(monkeypatch, eur_v3b, event)

    report = parity.compare_v3b_entry_decisions(
        "EURUSD",
        frame,
        account_scope="CTRADER:DEMO:47810571",
    )

    assert report["match"] is True
    assert report["compared_setups"] == 1
    assert report["legacy"] == report["studio"]
    assert report["legacy"][0]["side"] == "BUY"
    assert report["legacy"][0]["entry"] == pytest.approx(1.10220)
    assert report["legacy"][0]["sl"] == pytest.approx(1.09850)
    assert report["legacy"][0]["tp2"] == pytest.approx(1.10923)
    assert report["mismatches"] == []


def test_xauusd_sell_entry_path_matches_frozen_v3b(monkeypatch):
    frame = _frame([
        (4301.50, 4302.00, 4298.00, 4298.50),
        (4298.70, 4299.00, 4296.50, 4297.00),
    ])
    event = {
        "timestamp": frame.index[0].isoformat(),
        "event_type": "CHOCH",
        "direction": "BEARISH",
        "broken_level": 4300.00,
        "close": 4298.50,
        "event_invalidation_swing": {"type": "HIGH", "price": 4305.00},
    }
    _patch_structure(monkeypatch, gold_v3b, event)

    report = parity.compare_v3b_entry_decisions(
        "XAUUSD",
        frame,
        account_scope="CTRADER:DEMO:47810571",
    )

    assert report["match"] is True
    assert report["compared_setups"] == 1
    assert report["legacy"] == report["studio"]
    assert report["legacy"][0]["side"] == "SELL"
    assert report["legacy"][0]["entry"] == pytest.approx(4297.00)
    assert report["legacy"][0]["sl"] == pytest.approx(4305.50)
    assert report["legacy"][0]["tp2"] == pytest.approx(4280.85)


def test_wait_period_matches_without_inventing_a_setup(monkeypatch):
    frame = _frame([
        (1.1000, 1.1005, 1.0995, 1.1001),
        (1.1001, 1.1006, 1.0998, 1.1002),
    ])
    _patch_structure(monkeypatch, eur_v3b, None)

    report = parity.compare_v3b_entry_decisions(
        "EURUSD",
        frame,
        account_scope="CTRADER:DEMO:47810571",
    )

    assert report["match"] is True
    assert report["compared_setups"] == 0
    assert report["legacy"] == []
    assert report["studio"] == []
    assert report["mismatches"] == []
    assert report["post_entry_management_compared"] is False
    assert "tp1_partial_close" not in report["parity_scope"]


def test_account_scope_is_required():
    frame = _frame([
        (1.1000, 1.1005, 1.0995, 1.1001),
        (1.1001, 1.1006, 1.0998, 1.1002),
    ])
    with pytest.raises(ValueError, match="ACCOUNT_SCOPE"):
        parity.compare_v3b_entry_decisions("EURUSD", frame, account_scope="")


def test_parity_endpoint_is_authenticated_read_only_and_account_scoped(monkeypatch):
    actor = SimpleNamespace(id="1", email="owner@example.com")
    identity = AccountIdentity("47810571", "demo")
    frame = _frame([
        (1.1000, 1.1005, 1.0995, 1.1001),
        (1.1001, 1.1006, 1.0998, 1.1002),
    ])
    sentinel = {
        "match": True,
        "compared_setups": 0,
        "legacy": [],
        "studio": [],
        "mismatches": [],
        "parity_scope": list(parity.PARITY_SCOPE),
        "post_entry_management_compared": False,
        "account_scope": identity.scope,
        "legacy_only_level_policy": {"sl_buffer_points": 50, "minimum_sl_points": 100, "target_rr": 1.9},
    }

    monkeypatch.setattr(route_module, "current_user", lambda _request: actor)
    monkeypatch.setattr(route_module, "selected_identity", lambda: identity)
    monkeypatch.setattr(route_module, "load_simulation_5m", lambda *args, **kwargs: frame)
    monkeypatch.setattr(route_module, "compare_v3b_entry_decisions", lambda *args, **kwargs: sentinel)

    response = _client().post(
        "/strategy-studio/parity/run",
        json={
            "symbol": "EURUSD",
            "start": "2026-09-14T14:35:00Z",
            "end": "2026-09-14T14:45:00Z",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["account_scope"] == identity.scope
    assert body["post_entry_management_compared"] is False
    assert body["live_handoff_enabled"] is False

    source = open(route_module.__file__, encoding="utf-8").read()
    for forbidden in (
        "place_market_order",
        "execute_live_order_core",
        "claim_submission(",
        "claim_strategy_submission(",
        "save_auto_trade",
        "set_auto_trade",
    ):
        assert forbidden not in source


def test_live_status_reports_gate_without_enabling_it(monkeypatch):
    actor = SimpleNamespace(id="1", email="owner@example.com")
    monkeypatch.setattr(route_module, "current_user", lambda _request: actor)
    monkeypatch.setattr(
        route_module,
        "get_studio_live_state",
        lambda owner: {
            "owner_id": owner,
            "enabled": False,
            "enabled_strategy_id": None,
            "enabled_at": None,
            "updated_at": None,
        },
    )
    response = _client().get("/strategy-studio/live-status")
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False
    assert body["parity_status"] == "REQUIRES_VERIFICATION"
    assert body["entry_parity_only"] is True
    assert body["post_entry_management_compared"] is False
