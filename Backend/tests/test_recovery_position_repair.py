"""Original protection only; all broker reads and mutation transport are mocked."""
import copy
import importlib
import json
import time
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from models import TradeSubmissionAttempt, RecoveryAccount, RecoveryAttempt, StrategySetupLifecycle
from test_strategy_live_version_binding import case
from test_recovery_acceptance_crash import prepare
from test_recovery_accepted_execution import evidence
from test_broker_execution_metadata import fixture_metadata, quote
from test_recovery_store import db,store_api
from test_live_identity_metadata_integration import integrated


def api():
    try: return importlib.import_module('services.accepted_position_repair')
    except ModuleNotFoundError: pytest.fail('Task 4 position-bound repair service missing')


@pytest.fixture
def accepted(case):
    from services.submission_intent import prepare_entry,finish_entry
    f,p,key,token=prepare(case)
    prepare_entry(f,token,key,p,'owner-A')
    with f() as s:
        row=s.query(TradeSubmissionAttempt).filter_by(idempotency_key=key).one()
        attempt_id=row.id; client=row.broker_client_order_id
    observed=evidence(); observed['client_order_id']=client
    finish_entry(f,token,key,'ACCEPTED',observed)
    with f.begin() as s:
        s.query(RecoveryAccount).one().phase='POSITION_MANAGEMENT_READY'
        s.get(RecoveryAttempt,token.attempt_id).phase='POSITION_MANAGEMENT_READY'
        saved=copy.deepcopy(s.get(TradeSubmissionAttempt,attempt_id).accepted_execution)
    stamp=time.time()
    q=quote(timestamp=stamp);q.update(account_id='account-A',received_at=stamp)
    position={**observed,'source':'ProtoOAReconcileRes','observed_at':stamp,'sl':None,'tp2':None}
    data=dict(position=position,metadata=fixture_metadata(account_id='account-A'),quote=q)
    sends=[]
    def reader(identity): return copy.deepcopy(data)
    def amend(intent):
        from startup_recovery.runtime import operation_permit
        assert f.kw['bind'].pool.checkedout()==0
        with f() as s:
            assert s.get(TradeSubmissionAttempt,attempt_id).initial_protection['state']=='UNRESOLVED'
        frame=dict(ctidTraderAccountId='account-A',positionId=202,stopLoss='1.095',takeProfit='1.11')
        operation_permit().authorize_frame(SimpleNamespace(_recovery_environment='demo'),2110,frame)
        sends.append(frame)
        data['position'].update(sl='1.095',tp2='1.11',observed_at=time.time())
        return {'ok':True}
    return SimpleNamespace(factory=f,token=token,id=attempt_id,accepted=saved,data=data,reader=reader,amend=amend,sends=sends)


def run(c,**kwargs):
    return api().repair_accepted_position(c.factory,c.token,c.id,c.reader,kwargs.get('amend',c.amend))


def test_original_levels_repaired_while_entries_blocked(accepted):
    c=accepted
    result=run(c)
    assert result['state']=='CONFIRMED',result
    assert len(c.sends)==1
    with c.factory() as s:
        row=s.get(TradeSubmissionAttempt,c.id)
        assert row.attempt_status=='ACCEPTED'
        assert row.initial_protection['state']=='CONFIRMED'
        assert s.query(RecoveryAccount).one().phase=='POSITION_MANAGEMENT_READY'
    assert run(c)['state']=='CONFIRMED'
    assert len(c.sends)==1


@pytest.mark.parametrize('field,value',[('position_id','999'),('account_id','other'),('side','SELL'),('volume_units','10000'),('order_id','999'),('client_order_id','other')])
def test_wrong_broker_identity_denied(accepted,field,value):
    c=accepted;c.data['position'][field]=value
    with pytest.raises(RuntimeError):run(c)
    assert c.sends==[]


@pytest.mark.parametrize('field',['accepted_execution','execution_snapshot','accepted_execution_hash'])
def test_missing_original_evidence_denied(accepted,field):
    c=accepted
    with c.factory.begin() as s:setattr(s.get(TradeSubmissionAttempt,c.id),field,None)
    with pytest.raises(RuntimeError):run(c)
    assert c.sends==[]


def test_current_strategy_edit_does_not_recompute_protection(accepted):
    from services.strategy_studio_models import SavedStrategy
    c=accepted
    with c.factory.begin() as s:s.get(SavedStrategy,'saved-A').definition_json={'unrelated':'new version'}
    assert run(c)['state']=='CONFIRMED'
    assert c.sends[0]['stopLoss']=='1.095'
    assert c.sends[0]['takeProfit']=='1.11'


def test_stronger_stop_never_widened(accepted):
    c=accepted;c.data['position']['sl']='1.099'
    with pytest.raises(RuntimeError,match='PROTECTION'):run(c)
    assert c.sends==[]


@pytest.mark.parametrize('kind',['stale_quote','stale_metadata','fallback','distance','precision','stale_position'])
def test_constraints_fail_closed_without_changing_levels(accepted,kind):
    from live_integrity.metadata import digest,_hash_material
    c=accepted;m=c.data['metadata']
    if kind=='stale_quote':c.data['quote']['bid_timestamp']-=10
    if kind=='stale_position':c.data['position']['observed_at']-=10
    if kind=='stale_metadata':m['retrieved_at']-=100
    if kind=='fallback':m['fields']['tick_size']['authoritative']=False
    if kind=='distance':m['fields']['sl_distance']['value']='100000'
    if kind=='precision':m['fields']['tick_size']['value']='0.01';m['fields']['digits']['value']='2'
    m['metadata_hash']=digest(_hash_material(m));m['retrieval_id']=digest(dict(metadata_hash=m['metadata_hash'],retrieved_at=m['retrieved_at']))
    before=copy.deepcopy(c.accepted)
    with pytest.raises((RuntimeError,ValueError)):run(c)
    assert c.accepted==before and c.sends==[]


def test_ambiguous_amendment_survives_new_session_and_never_resends(accepted):
    c=accepted
    def timeout(intent):
        c.amend(intent)
        c.data['position'].update(sl=None,tp2=None)
        raise TimeoutError('mock lost response')
    assert run(c,amend=timeout)['state']=='UNRESOLVED'
    with c.factory() as s:assert s.get(TradeSubmissionAttempt,c.id).initial_protection['last_result']=='AMBIGUOUS'
    c.factory.kw['bind'].dispose()
    with pytest.raises(RuntimeError,match='UNRESOLVED'):run(c)
    assert len(c.sends)==1


@pytest.mark.parametrize('kind',['NEW_ORDER','wrong_position','extra_volume','wrong_sl','stale_epoch'])
def test_final_wire_boundary_is_position_bound(accepted,kind):
    from startup_recovery.runtime import operation_permit
    c=accepted
    def bad(intent):
        frame=dict(ctidTraderAccountId='account-A',positionId=202,stopLoss='1.095',takeProfit='1.11')
        if kind=='wrong_position':frame['positionId']=999
        if kind=='extra_volume':frame['volume']=100
        if kind=='wrong_sl':frame['stopLoss']='1.09'
        if kind=='stale_epoch':
            with c.factory.begin() as s:s.query(RecoveryAccount).one().owner_epoch+=1
        operation_permit().authorize_frame(SimpleNamespace(_recovery_environment='demo'),2106 if kind=='NEW_ORDER' else 2110,frame)
        c.sends.append(frame)
    assert run(c,amend=bad)['state']=='UNRESOLVED'
    assert c.sends==[]


def test_later_management_intent_blocks_initial_repair(accepted):
    c=accepted
    with c.factory.begin() as s:s.get(StrategySetupLifecycle,'setup-A').management_state={'target_protected_sl':1.099,'protection_state':'PENDING'}
    with pytest.raises(RuntimeError,match='PROTECTION'):run(c)
    assert c.sends==[]


def test_unresolved_initial_repair_blocks_later_management_capability(accepted):
    from startup_recovery.admission import mutation_guard,management_intent_hash
    c=accepted
    api().prepare_repair(c.factory,c.token,c.id,c.reader)
    with c.factory.begin() as s:
        row=s.get(StrategySetupLifecycle,'setup-A')
        row.management_state={'target_protected_sl':1.099,'protection_state':'PENDING','protection_request_sequence':0}
        key=management_intent_hash(row,'AMEND_POSITION')
    with pytest.raises(RuntimeError,match='PROTECTION'):
        with mutation_guard(c.factory,c.token,'AMEND_POSITION',key,setup_id='setup-A',management_intent_id=key):
            pytest.fail('later management acquired competing mutation capability')


def test_crash_after_intent_before_send_does_not_resend(accepted):
    c=accepted;api().prepare_repair(c.factory,c.token,c.id,c.reader)
    c.factory.kw['bind'].dispose()
    with pytest.raises(RuntimeError,match='UNRESOLVED'):run(c)
    assert c.sends==[]


def test_confirmation_after_lost_response_never_sends_again(accepted):
    c=accepted
    def lost(intent):c.amend(intent);raise TimeoutError()
    assert run(c,amend=lost)['state']=='UNRESOLVED'
    assert api().reconcile_initial_repair(c.factory,c.token,c.id,c.reader)['state']=='CONFIRMED'
    assert run(c)['state']=='CONFIRMED' and len(c.sends)==1


def test_db_failure_after_amend_preserves_intent(accepted,monkeypatch):
    c=accepted
    def fail(*args,**kwargs):raise ConnectionError('mock DB unavailable')
    monkeypatch.setattr(api(),'reconcile_initial_repair',fail)
    assert run(c)['state']=='UNRESOLVED'
    with c.factory() as s:assert s.get(TradeSubmissionAttempt,c.id).initial_protection['state']=='UNRESOLVED'
    assert len(c.sends)==1


def test_wrong_attempt_denied(accepted):
    c=accepted
    with pytest.raises(RuntimeError):api().repair_accepted_position(c.factory,c.token,999,c.reader,c.amend)
    assert c.sends==[]


def test_metadata_changes_between_publication_and_wire_block(accepted):
    c=accepted
    def changed(intent):
        from live_integrity.metadata import digest,_hash_material
        m=c.data['metadata'];m['fields']['sl_distance']['value']='90'
        m['metadata_hash']=digest(_hash_material(m));m['retrieval_id']=digest(dict(metadata_hash=m['metadata_hash'],retrieved_at=m['retrieved_at']))
        return c.amend(intent)
    assert run(c,amend=changed)['state']=='UNRESOLVED' and c.sends==[]


def test_production_amend_adapter_has_no_retry_or_entry(accepted,monkeypatch):
    import ctrader_connector as connector
    c=accepted
    assert hasattr(connector,'amend_original_position_protection'),'missing production repair-only adapter'
    # Non-numeric test account is deliberate: fixed-account mismatch must deny
    # before a socket can open, rather than silently selecting the live account.
    opened=Mock();monkeypatch.setattr(connector,'open_ctrader_json_socket',opened)
    monkeypatch.setattr(connector,'get_ctrader_config',lambda:dict(account_id='7',env='demo'))
    assert run(c,amend=connector.amend_original_position_protection)['state']=='UNRESOLVED'
    opened.assert_not_called()


def test_real_repair_connector_exact_wire_and_fixed_account(integrated,monkeypatch):
    import ctrader_connector as connector
    from startup_recovery.runtime import caller_token
    c=integrated;_,payload,claim=c.prepare()
    assert c.dispatch(payload,claim)['broker_result']=='ACCEPTED'
    token=caller_token()
    with c.factory.begin() as s:
        attempt=s.query(TradeSubmissionAttempt).one();attempt_id=attempt.id
        client=attempt.broker_client_order_id
        s.query(RecoveryAccount).one().phase='POSITION_MANAGEMENT_READY'
        s.get(RecoveryAttempt,token.attempt_id).phase='POSITION_MANAGEMENT_READY'
    position=dict(positionId=202,price='1.1001',tradeData=dict(symbolId=1,tradeSide=1,volume=2000000))
    amendments=[]
    old_open=connector.open_ctrader_json_socket;old_wire=connector.send_ctrader_request
    def opened(host,port,**kwargs):
        sock=old_open(host,port,**kwargs);sock._recovery_environment='demo';return sock
    def wire(sock,kind,body,expected):
        if kind==2124:return {'payloadType':2125,'payload':dict(ctidTraderAccountId=7,position=[copy.deepcopy(position)])}
        if kind==2137:return {'payloadType':2138,'payload':dict(ctidTraderAccountId=7,hasMore=False,order=[dict(orderId=101,positionId=202,clientOrderId=client,orderStatus=2)])}
        if kind==2110:
            connector.websocket_send_frame(sock,1,json.dumps(dict(payloadType=2110,payload=body)))
            amendments.append(copy.deepcopy(body))
            position.update(stopLoss=body['stopLoss'],takeProfit=body['takeProfit'])
            return {'payloadType':2126,'payload':{'ctidTraderAccountId':7}}
        assert kind!=2106,'repair emitted NEW_ORDER'
        return old_wire(sock,kind,body,expected)
    monkeypatch.setattr(connector,'open_ctrader_json_socket',opened)
    monkeypatch.setattr(connector,'send_ctrader_request',wire)
    assert api().repair_original_protection(c.factory,token,attempt_id)['state']=='CONFIRMED'
    assert amendments==[dict(ctidTraderAccountId=7,positionId=202,stopLoss=1.0951,takeProfit=1.1101)]
    assert len(c.broker_orders)==1  # fixture's accepted entry only
    assert all(s.close.called for s in c.sockets)


@pytest.fixture
def pg_accepted(accepted,db):
    from sqlalchemy import select
    from db import Base
    c=accepted
    with c.factory() as source,db.begin() as target:
        for table in Base.metadata.sorted_tables:
            rows=[dict(r) for r in source.execute(select(table)).mappings()]
            if rows:target.execute(table.insert(),rows)
    c.factory=db
    return c


def test_postgres_two_workers_only_one_amendment_intent(pg_accepted):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    c=pg_accepted;barrier=Barrier(2)
    def reader(identity):
        barrier.wait(timeout=4)
        return c.reader(identity)
    def worker():
        try:return api().prepare_repair(c.factory,c.token,c.id,reader)
        except RuntimeError as exc:return str(exc)
    with ThreadPoolExecutor(max_workers=2) as pool:
        a=pool.submit(worker);b=pool.submit(worker)
        results=[a.result(timeout=6),b.result(timeout=6)]
    assert sum(isinstance(r,api().RepairPermit) for r in results)==1,results
    assert any(r in ('RECOVERY_ACCOUNT_BUSY','RECOVERY_PROTECTION_UNRESOLVED') for r in results if isinstance(r,str))
    with c.factory() as s:assert s.get(TradeSubmissionAttempt,c.id).initial_protection['state']=='UNRESOLVED'


def test_postgres_handoff_blocked_until_amendment_reconciled(pg_accepted,store_api):
    from startup_recovery.types import HandoffEvidence
    from startup_recovery.unit_of_work import operation_uow
    c=pg_accepted;permit=api().prepare_repair(c.factory,c.token,c.id,c.reader)
    proof=HandoffEvidence('graceful-drain','b'*64,c.token.boot_id,True,True)
    with pytest.raises(RuntimeError,match='UNRESOLVED'):
        with operation_uow(c.factory,c.token) as uow:store_api.relinquish(uow.session,c.token,proof)
    c.data['position'].update(sl='1.095',tp2='1.11',observed_at=time.time())
    assert api().reconcile_initial_repair(c.factory,c.token,c.id,c.reader)['state']=='CONFIRMED'
    with operation_uow(c.factory,c.token) as uow:store_api.relinquish(uow.session,c.token,proof)
    with c.factory.begin() as s:
        b=store_api.begin_attempt(s,c.token.scope,'boot-b','b'*40)
        store_api.acquire_owner(s,b)
    with pytest.raises(RuntimeError):
        permit.authorize_frame(SimpleNamespace(_recovery_environment='demo'),2110,
            dict(ctidTraderAccountId='account-A',positionId=202,stopLoss='1.095',takeProfit='1.11'))
    assert c.sends==[]


def test_repair_transaction_rollback_does_not_publish_intent(accepted,monkeypatch):
    from sqlalchemy import event
    c=accepted
    def fail(session):raise ConnectionError('mock before commit')
    event.listen(c.factory,'before_commit',fail)
    try:
        with pytest.raises(ConnectionError):api().prepare_repair(c.factory,c.token,c.id,c.reader)
    finally:event.remove(c.factory,'before_commit',fail)
    with c.factory() as s:assert s.get(TradeSubmissionAttempt,c.id).initial_protection['state']=='UNASSESSED'
    assert c.sends==[]


def test_sell_stronger_stop_is_not_reset(accepted):
    c=accepted;a=copy.deepcopy(c.accepted);p=copy.deepcopy(c.data['position'])
    a.update(side='SELL',intended_sl='1.11',intended_tp='1.095')
    p.update(side='SELL',sl='1.105')
    with pytest.raises(RuntimeError,match='STRONGER_STOP'):
        api().prepare_initial_repair(a,p,c.data['metadata'],c.data['quote'])


def test_stale_quote_at_final_boundary_denies_send(accepted):
    c=accepted
    def stale(intent):c.data['quote']['ask_timestamp']-=10;return c.amend(intent)
    assert run(c,amend=stale)['state']=='UNRESOLVED' and c.sends==[]


def test_mutated_durable_repair_intent_cannot_be_confirmed(accepted):
    c=accepted;api().prepare_repair(c.factory,c.token,c.id,c.reader)
    with c.factory.begin() as s:
        row=s.get(TradeSubmissionAttempt,c.id)
        material=copy.deepcopy(row.initial_protection);material['intent']['sl']='1.09'
        row.initial_protection=material
    c.data['position'].update(sl='1.09',tp2='1.11',observed_at=time.time())
    with pytest.raises(RuntimeError,match='PROTECTION'):
        api().reconcile_initial_repair(c.factory,c.token,c.id,c.reader)
    assert c.sends==[]


def test_restart_reconciles_without_dead_manager_token_or_send_authority(accepted):
    c=accepted;api().prepare_repair(c.factory,c.token,c.id,c.reader)
    from startup_recovery.runtime import invalidate_token
    invalidate_token(c.token)
    c.data['position'].update(sl='1.095',tp2='1.11',observed_at=time.time())
    assert api().reconcile_initial_repair(c.factory,None,c.id,c.reader)['state']=='CONFIRMED'
    with c.factory() as s:
        account=s.query(RecoveryAccount).one()
        assert account.owner_attempt_id==c.token.attempt_id and account.owner_epoch==c.token.epoch
    assert c.sends==[]
