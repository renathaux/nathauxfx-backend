from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db import Base
from models import ExecutionProtocolState, IndicatorEvent, IndicatorEventLifecycle
from services.trade_submission_service import (
    ACCEPTED,
    FAILED_BEFORE_SEND,
    claim_strategy_submission,
    claim_submission,
    complete_submission,
    recover_unsent_claim,
)


def _factory(tmp_path: Path):
    database = tmp_path / "studio_submission.sqlite3"
    engine = create_engine(
        f"sqlite:///{database}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
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

    with factory() as session:
        session.add(StrategySetupLifecycle(
            setup_id=setup_id,
            owner_id="user:1",
            strategy_id="strat_1",
            account_id=account_id,
            account_scope=account_scope or f"CTRADER:DEMO:{account_id}",
            symbol="EURUSD",
            direction="BUY",
            status="ELIGIBLE",
            definition_snapshot={"schema_version": 1},
            updated_at=datetime.now(timezone.utc),
        ))
        session.commit()


def _studio_claim(factory, setup_id="sts1_setup", account_id="47810571"):
    return claim_strategy_submission(
        setup_id,
        account_id,
        "EURUSD",
        "BUY",
        {"action": "BUY", "entry": 1.1},
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
        {"action": "BUY"},
        owner_id="user:1",
        strategy_id="strat_1",
        session_factory=factory,
    )
    assert wrong["ok"] is False
    assert "claimable" in wrong["reason"].lower() or "scope" in wrong["reason"].lower()


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
