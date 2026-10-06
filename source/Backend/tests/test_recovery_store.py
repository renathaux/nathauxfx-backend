"""Real PostgreSQL ownership proofs; no broker transport is imported."""
import importlib
import os
from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def store_api():
    try:
        return importlib.import_module('startup_recovery.store')
    except ModuleNotFoundError:
        pytest.fail('Durable recovery ownership implementation missing')


@pytest.fixture
def db(store_api):
    # Include the separate Studio model registry even when this module runs
    # alone; production migrations already create these authority tables.
    from services import strategy_studio_models
    dsn = os.getenv('VERIFIER_TEST_PG_DSN')
    if not dsn:
        pytest.skip('Requires disposable PostgreSQL Unix socket')
    assert 'host=/tmp/fs-verifier-pg.' in dsn and '/verifier_test?' in dsn
    from models import Base
    engine = create_engine(dsn)
    schema = 'recovery_' + uuid4().hex
    with engine.begin() as c:
        c.execute(text('CREATE SCHEMA ' + schema))
    scoped = engine.execution_options(schema_translate_map={None: schema})
    Base.metadata.create_all(scoped)
    yield sessionmaker(scoped, expire_on_commit=False)
    with engine.begin() as c:
        c.execute(text('DROP SCHEMA ' + schema + ' CASCADE'))
    engine.dispose()


def begin(api, db, boot='boot-a'):
    from startup_recovery.types import AccountScope
    with db.begin() as s:
        return api.begin_attempt(s, AccountScope('ctrader', 'demo', '123'), boot, 'a' * 40)


def cutover(api, db, token):
    from startup_recovery.types import HandoffEvidence
    proof = HandoffEvidence('operator-termination', 'b' * 64,
                            'legacy-release', True, True)
    with db.begin() as s:
        api.establish_legacy_cutover(s, token.scope, proof)


def test_initial_attempt_does_not_seed_owner(store_api, db):
    api = store_api
    t = begin(api, db)
    with db.begin() as s:
        assert api.account_state(s, t.scope).phase == 'LEGACY_CUTOVER_REQUIRED'
        assert api.account_state(s, t.scope).owner_attempt_id is None
        with pytest.raises(api.RecoveryError, match='LEGACY_CUTOVER_REQUIRED'):
            api.acquire_owner(s, t)


def test_unique_attempt_epochs_only_one_owner_and_no_timeout_takeover(store_api, db):
    api = store_api
    a, b = begin(api, db), begin(api, db, 'boot-b')
    assert (a.epoch, b.epoch) == (1, 2)
    cutover(api, db, a)
    with db.begin() as s:
        api.acquire_owner(s, a)
    with db.begin() as s:
        with pytest.raises(api.RecoveryError, match='RECOVERY_OWNER_BUSY'):
            api.acquire_owner(s, b)
    with db.begin() as s:
        assert api.account_state(s, a.scope).owner_attempt_id == a.attempt_id


def test_independent_session_lock_contention_is_immediate_busy(store_api, db):
    api = store_api
    a, b = begin(api, db), begin(api, db, 'boot-b')
    cutover(api, db, a)
    with db.begin() as first:
        api.acquire_owner(first, a)
        with db.begin() as second:
            with pytest.raises(api.RecoveryError, match='RECOVERY_OWNER_BUSY'):
                api.acquire_owner(second, b)


def test_phase_skip_wrong_boot_and_crash_before_reconciliation_fail_closed(store_api, db):
    api = store_api
    a = begin(api, db)
    cutover(api, db, a)
    with db.begin() as s:
        api.acquire_owner(s, a)
    with db.begin() as s:
        with pytest.raises(api.RecoveryError, match='RECOVERY_PHASE_INVALID'):
            api.advance(s, a, 'BOOTSTRAP', 'NEW_ENTRIES_READY', 'c' * 64)
        assert not api.entries_ready(s, a)
    with db.begin() as s:
        with pytest.raises(api.RecoveryError, match='RECOVERY_TOKEN_INVALID'):
            api.advance(s, replace(a, boot_id='foreign'), 'BOOTSTRAP', 'DB_READY', 'c' * 64)
    for old, new in [('BOOTSTRAP', 'DB_READY'), ('DB_READY', 'BROKER_AUTHENTICATED'),
                     ('BROKER_AUTHENTICATED', 'STATE_DISCOVERED')]:
        with db.begin() as s:
            api.advance(s, a, old, new, 'c' * 64)
            assert not api.entries_ready(s, a)
    # New process / time passing cannot convert a crashed discovered owner to ready.
    b = begin(api, db, 'boot-b')
    with db.begin() as s:
        assert not api.entries_ready(s, b)
        with pytest.raises(api.RecoveryError, match='RECOVERY_OWNER_BUSY'):
            api.acquire_owner(s, b)


def test_relinquish_requires_verified_drain_then_old_token_cannot_advance(store_api, db):
    from startup_recovery.types import HandoffEvidence
    api = store_api
    a, b = begin(api, db), begin(api, db, 'boot-b')
    cutover(api, db, a)
    with db.begin() as s:
        api.acquire_owner(s, a)
    bad = HandoffEvidence('graceful-drain', 'd' * 64, a.boot_id, False, True)
    with db.begin() as s:
        with pytest.raises(api.RecoveryError, match='RECOVERY_HANDOFF_UNPROVEN'):
            api.relinquish(s, a, bad)
    with db.begin() as s:
        api.relinquish(s, a, replace(bad, prior_process_stopped=True))
    with db.begin() as s:
        api.acquire_owner(s, b)
        with pytest.raises(api.RecoveryError, match='RECOVERY_TOKEN_STALE'):
            api.advance(s, a, 'BOOTSTRAP', 'DB_READY', 'c' * 64)


@pytest.mark.parametrize('stopped,settled', [(False, True), (True, False), (False, False)])
def test_legacy_cutover_requires_both_termination_and_reconciliation(store_api, db, stopped, settled):
    from startup_recovery.types import HandoffEvidence
    api = store_api
    a = begin(api, db)
    with db.begin() as s:
        with pytest.raises(api.RecoveryError, match='RECOVERY_HANDOFF_UNPROVEN'):
            api.establish_legacy_cutover(s, a.scope,
                HandoffEvidence('operator-termination', 'b' * 64, 'legacy-release', stopped, settled))
        assert api.account_state(s, a.scope).owner_attempt_id is None


def test_ready_requires_every_transition_and_current_owner_manifest(store_api, db):
    api = store_api
    a = begin(api, db)
    cutover(api, db, a)
    with db.begin() as s:
        api.acquire_owner(s, a)
    phases = ['BOOTSTRAP', 'DB_READY', 'BROKER_AUTHENTICATED', 'STATE_DISCOVERED',
              'STATE_RECONCILED', 'POSITION_MANAGEMENT_READY', 'NEW_ENTRIES_READY']
    for old, new in zip(phases, phases[1:]):
        with db.begin() as s:
            api.advance(s, a, old, new, 'e' * 64)
            assert api.entries_ready(s, a) is (new == 'NEW_ENTRIES_READY')


def test_db_unavailable_never_returns_ready(store_api):
    from startup_recovery.types import AccountScope, ManagerToken
    api = store_api
    class Disconnected:
        def execute(self, *_a, **_k):
            raise OSError('connection unavailable')
    t = ManagerToken(AccountScope('ctrader', 'demo', '123'), 'attempt', 1, 'boot')
    with pytest.raises(api.RecoveryError, match='RECOVERY_DB_UNAVAILABLE'):
        api.entries_ready(Disconnected(), t)


def test_migration_creates_empty_fail_closed_tables(store_api, db):
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from models import RecoveryAccount, RecoveryAttempt, RecoveryMutation, RecoveryCheckpointHead
    import importlib.util
    path = Path('migrations/versions/20261001_0029_startup_recovery.py')
    assert path.exists(), 'Recovery migration required'
    spec = importlib.util.spec_from_file_location('recovery_migration', path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.down_revision == '20261001_0028'
    with db.begin() as s:
        connection = s.connection()
        # Test only the additive migration against otherwise-existing baseline tables.
        for model in (RecoveryCheckpointHead, RecoveryMutation, RecoveryAttempt, RecoveryAccount):
            model.__table__.drop(connection)
        schema = connection.get_execution_options()['schema_translate_map'][None]
        connection.execute(text('SET LOCAL search_path TO ' + schema))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        assert s.query(RecoveryAccount).count() == 0
        assert s.query(RecoveryAttempt).count() == 0
        assert s.query(RecoveryMutation).count() == 0
        assert s.query(RecoveryCheckpointHead).count() == 0
    t = begin(store_api, db)
    with db.begin() as s:
        with pytest.raises(store_api.RecoveryError, match='LEGACY_CUTOVER_REQUIRED'):
            store_api.acquire_owner(s, t)


def test_unresolved_authoritative_submission_prevents_cutover(store_api, db):
    from datetime import datetime, timezone
    from models import TradeSubmissionAttempt
    api = store_api
    t = begin(api, db)
    with db.begin() as s:
        s.add(TradeSubmissionAttempt(event_id='evt', mode='LIVE', owner_id='owner',
            account_id='123', symbol='EURUSD', direction='BUY', signal_setup_id='setup',
            idempotency_key='claim', attempt_status='RECONCILIATION_REQUIRED',
            claimed_at=datetime.now(timezone.utc), broker_client_order_id='client',
            request_payload_fingerprint='f' * 64, reconciliation_status='RECONCILIATION_REQUIRED',
            updated_at=datetime.now(timezone.utc)))
    with pytest.raises(api.RecoveryError, match='RECOVERY_OPERATIONS_UNRESOLVED'):
        cutover(api, db, t)
    with db.begin() as s:
        assert api.account_state(s, t.scope).owner_attempt_id is None


def test_older_standby_epoch_cannot_take_over_after_newer_owner_relinquishes(store_api, db):
    from startup_recovery.types import HandoffEvidence
    api = store_api
    a, b = begin(api, db), begin(api, db, 'boot-b')
    cutover(api, db, b)
    with db.begin() as s:
        api.acquire_owner(s, b)
    with db.begin() as s:
        api.relinquish(s, b, HandoffEvidence('graceful-drain', 'd' * 64, b.boot_id, True, True))
    with db.begin() as s:
        with pytest.raises(api.RecoveryError, match='RECOVERY_TOKEN_STALE'):
            api.acquire_owner(s, a)


def test_pending_protection_cannot_be_declared_settled_by_recovery_proof(store_api, db):
    from models import StrategySetupLifecycle
    from test_strategy_studio_position_manager import seed_lifecycle
    t = begin(store_api, db)
    seed_lifecycle(db, account_id='123', account_scope='CTRADER:DEMO:123', tp1_done=True)
    with db.begin() as s:
        s.get(StrategySetupLifecycle, 'setup-1').management_state = {
            'target_protected_sl': 1.1025, 'protection_state': 'PENDING'}
    with pytest.raises(store_api.RecoveryError, match='RECOVERY_OPERATIONS_UNRESOLVED'):
        cutover(store_api, db, t)
