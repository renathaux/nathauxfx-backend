"""Explicit admitted-state fixture for isolated downstream unit tests only.

Does not replace the gate. Real PostgreSQL allocation/transition/contending
workers are separately exercised by test_recovery_store/fencing. Never imported
by application code and never installed as a global pytest bypass.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4

from models import RecoveryAccount, RecoveryAttempt
from startup_recovery.runtime import manager_context
from startup_recovery.store import scope_key
from startup_recovery.types import AccountScope, ManagerToken


@contextmanager
def admitted_manager(factory, account_id, environment='demo'):
    scope = AccountScope('ctrader', environment, str(account_id))
    token = ManagerToken(scope, str(uuid4()), 1, 'isolated-fixture-boot')
    with factory.begin() as s:
        s.add(RecoveryAccount(scope_key=scope_key(scope), broker=scope.broker,
            environment=scope.environment, account_id=scope.account_id,
            allocated_epoch=1, owner_attempt_id=token.attempt_id, owner_epoch=1,
            phase='NEW_ENTRIES_READY', accepted_manifest_hash='e' * 64,
            handoff_evidence={'fixture': 'prior release terminated and reconciled'}))
        s.flush()
        s.add(RecoveryAttempt(attempt_id=token.attempt_id, scope_key=scope_key(scope),
            epoch=1, boot_id=token.boot_id, build_id='a' * 40,
            phase='NEW_ENTRIES_READY', phase_evidence_hash='e' * 64,
            created_at=datetime.now(timezone.utc)))
    with manager_context(token):
        yield token


@contextmanager
def admitted_worker(monkeypatch, account_id='fixture-worker'):
    """Isolated downstream startup fixture; actual owner checks remain enabled."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    import db
    engine = create_engine('sqlite:///:memory:', poolclass=StaticPool,
                           connect_args={'check_same_thread': False})
    db.Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(db, 'SessionLocal', factory)
            with admitted_manager(factory, account_id) as token:
                yield token
    finally:
        engine.dispose()
