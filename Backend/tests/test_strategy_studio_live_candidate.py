from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from ctrader_account_context import AccountIdentity
from db import Base
from models import StrategySetupLifecycle, StrategyStudioLiveState
from services.strategy_engine.types import EvaluationResult, EvaluationState
from services.strategy_studio_models import SavedStrategy, StrategyStudioSelection
from services import strategy_studio_live_candidate as candidate
from services.strategy_studio_live_state import studio_live_enabled


OWNER = "user:1"
STRATEGY_ID = "strat_live_test"
IDENTITY = AccountIdentity("47810571", "demo")
NOW = datetime(2026, 9, 17, 13, 0, tzinfo=timezone.utc)


def _factory(tmp_path: Path):
    database = tmp_path / "studio_live_candidate.sqlite3"
    engine = create_engine(
        f"sqlite:///{database}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _definition(symbols=None):
    return {
        "schema_version": 1,
        "symbols": symbols or ["EURUSD"],
        "trading_timeframe": "5m",
        "trend": {"timeframe": None, "methods": []},
        "structure": {
            "trigger": "BOS_CHOCH",
            "break_validation": ["CLOSE_BEYOND", "MIN_BODY_PERCENT"],
            "minimum_body_percent": 50.0,
            "minimum_distance_pips": None,
        },
        "confirmation": {
            "rules": ["NEXT_SAME_DIRECTION", "SECOND_CLOSE_BEYOND"],
            "minimum_body_percent": None,
        },
        "entry": {"method": "CONFIRMATION_CLOSE"},
        "stop_loss": {"method": "LAST_SWING", "buffer_pips": 0.0, "fixed_distance": None},
        "tp1": {"enabled": False, "target_r": None, "close_percent": None, "protection_r": None},
        "tp2": {"method": "FIXED_R", "value": 1.90},
        "risk": {"method": "PERCENT_BALANCE", "value": 1.0},
    }


def _save_active(factory, *, symbols=None):
    with factory() as session:
        session.add(SavedStrategy(
            strategy_id=STRATEGY_ID,
            owner_id=OWNER,
            name="Shadow candidate test",
            schema_version=1,
            definition_json=_definition(symbols),
            created_at=NOW,
            updated_at=NOW,
        ))
        session.add(StrategyStudioSelection(
            owner_id=OWNER,
            strategy_id=STRATEGY_ID,
            activated_at=NOW,
            updated_at=NOW,
        ))
        session.commit()


def _enable(factory):
    with factory() as session:
        session.add(StrategyStudioLiveState(
            owner_id=OWNER,
            enabled=True,
            enabled_strategy_id=STRATEGY_ID,
            enabled_at=NOW,
            updated_at=NOW,
        ))
        session.commit()


def _timeline():
    stamp = pd.Timestamp("2026-09-17T13:05:00Z")
    return SimpleNamespace(timestamps=lambda: [stamp])


def _ready_result():
    return EvaluationResult(
        signal="BUY",
        steps={
            "structure": {"state": "PASSED", "event_type": "BOS", "direction": "BUY"},
            "entry": {"state": "PASSED", "price": 1.1010},
            "stop_loss": {"state": "PASSED", "price": 1.0990},
            "tp2": {"state": "PASSED", "price": 1.1048},
        },
        setup_id="setup_eval_abc",
        entry=1.1010,
        sl=1.0990,
        tp1=None,
        tp2=1.1048,
        risk_budget={"method": "PERCENT_BALANCE", "value": 1.0, "dollars": 100.0},
        next_state=EvaluationState("READY", None),
    )


def _pending_state():
    return EvaluationState(
        "WAITING",
        {
            "direction": "BUY",
            "event_timestamp": "2026-09-17T13:00:00+00:00",
            "broken_level": 1.1005,
            "invalidation_price": 1.0990,
            "trigger_close": 1.1008,
        },
    )


def test_live_gate_defaults_off_and_does_not_write_lifecycle(tmp_path, monkeypatch):
    factory = _factory(tmp_path)
    _save_active(factory)
    assert studio_live_enabled(OWNER, factory) is False

    monkeypatch.setattr(candidate, "build_market_facts", lambda *a, **k: (_ for _ in ()).throw(AssertionError("gate off must not evaluate")))
    result = candidate.build_studio_candidate(
        OWNER, IDENTITY, "EURUSD", {"5m": pd.DataFrame()},
        account_balance=10000.0, prior_state=EvaluationState(), session_factory=factory,
    )

    assert result["signal"] == "WAIT"
    assert result["reason"] == "WAIT_STUDIO_LIVE_DISABLED"
    assert result["studio_live_ready"] is False
    with factory() as session:
        assert session.query(StrategySetupLifecycle).count() == 0


def test_gate_on_without_active_strategy_waits_without_lifecycle(tmp_path):
    factory = _factory(tmp_path)
    with factory() as session:
        session.add(StrategyStudioLiveState(
            owner_id=OWNER, enabled=True, enabled_strategy_id=None,
            enabled_at=NOW, updated_at=NOW,
        ))
        session.commit()

    result = candidate.build_studio_candidate(
        OWNER, IDENTITY, "EURUSD", {"5m": pd.DataFrame()},
        account_balance=10000.0, prior_state=EvaluationState(), session_factory=factory,
    )
    assert result["reason"] == "WAIT_STUDIO_NO_ACTIVE_STRATEGY"
    with factory() as session:
        assert session.query(StrategySetupLifecycle).count() == 0


def test_symbol_not_in_active_strategy_waits_before_evaluation(tmp_path, monkeypatch):
    factory = _factory(tmp_path)
    _save_active(factory, symbols=["XAUUSD"])
    _enable(factory)
    monkeypatch.setattr(candidate, "build_market_facts", lambda *a, **k: (_ for _ in ()).throw(AssertionError("disabled symbol must not evaluate")))

    result = candidate.build_studio_candidate(
        OWNER, IDENTITY, "EURUSD", {"5m": pd.DataFrame()},
        account_balance=10000.0, prior_state=EvaluationState(), session_factory=factory,
    )
    assert result["reason"] == "WAIT_STUDIO_SYMBOL_DISABLED"


def test_evaluator_wait_does_not_create_eligible_lifecycle(tmp_path, monkeypatch):
    factory = _factory(tmp_path)
    _save_active(factory)
    _enable(factory)
    monkeypatch.setattr(candidate, "build_market_facts", lambda *a, **k: _timeline())
    monkeypatch.setattr(candidate, "evaluate_strategy", lambda *a, **k: EvaluationResult(
        signal="WAIT", steps={"confirmation": {"state": "WAITING"}}, setup_id="setup_eval_wait",
        entry=None, sl=None, tp1=None, tp2=None, risk_budget=None,
        next_state=EvaluationState("WAITING", _pending_state().pending_setup),
    ))

    result = candidate.build_studio_candidate(
        OWNER, IDENTITY, "EURUSD", {"5m": pd.DataFrame()},
        account_balance=10000.0, prior_state=_pending_state(), session_factory=factory,
    )
    assert result["signal"] == "WAIT"
    assert result["studio_live_ready"] is False
    with factory() as session:
        assert session.query(StrategySetupLifecycle).count() == 0


def test_ready_candidate_persists_deterministic_account_scoped_eligible_setup(tmp_path, monkeypatch):
    factory = _factory(tmp_path)
    _save_active(factory)
    _enable(factory)
    monkeypatch.setattr(candidate, "build_market_facts", lambda *a, **k: _timeline())
    monkeypatch.setattr(candidate, "evaluate_strategy", lambda *a, **k: _ready_result())

    first = candidate.build_studio_candidate(
        OWNER, IDENTITY, "EURUSD", {"5m": pd.DataFrame()},
        account_balance=10000.0, prior_state=_pending_state(), session_factory=factory,
    )
    second = candidate.build_studio_candidate(
        OWNER, IDENTITY, "EURUSD", {"5m": pd.DataFrame()},
        account_balance=10000.0, prior_state=_pending_state(), session_factory=factory,
    )

    assert first["signal"] == "BUY"
    assert first["studio_live_ready"] is True
    assert first["setup_id"] == second["setup_id"]
    assert first["account_scope"] == IDENTITY.scope
    assert first["strategy_id"] == STRATEGY_ID
    assert first["entry"] == 1.1010
    assert first["sl"] == 1.0990
    assert first["tp2"] == 1.1048
    assert first["fundamental_policy"] == "BLOCK_OPPOSITE"
    assert first["evaluator_steps"]["entry"]["state"] == "PASSED"

    with factory() as session:
        rows = session.query(StrategySetupLifecycle).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.setup_id == first["setup_id"]
        assert row.account_id == IDENTITY.account_id
        assert row.account_scope == IDENTITY.scope
        assert row.strategy_id == STRATEGY_ID
        assert row.status == "ELIGIBLE"
        assert row.definition_snapshot["schema_version"] == 1


def test_consumed_setup_is_never_reopened_by_re_evaluation(tmp_path, monkeypatch):
    factory = _factory(tmp_path)
    _save_active(factory)
    _enable(factory)
    monkeypatch.setattr(candidate, "build_market_facts", lambda *a, **k: _timeline())
    monkeypatch.setattr(candidate, "evaluate_strategy", lambda *a, **k: _ready_result())

    first = candidate.build_studio_candidate(
        OWNER, IDENTITY, "EURUSD", {"5m": pd.DataFrame()},
        account_balance=10000.0, prior_state=_pending_state(), session_factory=factory,
    )
    with factory() as session:
        row = session.get(StrategySetupLifecycle, first["setup_id"])
        row.status = "CONSUMED"
        row.updated_at = datetime.now(timezone.utc)
        session.commit()

    again = candidate.build_studio_candidate(
        OWNER, IDENTITY, "EURUSD", {"5m": pd.DataFrame()},
        account_balance=10000.0, prior_state=_pending_state(), session_factory=factory,
    )
    assert again["studio_live_ready"] is False
    assert again["reason"] == "WAIT_STUDIO_SETUP_CONSUMED"
    with factory() as session:
        assert session.get(StrategySetupLifecycle, first["setup_id"]).status == "CONSUMED"


def test_candidate_source_has_no_broker_or_live_auto_mutation_imports():
    source = (Path(__file__).resolve().parents[1] / "services" / "strategy_studio_live_candidate.py").read_text(encoding="utf-8")
    forbidden = (
        "place_market_order",
        "execute_live_order_core",
        "set_auto_trade",
        "LIVE_AUTO_TRADE_ENABLED",
        "ctrader_connector",
    )
    for token in forbidden:
        assert token not in source
