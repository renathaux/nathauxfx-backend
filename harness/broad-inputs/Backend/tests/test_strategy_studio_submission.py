from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db import Base
from test_broker_execution_metadata import fixture_metadata
from models import ExecutionProtocolState, IndicatorEvent, IndicatorEventLifecycle
from services.trade_submission_service import (
    ACCEPTED,
    FAILED_BEFORE_SEND,
    claim_strategy_submission as _claim_strategy_submission,
    claim_submission as _claim_submission,
    complete_submission,
    recover_unsent_claim,
)


def _admitted_call(function, account, factory, *args, **kwargs):
    from startup_recovery.runtime import manager_context
    with manager_context(factory.recovery_tokens[str(account)]):
        return function(*args, **kwargs)


def claim_strategy_submission(setup_id, account_id, *args, **kwargs):
    return _admitted_call(_claim_strategy_submission,account_id,kwargs['session_factory'],
                         setup_id,account_id,*args,**kwargs)


def claim_submission(event_id,mode,account_id,*args,**kwargs):
    return _admitted_call(_claim_submission,account_id,kwargs['session_factory'],
                         event_id,mode,account_id,*args,**kwargs)


def _factory(tmp_path: Path):
    database = tmp_path / "studio_submission.sqlite3"
    engine = create_engine(
        f"sqlite:///{database}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    from recovery_fixture import admitted_manager
    factory.recovery_tokens={}
    for account in ('47810571','111','222','999'):
        with admitted_manager(factory,account) as token:
            factory.recovery_tokens[account]=token
    with factory() as session:
        session.add(ExecutionProtocolState(
            singleton_id=1,
            protocol_version="indicator-event-execution-v2",
            updated_at=datetime.now(timezone.utc),
        ))
        session.commit()
    return factory


def _add_studio_lifecycle(factory, *, setup_id="sts1_setup", account_id="47810571", account_scope=None):
    from models import StrategySetupLifecycle
    from services.strategy_studio_models import SavedStrategy
    from services.strategy_live_binding import make_entry_binding, freeze_plan
    from services.strategy_studio_schema import normalize_definition
    from test_strategy_studio_live_candidate import _definition, _ready_result
    definition = normalize_definition(_definition())
    scope = account_scope or f"CTRADER:DEMO:{account_id}"

    with factory() as session:
        saved = session.get(SavedStrategy, 'strat_1')
        if saved is None:
            saved = SavedStrategy(strategy_id='strat_1', owner_id='user:1', name='fixture', schema_version=1,
                                  definition_json=definition, created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc))
            session.add(saved)
            session.flush()
        from dataclasses import replace
        result = replace(_ready_result(), entry=1.1)
        binding = make_entry_binding(session, saved, freeze_plan(definition, result, symbol='EURUSD', account_balance=10000., account_scope=scope, broker_metadata=fixture_metadata(account_id=account_id, environment=scope.split(':')[1].lower())))
        session.add(StrategySetupLifecycle(
            setup_id=setup_id,
            owner_id="user:1",
            strategy_id="strat_1",
            account_id=account_id,
            account_scope=account_scope or f"CTRADER:DEMO:{account_id}",
            symbol="EURUSD",
            direction="BUY",
            status="ELIGIBLE",
            definition_snapshot=definition,
            entry_binding=binding,
            updated_at=datetime.now(timezone.utc),
        ))
        session.commit()


def _payload(factory, setup_id='sts1_setup'):
    from models import StrategySetupLifecycle
    with factory() as session:
        binding = session.get(StrategySetupLifecycle, setup_id).entry_binding
    plan = binding['frozen_plan']
    return dict(symbol='EURUSD', action='BUY', signal='BUY', entry=plan['entry'], sl=plan['sl'], tp1=plan['tp1'], tp2=plan['tp2'],
                studio_owner_id='user:1', studio_strategy_id='strat_1', studio_setup_id=setup_id, studio_account_scope=plan['account_scope'],
                studio_binding=binding, studio_risk_method='PERCENT_BALANCE', studio_risk_value=1., requested_risk_percent=1.,
                risk_percent=1., risk_amount=100., account_balance_used=10000., volume=.2, volume_units=20000, mode='demo',
                studio_tp1_enabled=False, tp1_definition=plan['tp1_definition'], fundamental_policy=plan['fundamental_policy'],
                risk=dict(account_balance=10000., risk_percent=1., risk_amount=100., broker_metadata=plan['broker_metadata']))


def _studio_claim(factory, setup_id="sts1_setup", account_id="47810571"):
    return claim_strategy_submission(
        setup_id,
        account_id,
        "EURUSD",
        "BUY",
        _payload(factory, setup_id),
        owner_id="user:1",
        strategy_id="strat_1",
        session_factory=factory,
    )


def test_studio_claim_is_atomic_exactly_once(tmp_path):
    factory = _factory(tmp_path)
    _add_studio_lifecycle(factory)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: _studio_claim(factory), range(2)))

    assert sum(1 for result in results if result.get("ok")) == 1
    assert sum(1 for result in results if not result.get("ok")) == 1

    from models import StrategySetupLifecycle, TradeSubmissionAttempt
    with factory() as session:
        attempts = session.query(TradeSubmissionAttempt).all()
        assert len(attempts) == 1
        assert attempts[0].event_id == "studio:sts1_setup"
        assert attempts[0].signal_setup_id == "sts1_setup"
        assert attempts[0].lifecycle_kind == "STRATEGY_STUDIO"
        assert session.get(StrategySetupLifecycle, "sts1_setup").status == "SUBMITTING"


def test_studio_claims_for_different_accounts_and_setup_ids_do_not_collide(tmp_path):
    factory = _factory(tmp_path)
    _add_studio_lifecycle(factory, setup_id="sts1_a", account_id="111")
    _add_studio_lifecycle(factory, setup_id="sts1_b", account_id="222")

    first = _studio_claim(factory, "sts1_a", "111")
    second = _studio_claim(factory, "sts1_b", "222")

    assert first["ok"] is True
    assert second["ok"] is True
    assert first["idempotency_key"] != second["idempotency_key"]


def test_studio_claim_rejects_owner_strategy_or_account_mismatch(tmp_path):
    factory = _factory(tmp_path)
    _add_studio_lifecycle(factory)

    wrong = claim_strategy_submission(
        "sts1_setup",
        "999",
        "EURUSD",
        "BUY",
        _payload(factory),
        owner_id="user:1",
        strategy_id="strat_1",
        session_factory=factory,
    )
    assert wrong["ok"] is False
    assert wrong["reason"] == 'STRATEGY_SETUP_MISSING'


def test_studio_completion_transitions_setup_lifecycle_to_consumed(tmp_path):
    factory = _factory(tmp_path)
    _add_studio_lifecycle(factory)
    claim = _studio_claim(factory)
    assert claim["ok"] is True

    assert complete_submission(
        claim["idempotency_key"],
        {"broker_result": ACCEPTED, "order_id": "o1", "position_id": "p1"},
        session_factory=factory,
    ) is True

    from models import StrategySetupLifecycle, TradeSubmissionAttempt
    with factory() as session:
        lifecycle = session.get(StrategySetupLifecycle, "sts1_setup")
        attempt = session.query(TradeSubmissionAttempt).one()
        assert lifecycle.status == "CONSUMED"
        assert lifecycle.broker_position_id == "p1"
        assert attempt.attempt_status == "ACCEPTED"


def test_studio_unsent_recovery_returns_setup_to_eligible(tmp_path):
    factory = _factory(tmp_path)
    _add_studio_lifecycle(factory)
    claim = _studio_claim(factory)
    assert claim["ok"] is True

    assert recover_unsent_claim(claim["idempotency_key"], session_factory=factory) is True

    from models import StrategySetupLifecycle, TradeSubmissionAttempt
    with factory() as session:
        lifecycle = session.get(StrategySetupLifecycle, "sts1_setup")
        attempt = session.query(TradeSubmissionAttempt).one()
        assert lifecycle.status == "ELIGIBLE"
        assert attempt.attempt_status == FAILED_BEFORE_SEND


def test_existing_indicator_claim_public_contract_remains_compatible(tmp_path):
    factory = _factory(tmp_path)
    now = datetime.now(timezone.utc)
    with factory() as session:
        session.add(IndicatorEvent(
            event_id="evt_1",
            symbol="EURUSD",
            timeframe="5m",
            candle_timestamp=now,
            classification="BOS",
            direction="BUY",
            broken_level=1.1,
            opposite_swing={"type": "LOW", "price": 1.09},
            identity={},
            payload={},
            configuration_version="test",
            is_historical=False,
            created_at=now,
        ))
        session.add(IndicatorEventLifecycle(
            event_id="evt_1",
            mode="LIVE",
            owner_id="OWNER",
            account_id="47810571",
            status="ELIGIBLE",
            signal_setup_id="sig_1",
            updated_at=now,
        ))
        session.commit()

    result = claim_submission(
        "evt_1",
        "LIVE",
        "47810571",
        "EURUSD",
        "sig_1",
        {"action": "BUY"},
        owner_id="OWNER",
        direction="BUY",
        session_factory=factory,
    )

    assert result["ok"] is True
    from models import TradeSubmissionAttempt
    with factory() as session:
        attempt = session.query(TradeSubmissionAttempt).one()
        assert attempt.lifecycle_kind == "INDICATOR_EVENT"


def test_stage3_migration_follows_0024_and_removes_submission_event_fk():
    migration = Path(__file__).resolve().parents[1] / "migrations" / "versions" / "20260917_0025_strategy_studio_live.py"
    source = migration.read_text(encoding="utf-8")
    assert 'revision = "20260917_0025"' in source
    assert 'down_revision = "20260917_0024"' in source
    assert "lifecycle_kind" in source
    assert "trade_submission_attempts_event_id_fkey" in source


def test_studio_claim_preserves_immutable_execution_snapshot(tmp_path):
    from models import StrategySetupLifecycle
    factory = _factory(tmp_path)
    _add_studio_lifecycle(factory)
    claim = _studio_claim(factory)
    assert claim["ok"]
    with factory() as session:
        row = session.get(StrategySetupLifecycle, "sts1_setup")
        snapshot = row.execution_snapshot
        assert snapshot["entry"] == 1.1
        assert snapshot["client_order_id"] == claim["broker_client_order_id"]
        assert snapshot["strategy_id"] == "strat_1"
        assert len(snapshot["definition_hash"]) == 64
