"""Original ledger identity only; no broker or current-strategy reconstruction."""
import copy
from datetime import datetime, timezone
import pytest
from models import TradeSubmissionAttempt
from services import trade_submission_service as service
from test_strategy_live_version_binding import case
from test_recovery_store import store_api, db


def api():
    assert hasattr(service, 'capture_execution_snapshot'), 'original execution snapshot boundary missing'
    return service


def row(factory, payload):
    snapshot = api().capture_execution_snapshot(payload)
    with factory.begin() as s:
        now = datetime.now(timezone.utc)
        item = TradeSubmissionAttempt(event_id='e', mode='LIVE', owner_id='owner-A',
            account_id='account-A', symbol='EURUSD', direction='BUY', signal_setup_id='setup-A',
            idempotency_key='original', attempt_status='SUBMITTING', claimed_at=now,
            request_started_at=now, broker_client_order_id='original-client',
            request_payload_fingerprint='a'*64, strategy_identity=snapshot['strategy_identity'],
            frozen_plan_hash=snapshot['frozen_plan_hash'], execution_snapshot=snapshot,
            reconciliation_status='PENDING', updated_at=now)
        s.add(item); s.flush()
        return item.id


def evidence():
    return dict(account_id='account-A', environment='demo', symbol='EURUSD', symbol_id=1,
        side='BUY', client_order_id='original-client', position_id='202', order_id='101',
        entry='1.10001', volume_units='20000', accepted_at='2026-10-01T00:00:00+00:00')


def test_snapshot_is_detached_and_binds_original_plan(case):
    _, payload = case
    snapshot = api().capture_execution_snapshot(payload)
    payload['studio_binding']['frozen_plan']['sl'] = '9'
    assert snapshot['frozen_plan']['sl'] == 1.095  # preserve existing hashed JSON representation
    assert snapshot['account_id'] == 'account-A'
    assert snapshot['symbol_id'] == 1
    assert snapshot['volume_protocol_cents'] == '2000000'


def test_acceptance_is_durable_idempotent_and_not_a_second_outcome_ledger(case):
    factory, payload = case
    key = row(factory, payload)
    with factory.begin() as s:
        first = api().record_accepted_execution(s, key, evidence())
    with factory.begin() as s:
        second = api().record_accepted_execution(s, key, evidence())
        assert second == first
    with factory() as s:
        accepted = api().require_accepted_execution(s.get(TradeSubmissionAttempt, key))
        assert accepted['position_id'] == '202'
        assert accepted['intended_sl'] == '1.095'
        assert accepted['intended_tp'] == '1.11'


@pytest.mark.parametrize('field,value', [('position_id','203'), ('account_id','foreign'),
    ('environment','live'), ('symbol_id',41), ('symbol','XAUUSD'), ('side','SELL'),
    ('client_order_id','foreign'), ('volume_units','10000')])
def test_conflicting_acceptance_never_overwrites_original(case, field, value):
    factory, payload = case
    key = row(factory, payload)
    with factory.begin() as s: api().record_accepted_execution(s, key, evidence())
    changed = evidence(); changed[field] = value
    with pytest.raises(RuntimeError, match='RECOVERY_'):
        with factory.begin() as s: api().record_accepted_execution(s, key, changed)
    with factory() as s: assert s.get(TradeSubmissionAttempt,key).broker_position_id == '202'


@pytest.mark.parametrize('missing', ['execution_snapshot','accepted_execution','accepted_execution_hash'])
def test_legacy_missing_identity_is_never_rebuilt(case, missing):
    factory, payload = case
    key = row(factory, payload)
    with factory.begin() as s:
        api().record_accepted_execution(s, key, evidence())
        setattr(s.get(TradeSubmissionAttempt,key), missing, None)
    with factory() as s:
        with pytest.raises(RuntimeError, match='RECOVERY_ACCEPTED_IDENTITY_UNVERIFIED'):
            api().require_accepted_execution(s.get(TradeSubmissionAttempt,key))


def test_mutated_original_snapshot_is_rejected(case):
    factory, payload = case
    key = row(factory,payload)
    with factory.begin() as s:
        item = s.get(TradeSubmissionAttempt,key)
        snapshot = copy.deepcopy(item.execution_snapshot)
        snapshot['frozen_plan']['sl'] = '1'
        item.execution_snapshot = snapshot
    with pytest.raises(RuntimeError, match='RECOVERY_'):
        with factory.begin() as s: api().record_accepted_execution(s,key,evidence())


def test_additive_migration_preserves_null_legacy_identity(db):
    import importlib.util
    from pathlib import Path
    from sqlalchemy import text
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from test_recovery_fencing import create_submission
    key = create_submission(db)
    spec = importlib.util.spec_from_file_location('accepted_migration',
        Path('migrations/versions/20261001_0030_accepted_execution_recovery.py'))
    migration = importlib.util.module_from_spec(spec); spec.loader.exec_module(migration)
    with db.begin() as s:
        conn = s.connection()
        schema = conn.get_execution_options()['schema_translate_map'][None]
        conn.execute(text('SET LOCAL search_path TO ' + schema))
        for name in ('execution_snapshot','accepted_execution','accepted_execution_hash','initial_protection','send_intent'):
            conn.execute(text('ALTER TABLE trade_submission_attempts DROP COLUMN ' + name))
        with Operations.context(MigrationContext.configure(conn)): migration.upgrade()
    with db() as s:
        item = s.get(TradeSubmissionAttempt,key)
        assert item.execution_snapshot is None and item.accepted_execution is None
        assert item.send_intent is None and item.initial_protection is None
        with pytest.raises(RuntimeError,match='RECOVERY_ACCEPTED_IDENTITY_UNVERIFIED'):
            api().require_accepted_execution(item)
