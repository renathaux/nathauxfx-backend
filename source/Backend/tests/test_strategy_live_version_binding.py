"""All broker calls are mocks. These tests never activate a hosted strategy."""
import copy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db import Base
from models import ExecutionProtocolState, StrategySetupLifecycle
from services.strategy_studio_models import SavedStrategy
from services.strategy_studio_schema import normalize_definition
from services.strategy_live_binding import (
    freeze_plan, make_entry_binding, dispatch_bound_order, BindingError,
)
from services.trade_submission_service import claim_strategy_submission, mark_request_started
from test_strategy_studio_live_candidate import _definition
from test_broker_execution_metadata import fixture_metadata


@pytest.fixture
def case(tmp_path, monkeypatch):
    monkeypatch.setattr('services.submission_capacity.read_broker_positions',lambda token: [])
    engine = create_engine(f"sqlite:///{tmp_path / 'binding.sqlite'}", connect_args={'check_same_thread': False, 'timeout': 5})
    Base.metadata.create_all(engine)
    from services.submission_reservation import install_reservation_guard
    with engine.begin() as connection:
        install_reservation_guard(connection)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    definition = normalize_definition(_definition())
    definition['tp1'].update(enabled=True, target_r=1.0, close_percent=50.0, protection_r=0.0)
    result = SimpleNamespace(signal='BUY', entry=1.1, sl=1.095, tp1=1.105, tp2=1.11,
                             risk_budget={'method':'PERCENT_BALANCE','value':1.0,'dollars':100.0})
    with factory() as s:
        row = SavedStrategy(strategy_id='saved-A', owner_id='owner-A', name='same name', schema_version=1,
                            definition_json=definition, created_at=now, updated_at=now)
        s.add(row); s.flush()
        binding = make_entry_binding(s, row, freeze_plan(definition, result, symbol='EURUSD', account_balance=10000, account_scope='CTRADER:DEMO:account-A', broker_metadata=fixture_metadata(account_id='account-A')))
        s.add(ExecutionProtocolState(singleton_id=1,protocol_version='indicator-event-execution-v2',updated_at=now))
        s.add(StrategySetupLifecycle(setup_id='setup-A', owner_id='owner-A', strategy_id='saved-A',account_id='account-A',account_scope='CTRADER:DEMO:account-A',symbol='EURUSD',direction='BUY',status='ELIGIBLE',definition_snapshot=definition,entry_binding=binding,updated_at=now))
        s.commit()
    payload=dict(symbol='EURUSD',action='BUY',signal='BUY',entry=1.1,sl=1.095,tp1=1.105,tp2=1.11,
                 execution_source='STRATEGY_STUDIO',studio_owner_id='owner-A',studio_strategy_id='saved-A',studio_setup_id='setup-A',studio_account_scope='CTRADER:DEMO:account-A',
                 studio_binding=binding,studio_risk_method='PERCENT_BALANCE',studio_risk_value=1.0,requested_risk_percent=1.0,
                 risk_amount=100.0,risk_percent=1.0,account_balance_used=10000.0,volume=0.2,volume_units=20000,mode='demo',
                 studio_tp1_enabled=True,tp1_definition=definition['tp1'],fundamental_policy=definition['fundamentals']['mode'],
                 risk={'account_balance':10000.0,'risk_percent':1.0,'risk_amount':100.0,'volume_units':20000,'lot_size':0.2})
    payload['risk']['broker_metadata'] = copy.deepcopy(binding['frozen_plan']['broker_metadata'])
    from recovery_fixture import admitted_manager
    with admitted_manager(factory, 'account-A'):
        yield factory,payload
    engine.dispose()


def claim(factory,payload,owner='owner-A'):
    return claim_strategy_submission('setup-A','account-A','EURUSD','BUY',payload,owner_id=owner,strategy_id=payload['studio_strategy_id'],session_factory=factory)


def test_unchanged_identity_allows_mock_dispatch(case):
    factory,payload=case
    result=claim(factory,payload)
    assert result['ok'], result
    assert mark_request_started(result['idempotency_key'],session_factory=factory)
    broker=Mock(return_value={'ok':True})
    assert dispatch_bound_order(payload,result['idempotency_key'],'owner-A',broker,session_factory=factory)=={'ok':True}
    broker.assert_called_once()


@pytest.mark.parametrize('mutation',['config','updated_at','delete','owner','id'])
def test_changed_saved_row_blocks_new_claim(case,mutation):
    factory,payload=case
    with factory() as s:
        row=s.get(SavedStrategy,'saved-A')
        if mutation=='config':
            d=copy.deepcopy(row.definition_json);d['risk']['value']=2;row.definition_json=d
        elif mutation=='updated_at':row.updated_at+=timedelta(seconds=1)
        elif mutation=='delete':s.delete(row)
        elif mutation=='owner':row.owner_id='owner-B'
        else:row.strategy_id='saved-B'
        s.commit()
    assert not claim(factory,payload)['ok']
    broker = Mock()
    assert not dispatch_bound_order(payload, 'not-claimed', 'owner-A', broker, session_factory=factory)['ok']
    broker.assert_not_called()


def test_edit_after_claim_before_dispatch_never_calls_broker(case):
    factory,payload=case
    claimed=claim(factory,payload);assert claimed['ok']
    assert mark_request_started(claimed['idempotency_key'],session_factory=factory)
    with factory() as s:
        row=s.get(SavedStrategy,'saved-A');row.updated_at+=timedelta(seconds=1);s.commit()
    broker=Mock()
    result=dispatch_bound_order(payload,claimed['idempotency_key'],'owner-A',broker,session_factory=factory)
    assert result['reason']=='STRATEGY_VERSION_CHANGED'
    broker.assert_not_called()


@pytest.mark.parametrize('field',['entry','sl','tp1','tp2','requested_risk_percent','volume_units','risk_amount','mode'])
def test_payload_mutation_after_claim_blocks_dispatch(case,field):
    factory,payload=case
    claimed=claim(factory,payload);assert claimed['ok']
    assert mark_request_started(claimed['idempotency_key'],session_factory=factory)
    payload[field]='live' if field=='mode' else float(payload[field])+1
    broker=Mock()
    assert not dispatch_bound_order(payload,claimed['idempotency_key'],'owner-A',broker,session_factory=factory)['ok']
    broker.assert_not_called()


@pytest.mark.parametrize('field',['owner_id','strategy_id','updated_at','schema_version','config_hash','canonical_version'])
def test_missing_identity_field_fails_closed(case,field):
    factory,payload=case
    del payload['studio_binding']['strategy_identity'][field]
    result=claim(factory,payload)
    assert result['reason']=='STRATEGY_IDENTITY_MISSING'


@pytest.mark.parametrize('field',['frozen_plan','frozen_plan_hash','strategy_identity'])
def test_missing_binding_component_fails_closed(case,field):
    factory,payload=case
    del payload['studio_binding'][field]
    assert claim(factory,payload)['reason']=='STRATEGY_IDENTITY_MISSING'


def test_legacy_setup_never_acquires_current_identity(case):
    factory,payload=case
    with factory() as s:
        s.get(StrategySetupLifecycle,'setup-A').entry_binding=None;s.commit()
    assert claim(factory,payload)['reason']=='STRATEGY_IDENTITY_MISSING'


def test_runtime_owner_mismatch_blocks_broker(case):
    factory,payload=case
    claimed=claim(factory,payload);assert claimed['ok']
    broker=Mock()
    assert not dispatch_bound_order(payload,claimed['idempotency_key'],'owner-B',broker,session_factory=factory)['ok']
    broker.assert_not_called()


def test_same_name_same_config_other_saved_id_cannot_replace_setup(case):
    from services.strategy_live_binding import saved_identity
    factory, payload = case
    with factory() as session:
        original = session.get(SavedStrategy, 'saved-A')
        other = SavedStrategy(strategy_id='saved-B', owner_id=original.owner_id,
                              name=original.name, schema_version=original.schema_version,
                              definition_json=copy.deepcopy(original.definition_json),
                              created_at=original.created_at, updated_at=original.updated_at)
        session.add(other); session.flush()
        payload['studio_strategy_id'] = 'saved-B'
        payload['studio_binding']['strategy_identity'] = saved_identity(session, other)
        session.commit()
    assert claim(factory, payload)['ok'] is False
    broker = Mock()
    assert dispatch_bound_order(payload, 'unclaimed', 'owner-A', broker, session_factory=factory)['ok'] is False
    broker.assert_not_called()


def test_three_positions_still_blocked(case):
    factory,payload=case
    with factory() as s:
        row=s.get(SavedStrategy,'saved-A');d=copy.deepcopy(row.definition_json)
        d['risk'].update(max_concurrent_positions=3,max_combined_open_risk_percent=3);row.definition_json=d;s.flush()
        with pytest.raises(BindingError,match='MULTI_POSITION_NOT_SUPPORTED'):
            make_entry_binding(s,row,payload['studio_binding']['frozen_plan'])


def test_concurrent_edit_rejected_by_durable_dispatch_reservation(case):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    factory,payload=case
    claimed=claim(factory,payload);assert claimed['ok']
    assert mark_request_started(claimed['idempotency_key'],session_factory=factory)
    entered=threading.Event();attempting=threading.Event();committed=threading.Event()
    from sqlalchemy.exc import IntegrityError
    def edit():
        entered.wait(2)
        with factory() as s:
            row=s.get(SavedStrategy,'saved-A');row.updated_at+=timedelta(seconds=1)
            attempting.set()
            with pytest.raises(IntegrityError, match='STRATEGY_EXECUTION_UNRESOLVED'):
                s.commit()
            s.rollback()
    def broker():
        entered.set();assert attempting.wait(2)
        assert not committed.wait(.1)
        return {'ok':True}
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(edit)
        assert dispatch_bound_order(payload,claimed['idempotency_key'],'owner-A',broker,session_factory=factory)['ok']
        future.result(timeout=3)
    assert not committed.is_set()
    with factory() as s:
        assert s.get(SavedStrategy, 'saved-A').updated_at == datetime(2026, 10, 1)


@pytest.mark.parametrize('field',['owner_id','strategy_id','updated_at','schema_version','config_hash','canonical_version'])
def test_persisted_identity_field_missing_blocks_dispatch(case, field):
    factory, payload = case
    claimed = claim(factory,payload); assert claimed['ok']
    assert mark_request_started(claimed['idempotency_key'], session_factory=factory)
    with factory() as s:
        row = s.get(StrategySetupLifecycle, 'setup-A')
        binding = copy.deepcopy(row.entry_binding)
        del binding['strategy_identity'][field]
        row.entry_binding = binding
        s.commit()
    broker = Mock()
    result = dispatch_bound_order(payload, claimed['idempotency_key'], 'owner-A', broker, session_factory=factory)
    assert result['reason'] == 'STRATEGY_IDENTITY_MISSING'
    broker.assert_not_called()


def test_mutated_persisted_plan_blocks_even_when_client_rehashes(case):
    from services.strategy_live_binding import fingerprint
    factory, payload = case
    # A client cannot replace the durable snapshot with a self-consistent hash.
    payload['studio_binding']['frozen_plan']['sl'] = 1.08
    payload['studio_binding']['frozen_plan_hash'] = fingerprint(payload['studio_binding']['frozen_plan'])
    payload['sl'] = 1.08
    assert claim(factory, payload)['reason'] == 'STRATEGY_PLAN_CHANGED'


def test_setup_identity_changes_with_saved_version(case):
    from services.strategy_studio_live_candidate import _setup_id
    _, payload = case
    identity = copy.deepcopy(payload['studio_binding']['strategy_identity'])
    args = dict(owner_id='owner-A', strategy_id='saved-A', schema_version=1,
                account_scope='CTRADER:DEMO:account-A', symbol='EURUSD', direction='BUY',
                structure_event_time='2026-10-01T01:00:00Z', entry_trigger_time='2026-10-01T01:05:00Z',
                broken_level=1.1, evaluator_setup_id='same-signal')
    first = _setup_id(**args, strategy_identity=identity)
    identity['config_hash'] = 'b' * 64
    assert first != _setup_id(**args, strategy_identity=identity)


def test_exact_decimal_canonicalization():
    from services.strategy_live_binding import _emit, _parse
    assert _emit(_parse('{"n":0.123456789012345678901234567890123456789}')) == '{"n":0.123456789012345678901234567890123456789}'
    assert _emit(_parse('{"n":1.000}')) == '{"n":1}'


@pytest.mark.parametrize('mutation', ['missing', 'changed', 'new_retrieval'])
def test_metadata_cannot_be_replaced_between_claim_and_dispatch(case, mutation):
    factory, payload = case
    claimed = claim(factory, payload)
    assert claimed['ok']
    assert mark_request_started(claimed['idempotency_key'], session_factory=factory)
    record = payload['risk']['broker_metadata']
    if mutation == 'missing': del payload['risk']['broker_metadata']
    elif mutation == 'changed': record['fields']['min_volume_units']['value'] = '2000'
    else: record['retrieved_at'] += 1
    broker = Mock()
    result = dispatch_bound_order(payload, claimed['idempotency_key'], 'owner-A', broker, session_factory=factory)
    assert result['reason'] == 'BROKER_METADATA_PLAN_CHANGED'
    broker.assert_not_called()


@pytest.mark.parametrize('source', [None, 'V3B', 'MANUAL'])
def test_studio_source_cannot_be_downgraded(case, monkeypatch, source):
    import api
    _, payload = case
    payload['execution_source'] = source
    prepare = Mock(side_effect=AssertionError('must reject before preparation'))
    broker = Mock(side_effect=AssertionError('must not send'))
    monkeypatch.setattr(api, 'prepare_ctrader_trade', prepare)
    monkeypatch.setattr(api, 'place_market_order_with_inflight_cleanup', broker)
    for result in (api._execute_live_order_core_impl(payload), api.claim_execution_submission(payload)):
        assert result['reason'] == 'STRATEGY_EXECUTION_SOURCE_CHANGED'
    prepare.assert_not_called()
    broker.assert_not_called()


def test_genuine_legacy_source_is_not_subject_to_studio_identity():
    from services.strategy_live_binding import reject_studio_source_downgrade
    reject_studio_source_downgrade(dict(execution_source='V3B', signal_setup_id='legacy',
                                      studio_strategy_id=None, studio_binding=None,
                                      studio_live_ready=False, studio_tp1_enabled=False))


def test_postgres_lock_query_is_owner_scoped_for_update(case):
    from sqlalchemy.dialects import postgresql
    from services.strategy_live_binding import lock_saved
    factory, _ = case
    # Capture the actual ORM query built by lock_saved (SQLite ignores FOR UPDATE;
    # PostgreSQL must emit it). No hosted DB connection is used.
    with factory() as s:
        statements = []
        from sqlalchemy import event
        def capture(orm_state):
            if orm_state.is_select:
                statements.append(str(orm_state.statement.compile(dialect=postgresql.dialect())))
        event.listen(s, 'do_orm_execute', capture)
        lock_saved(s, 'owner-A', 'saved-A')
    assert len(statements) == 1
    assert 'FOR UPDATE' in statements[0]
    assert 'owner_id =' in statements[0] and 'strategy_id =' in statements[0]


@pytest.mark.parametrize('mutation',[None, 'version', 'plan', 'legacy', 'owner'])
def test_actual_api_dispatch_boundary_uses_guard(case, monkeypatch, mutation):
    """Execute the unchanged production dispatch block, isolating unrelated gates.

    No app startup and no network. The real claim/marker/guard use a local DB;
    only the external broker adapter and surrounding application globals mock.
    """
    import ast
    from pathlib import Path
    import services.strategy_live_binding as binding_service
    factory, payload = case
    claimed = claim(factory, payload); assert claimed['ok']
    assert mark_request_started(claimed['idempotency_key'], session_factory=factory)
    owner = 'owner-A'
    if mutation == 'version':
        with factory() as s:
            s.get(SavedStrategy, 'saved-A').updated_at += timedelta(seconds=1); s.commit()
    if mutation == 'plan': payload['sl'] = 1.08
    if mutation == 'legacy': payload.pop('studio_binding')
    if mutation == 'owner': owner = 'owner-B'
    monkeypatch.setattr(binding_service, 'SessionLocal', factory)
    tree = ast.parse((Path(__file__).parents[1] / 'api.py').read_text())
    function = next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_execute_live_order_core_impl')
    blocks = [n for n in function.body if isinstance(n,ast.Try) and any(isinstance(c,ast.Call) and isinstance(c.func,ast.Name) and c.func.id=='dispatch_bound_order' for c in ast.walk(n))]
    assert len(blocks) == 1
    broker = Mock(return_value={'ok':True})
    env = dict(copy=copy, trade_payload=payload, studio_execution=True, submission_key=claimed['idempotency_key'],
               submission_claim=claimed, get_enabled_studio_live_owner=lambda:owner,
               place_market_order_with_inflight_cleanup=broker,
               require_reconciliation=Mock(side_effect=AssertionError('unexpected reconciliation')))
    exec(compile(ast.fix_missing_locations(ast.Module(body=blocks,type_ignores=[])), 'api-dispatch-boundary', 'exec'), env)
    if mutation:
        assert env['result']['ok'] is False
        broker.assert_not_called()
    else:
        assert env['result']['ok'] is True
        broker.assert_called_once()
        for field in ('entry','sl','tp1','tp2','volume','volume_units','risk','mode','action'):
            assert broker.call_args.kwargs[field] == payload[field]


def test_nullable_migration_preserves_legacy_management_state():
    import importlib.util
    from pathlib import Path
    from sqlalchemy import text, inspect
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    path = Path(__file__).parents[1] / 'migrations/versions/20261001_0028_strategy_entry_binding.py'
    spec = importlib.util.spec_from_file_location('binding_migration', path)
    migration = importlib.util.module_from_spec(spec); spec.loader.exec_module(migration)
    assert migration.down_revision == '20260928_0027'
    engine = create_engine('sqlite:///:memory:')
    with engine.begin() as connection:
        connection.execute(text('CREATE TABLE strategy_setup_lifecycle (setup_id TEXT, management_state TEXT)'))
        connection.execute(text('CREATE TABLE trade_submission_attempts (id INTEGER)'))
        connection.execute(text("INSERT INTO strategy_setup_lifecycle VALUES ('old', 'preserved')"))
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        assert connection.execute(text('SELECT management_state, entry_binding FROM strategy_setup_lifecycle')).one() == ('preserved', None)
        assert {'strategy_identity','frozen_plan_hash'} <= {c['name'] for c in inspect(connection).get_columns('trade_submission_attempts')}
        migration.downgrade()
        assert connection.execute(text('SELECT management_state FROM strategy_setup_lifecycle')).scalar() == 'preserved'
    engine.dispose()
