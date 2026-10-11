"""Committed unresolved intent survives every failure around mocked network I/O."""
import importlib
import pytest
from models import TradeSubmissionAttempt
from startup_recovery.runtime import caller_token
from test_strategy_live_version_binding import case, claim
from test_recovery_accepted_execution import evidence
from test_recovery_store import store_api,db,begin
from test_recovery_fencing import ready,create_submission


def api():
    try: return importlib.import_module('services.submission_intent')
    except ModuleNotFoundError: pytest.fail('Durable pre-send transaction boundary missing')


def prepare(case):
    factory,payload = case
    with factory.kw['bind'].begin() as c: api().install_reservation_guard(c)
    result=claim(factory,payload); assert result['ok']
    return factory,payload,result['idempotency_key'],caller_token()


def test_crash_after_intent_commit_before_call_blocks_replay(case):
    m=api(); factory,payload,key,token=prepare(case)
    permit=m.prepare_entry(factory,token,key,payload,'owner-A')
    with factory() as s:
        attempt=s.query(TradeSubmissionAttempt).filter_by(idempotency_key=key).one()
        assert attempt.send_intent['state']=='UNRESOLVED'
        assert attempt.send_intent['kind']=='SEND_INTENT'
        assert attempt.send_intent['epoch']==token.epoch
    with pytest.raises(RuntimeError,match='RECOVERY_OPERATION_UNRESOLVED'):
        m.prepare_entry(factory,token,key,payload,'owner-A')
    assert not permit.sent


def test_broker_acceptance_before_result_transaction_remains_unresolved(case):
    m=api(); factory,payload,key,token=prepare(case)
    permit=m.prepare_entry(factory,token,key,payload,'owner-A')
    assert factory.kw['bind'].pool.checkedout()==0
    # No result transaction occurs: process dies after accepted response.
    with factory() as s:
        item=s.query(TradeSubmissionAttempt).filter_by(idempotency_key=key).one()
        assert item.send_intent['state']=='UNRESOLVED'
        assert item.accepted_execution is None
    assert permit.operation_key


def test_result_commit_records_original_accepted_identity(case):
    m=api(); factory,payload,key,token=prepare(case)
    permit=m.prepare_entry(factory,token,key,payload,'owner-A')
    accepted=evidence()
    with factory() as s:
        accepted['client_order_id']=s.query(TradeSubmissionAttempt).filter_by(idempotency_key=key).one().broker_client_order_id
    m.finish_entry(factory,token,key,'ACCEPTED',accepted)
    with factory() as s:
        item=s.query(TradeSubmissionAttempt).filter_by(idempotency_key=key).one()
        assert item.accepted_execution['position_id']=='202'
        assert item.send_intent['state']=='ACCEPTED'
        assert item.attempt_status=='ACCEPTED'
    assert factory.kw['bind'].pool.checkedout()==0


def test_failed_result_transaction_cannot_erase_intent(case):
    m=api(); factory,payload,key,token=prepare(case)
    m.prepare_entry(factory,token,key,payload,'owner-A')
    with pytest.raises(RuntimeError):
        m.finish_entry(factory,token,key,'ACCEPTED',{'position_id':'wrong-without-proof'})
    with factory() as s:
        assert s.query(TradeSubmissionAttempt).filter_by(idempotency_key=key).one().send_intent['state']=='UNRESOLVED'


def test_result_ambiguous_never_permits_reentry(case):
    m=api(); factory,payload,key,token=prepare(case)
    m.prepare_entry(factory,token,key,payload,'owner-A')
    m.finish_entry(factory,token,key,'AMBIGUOUS',None)
    with pytest.raises(RuntimeError,match='RECOVERY_OPERATION_UNRESOLVED'):
        m.prepare_entry(factory,token,key,payload,'owner-A')


@pytest.mark.parametrize('field',['send_intent','initial_protection'])
def test_unresolved_intent_blocks_handoff_even_with_accepted_entry_status(store_api,db,field):
    from startup_recovery.types import HandoffEvidence
    a=begin(store_api,db);ready(store_api,db,a)
    key=create_submission(db)
    with db.begin() as s:
        row=s.get(TradeSubmissionAttempt,key)
        row.attempt_status='ACCEPTED';row.reconciliation_status='MATCHED'
        setattr(row,field,{'state':'UNRESOLVED'})
    proof=HandoffEvidence('graceful-drain','d'*64,a.boot_id,True,True)
    with db.begin() as s:
        with pytest.raises(RuntimeError,match='RECOVERY_OPERATIONS_UNRESOLVED'):
            store_api.relinquish(s,a,proof)
    # Fixture represents original-ledger reconciliation, not recovery outcome copying.
    with db.begin() as s: setattr(s.get(TradeSubmissionAttempt,key),field,{'state':'CONFIRMED'})
    with db.begin() as s: store_api.relinquish(s,a,proof)
    b=begin(store_api,db,'boot-b')
    with db.begin() as s: store_api.acquire_owner(s,b)


@pytest.mark.parametrize('sql',["UPDATE saved_strategies SET name='edited' WHERE strategy_id='saved-A'",
                              "DELETE FROM saved_strategies WHERE strategy_id='saved-A'"])
def test_unresolved_entry_reservation_blocks_direct_saved_row_writers(case,sql):
    from sqlalchemy import text
    m=api();factory,payload,key,token=prepare(case)
    assert hasattr(m,'install_reservation_guard'), 'Durable saved-version reservation missing'
    with factory.kw['bind'].begin() as c: m.install_reservation_guard(c)
    m.prepare_entry(factory,token,key,payload,'owner-A')
    with pytest.raises(Exception,match='STRATEGY_EXECUTION_UNRESOLVED'):
        with factory.kw['bind'].begin() as c: c.execute(text(sql))


def test_production_dispatch_commits_intent_and_releases_db_before_callback(case):
    from services.strategy_live_binding import dispatch_bound_order
    from services.trade_submission_service import mark_request_started
    factory,payload,key,token=prepare(case)
    with factory.kw['bind'].begin() as c: api().install_reservation_guard(c)
    assert mark_request_started(key,session_factory=factory)
    called=[]
    def broker():
        assert factory.kw['bind'].pool.checkedout()==0
        with factory() as s:
            item=s.query(TradeSubmissionAttempt).filter_by(idempotency_key=key).one()
            assert item.send_intent['state']=='UNRESOLVED'
        called.append(True)
        return {'ok':False,'broker_result':'AMBIGUOUS','reason':'mock lost response'}
    result=dispatch_bound_order(payload,key,'owner-A',broker,session_factory=factory)
    assert result['broker_result']=='AMBIGUOUS'
    assert called==[True]


def test_postgres_waiting_editor_observes_newly_committed_intent(db):
    """An UPDATE started before intent commit cannot use its old snapshot."""
    from concurrent.futures import ThreadPoolExecutor
    from datetime import datetime, timezone
    from threading import Event
    from sqlalchemy import select, text
    from services.strategy_studio_models import SavedStrategy
    from services.submission_reservation import install_reservation_guard
    now = datetime.now(timezone.utc)
    key = create_submission(db)
    with db.begin() as s:
        install_reservation_guard(s.connection())
        s.add(SavedStrategy(strategy_id='reserved', owner_id='owner', name='original',
            schema_version=1, definition_json={}, created_at=now, updated_at=now))
        row = s.get(TradeSubmissionAttempt, key)
        row.owner_id = 'owner'
        row.strategy_identity = {'strategy_id': 'reserved'}
    waiting = Event()
    def editor():
        with db.begin() as s:
            s.execute(text("SET LOCAL statement_timeout = '3s'"))
            row = s.get(SavedStrategy, 'reserved')
            row.name = 'changed'
            waiting.set()
            s.flush()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with db.begin() as s:
            s.execute(select(SavedStrategy).where(SavedStrategy.strategy_id == 'reserved').with_for_update()).scalar_one()
            s.get(TradeSubmissionAttempt, key).send_intent = {'state': 'UNRESOLVED'}
            s.flush()
            future = pool.submit(editor)
            assert waiting.wait(2)
        with pytest.raises(Exception, match='STRATEGY_EXECUTION_UNRESOLVED'):
            future.result(timeout=4)
    with db.begin() as s:
        assert s.get(SavedStrategy, 'reserved').name == 'original'
        s.get(TradeSubmissionAttempt, key).send_intent = {'state': 'ACCEPTED'}
    with db.begin() as s:
        s.get(SavedStrategy, 'reserved').name = 'after reconciliation'


def test_database_disconnect_after_send_preserves_committed_intent(case):
    from contextlib import contextmanager
    from sqlalchemy.exc import OperationalError
    m=api(); factory,payload,key,token=prepare(case)
    m.prepare_entry(factory,token,key,payload,'owner-A')
    @contextmanager
    def disconnected():
        raise OperationalError('fixture connection lost', None, RuntimeError('offline'))
        yield  # context-manager protocol; never reached
    with pytest.raises(OperationalError):
        m.finish_entry(disconnected,token,key,'ACCEPTED',evidence())
    with factory() as s:
        row=s.query(TradeSubmissionAttempt).filter_by(idempotency_key=key).one()
        assert row.send_intent['state']=='UNRESOLVED'
        assert row.accepted_execution is None
    with pytest.raises(RuntimeError,match='RECOVERY_OPERATION_UNRESOLVED'):
        m.prepare_entry(factory,token,key,payload,'owner-A')


@pytest.mark.parametrize('category', ['ACCEPTED', 'DEFINITELY_REJECTED', 'FAILED_BEFORE_SEND'])
def test_legacy_result_transition_cannot_settle_durable_intent(case, category):
    from services.trade_submission_service import complete_submission
    m=api(); factory,payload,key,token=prepare(case)
    m.prepare_entry(factory,token,key,payload,'owner-A')
    assert complete_submission(key, {'broker_result': category, 'position_id': '202'},
                               session_factory=factory) is False
    with factory() as s:
        row=s.query(TradeSubmissionAttempt).filter_by(idempotency_key=key).one()
        assert row.attempt_status=='SUBMITTING'
        assert row.send_intent['state']=='UNRESOLVED'
        assert row.broker_position_id is None


def test_dispatch_records_acceptance_from_exact_broker_observation(case):
    from services.strategy_live_binding import dispatch_bound_order
    factory,payload,key,token=prepare(case)
    with factory() as s:
        client=s.query(TradeSubmissionAttempt).filter_by(idempotency_key=key).one().broker_client_order_id
    result={'ok': True, 'broker_result': 'ACCEPTED', 'mode': 'demo', 'raw': {
        'ctidTraderAccountId': 'account-A', 'executionType': 'ORDER_FILLED',
        'order': {'orderId': 101, 'clientOrderId': client},
        'position': {'positionId': 202, 'price': '1.10001',
                     'tradeData': {'symbolId': 1, 'tradeSide': 1, 'volume': 2000000}}}}
    assert dispatch_bound_order(payload,key,'owner-A',lambda: result,session_factory=factory)['ok']
    with factory() as s:
        row=s.query(TradeSubmissionAttempt).filter_by(idempotency_key=key).one()
        assert row.accepted_execution['entry']=='1.10001'
        assert row.accepted_execution['volume_units']=='20000'
        assert row.send_intent['state']=='ACCEPTED'


def test_bare_accepted_boolean_cannot_invent_position_evidence(case):
    from services.strategy_live_binding import dispatch_bound_order
    factory,payload,key,token=prepare(case)
    result=dispatch_bound_order(payload,key,'owner-A',lambda: {'ok':True,'broker_result':'ACCEPTED'},session_factory=factory)
    assert result['ok'] is False
    assert result['broker_result']=='AMBIGUOUS'
    with factory() as s:
        row=s.query(TradeSubmissionAttempt).filter_by(idempotency_key=key).one()
        assert row.send_intent['state']=='UNRESOLVED'
        assert row.accepted_execution is None


def test_studio_outer_coordination_does_not_hold_transaction_over_callback(case, monkeypatch):
    from services import account_execution_coordination as coordination
    from contextlib import contextmanager
    factory, _ = case
    @contextmanager
    def forbidden_lock(*args):
        pytest.fail('outer network-duration lock still acquired')
        yield
    monkeypatch.setattr(coordination, 'account_lock', forbidden_lock)
    assert coordination.run_normal_submission(factory, '7', lambda: 'done',
                                               durable_intent=True) == 'done'


@pytest.mark.parametrize('kind', ['exact', 'duplicate', 'truncated', 'missing_original'])
def test_restart_reconciliation_uses_original_attempt_without_resend(case, kind):
    from services import accepted_execution
    assert hasattr(accepted_execution, 'reconcile_accepted_execution')
    m=api(); factory,payload,key,token=prepare(case)
    m.prepare_entry(factory,token,key,payload,'owner-A')
    with factory.begin() as s:
        row=s.query(TradeSubmissionAttempt).filter_by(idempotency_key=key).one()
        original_id=row.id
        observation=evidence();observation['client_order_id']=row.broker_client_order_id
        if kind=='missing_original': row.execution_snapshot=None
    items=[observation,dict(observation,position_id='303')] if kind=='duplicate' else [observation]
    with factory.begin() as s:
        if kind=='exact':
            accepted_execution.reconcile_accepted_execution(s,original_id,
                {'complete':True,'positions':items})
        else:
            with pytest.raises(RuntimeError,match='RECOVERY_'):
                accepted_execution.reconcile_accepted_execution(s,original_id,
                    {'complete':kind!='truncated','positions':items})
    with factory() as s:
        row=s.get(TradeSubmissionAttempt,original_id)
        assert row.send_intent['state']==('ACCEPTED' if kind=='exact' else 'UNRESOLVED')
        assert row.broker_position_id==('202' if kind=='exact' else None)
