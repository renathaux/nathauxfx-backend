from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db import Base
from models import StrategyStudioLiveState, TradeSubmissionAttempt
import routes.strategy_studio as route_module
from services import strategy_studio_live_state as live_state


def _client():
    app = FastAPI()
    app.include_router(route_module.router)
    return TestClient(app)


def _actor():
    return SimpleNamespace(id="1", email="owner@example.com")


def _readiness(**overrides):
    value = {
        "ready": True,
        "parity_verified": True,
        "parity_status": "VERIFIED",
        "active_strategy_id": "strat_1",
        "configured_symbols": ["EURUSD", "XAUUSD"],
        "account_scope": "CTRADER:DEMO:47810571",
        "unresolved_reconciliation": False,
        "reports": {
            "EURUSD": {"match": True, "compared_setups": 2},
            "XAUUSD": {"match": True, "compared_setups": 1},
        },
        "reason": None,
    }
    value.update(overrides)
    return value


def _auth(monkeypatch):
    monkeypatch.setattr(route_module, "current_user_with_csrf", lambda _request: _actor())
    monkeypatch.setattr(route_module, "current_user", lambda _request: _actor())


def test_live_handoff_enable_requires_explicit_confirmation(monkeypatch):
    _auth(monkeypatch)
    setter = MagicMock()
    monkeypatch.setattr(route_module, "set_studio_live_state", setter, raising=False)
    monkeypatch.setattr(route_module, "evaluate_live_handoff_readiness", lambda _owner: _readiness(), raising=False)

    response = _client().post(
        "/strategy-studio/live-handoff",
        json={"enabled": True, "confirm": False, "strategy_id": "strat_1"},
    )

    assert response.status_code == 400
    assert "confirmation" in str(response.json().get("detail", "")).lower()
    setter.assert_not_called()


def test_live_handoff_enable_requires_active_strategy_match(monkeypatch):
    _auth(monkeypatch)
    setter = MagicMock()
    monkeypatch.setattr(route_module, "set_studio_live_state", setter, raising=False)
    monkeypatch.setattr(route_module, "evaluate_live_handoff_readiness", lambda _owner: _readiness(), raising=False)

    response = _client().post(
        "/strategy-studio/live-handoff",
        json={"enabled": True, "confirm": True, "strategy_id": "strat_other"},
    )

    assert response.status_code == 409
    assert "active" in str(response.json().get("detail", "")).lower()
    setter.assert_not_called()


def test_live_handoff_enable_requires_green_parity_for_all_configured_symbols(monkeypatch):
    _auth(monkeypatch)
    setter = MagicMock()
    monkeypatch.setattr(route_module, "set_studio_live_state", setter, raising=False)
    monkeypatch.setattr(
        route_module,
        "evaluate_live_handoff_readiness",
        lambda _owner: _readiness(
            ready=False,
            parity_verified=False,
            parity_status="MISMATCH",
            reason="STRATEGY_STUDIO_PARITY_NOT_VERIFIED",
            reports={"EURUSD": {"match": True}, "XAUUSD": {"match": False}},
        ),
        raising=False,
    )

    response = _client().post(
        "/strategy-studio/live-handoff",
        json={"enabled": True, "confirm": True, "strategy_id": "strat_1"},
    )

    assert response.status_code == 409
    assert "PARITY" in str(response.json().get("detail", "")).upper()
    setter.assert_not_called()


def test_live_handoff_enable_fails_closed_on_unresolved_studio_reconciliation(monkeypatch):
    _auth(monkeypatch)
    setter = MagicMock()
    monkeypatch.setattr(route_module, "set_studio_live_state", setter, raising=False)
    monkeypatch.setattr(
        route_module,
        "evaluate_live_handoff_readiness",
        lambda _owner: _readiness(
            ready=False,
            unresolved_reconciliation=True,
            reason="STRATEGY_STUDIO_RECONCILIATION_UNRESOLVED",
        ),
        raising=False,
    )

    response = _client().post(
        "/strategy-studio/live-handoff",
        json={"enabled": True, "confirm": True, "strategy_id": "strat_1"},
    )

    assert response.status_code == 409
    assert "RECONCILIATION" in str(response.json().get("detail", "")).upper()
    setter.assert_not_called()


def test_live_handoff_enable_writes_only_durable_studio_state(monkeypatch):
    _auth(monkeypatch)
    monkeypatch.setattr(route_module, "evaluate_live_handoff_readiness", lambda _owner: _readiness(), raising=False)
    setter = MagicMock(return_value={
        "owner_id": "user:1",
        "enabled": True,
        "enabled_strategy_id": "strat_1",
        "enabled_at": "2026-09-17T19:00:00+00:00",
        "updated_at": "2026-09-17T19:00:00+00:00",
    })
    monkeypatch.setattr(route_module, "set_studio_live_state", setter, raising=False)

    response = _client().post(
        "/strategy-studio/live-handoff",
        json={"enabled": True, "confirm": True, "strategy_id": "strat_1"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["state"]["enabled"] is True
    setter.assert_called_once_with("user:1", "strat_1", True, True)

    source = Path(route_module.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "live_auto_toggle(",
        "save_auto_trade_state(",
        "place_market_order(",
        "execute_live_order_core(",
        "claim_strategy_submission(",
    ):
        assert forbidden not in source


def test_live_handoff_disable_is_confirmation_gated_and_does_not_require_green_parity(monkeypatch):
    _auth(monkeypatch)
    setter = MagicMock(return_value={
        "owner_id": "user:1", "enabled": False, "enabled_strategy_id": None,
        "enabled_at": None, "updated_at": "2026-09-17T19:00:00+00:00",
    })
    readiness = MagicMock(side_effect=AssertionError("disable must not require parity"))
    monkeypatch.setattr(route_module, "set_studio_live_state", setter, raising=False)
    monkeypatch.setattr(route_module, "evaluate_live_handoff_readiness", readiness, raising=False)

    denied = _client().post(
        "/strategy-studio/live-handoff",
        json={"enabled": False, "confirm": False, "strategy_id": "strat_1"},
    )
    assert denied.status_code == 400
    setter.assert_not_called()

    response = _client().post(
        "/strategy-studio/live-handoff",
        json={"enabled": False, "confirm": True, "strategy_id": "strat_1"},
    )
    assert response.status_code == 200
    assert response.json()["state"]["enabled"] is False
    setter.assert_called_once_with("user:1", "strat_1", False, True)
    readiness.assert_not_called()


def test_live_status_exposes_fresh_readiness_without_mutating_gate(monkeypatch):
    _auth(monkeypatch)
    monkeypatch.setattr(route_module, "evaluate_live_handoff_readiness", lambda _owner: _readiness(), raising=False)
    monkeypatch.setattr(
        route_module,
        "get_studio_live_state",
        lambda owner: {
            "owner_id": owner, "enabled": False, "enabled_strategy_id": None,
            "enabled_at": None, "updated_at": None,
        },
    )

    response = _client().get("/strategy-studio/live-status")
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False
    assert body["parity_verified"] is True
    assert body["parity_status"] == "VERIFIED"
    assert body["active_strategy_id"] == "strat_1"


def _factory(tmp_path: Path):
    database = tmp_path / "studio_live_state.sqlite3"
    engine = create_engine(
        f"sqlite:///{database}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_durable_live_state_requires_confirmation_and_defaults_off(tmp_path):
    factory = _factory(tmp_path)
    assert live_state.get_studio_live_state("user:1", factory)["enabled"] is False

    setter = getattr(live_state, "set_studio_live_state", None)
    assert setter is not None, "Task 8 contract missing: set_studio_live_state"
    with pytest.raises(ValueError, match="confirm|confirmation"):
        setter("user:1", "strat_1", True, False, factory)

    enabled = setter("user:1", "strat_1", True, True, factory)
    assert enabled["enabled"] is True
    assert enabled["enabled_strategy_id"] == "strat_1"

    disabled = setter("user:1", "strat_1", False, True, factory)
    assert disabled["enabled"] is False
    assert disabled["enabled_strategy_id"] is None


def test_unresolved_strategy_studio_reconciliation_detection(tmp_path):
    factory = _factory(tmp_path)
    detector = getattr(live_state, "has_unresolved_studio_reconciliation", None)
    assert detector is not None, "Task 8 contract missing: has_unresolved_studio_reconciliation"

    now = datetime.now(timezone.utc)
    with factory() as session:
        session.add(TradeSubmissionAttempt(
            event_id="studio:sts1_pending",
            lifecycle_kind="STRATEGY_STUDIO",
            mode="LIVE",
            account_id="47810571",
            owner_id="user:1",
            direction="BUY",
            symbol="EURUSD",
            signal_setup_id="sts1_pending",
            idempotency_key="fs1_pending",
            attempt_status="RECONCILIATION_REQUIRED",
            claimed_at=now,
            broker_client_order_id="client-pending",
            request_payload_fingerprint="fingerprint",
            reconciliation_status="REQUIRED",
            updated_at=now,
        ))
        session.commit()

    assert detector("user:1", factory) is True
    assert detector("user:2", factory) is False


def test_live_handoff_busy_reports_admission_without_changing_state(monkeypatch, tmp_path):
    from services.heavy_replay_admission import heavy_replay_lease
    _auth(monkeypatch)
    monkeypatch.setenv('HEAVY_REPLAY_LOCK_PATH', str(tmp_path / 'heavy.lock'))
    setter = MagicMock()
    monkeypatch.setattr(route_module, 'set_studio_live_state', setter)
    with heavy_replay_lease():
        response = _client().post('/strategy-studio/live-handoff', json={
            'enabled': True, 'confirm': True, 'strategy_id': 'strat_1'})
    assert response.status_code == 429
    assert response.json()['detail'] == 'HEAVY_BACKTEST_BUSY'
    setter.assert_not_called()
