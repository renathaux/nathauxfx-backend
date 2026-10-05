"""Task 3 production boundaries; external broker transport is always mocked."""
import copy
import json
import pytest
from models import TradeSubmissionAttempt
from models import StrategySetupLifecycle
from test_strategy_live_version_binding import case
from test_live_identity_metadata_integration import integrated
from test_recovery_acceptance_crash import prepare
from test_recovery_accepted_execution import evidence
from test_recovery_store import db,store_api


def test_claim_uses_same_canonical_account_lock_order(case,monkeypatch):
    from startup_recovery.unit_of_work import UnitOfWork
    from test_strategy_live_version_binding import claim
    ranks=[]
    acquire=UnitOfWork.acquire
    def tracked(self,rank,key):
        result=acquire(self,rank,key)
        if result: ranks.append(rank)
        return result
    monkeypatch.setattr(UnitOfWork,'acquire',tracked)
    factory,payload=case
    result=claim(factory,payload)
    assert result['ok'],result
    assert ranks==[1,2,3,4,5,6,7]


def unresolved(case):
    from services.submission_intent import prepare_entry
    factory,payload,key,token=prepare(case)
    prepare_entry(factory,token,key,payload,'owner-A')
    with factory() as s:
        row=s.query(TradeSubmissionAttempt).filter_by(idempotency_key=key).one()
        observed=evidence();observed['client_order_id']=row.broker_client_order_id
    return factory,payload,key,token,observed


@pytest.mark.parametrize('conflict',['missing_order','duplicate_order','duplicate_position'])
def test_reconciliation_rejects_missing_or_already_owned_broker_identity(case,conflict):
    from test_recovery_accepted_execution import row
    from services.trade_submission_service import reconcile_incomplete_submissions
    f,p,key,token,observed=unresolved(case)
    if conflict=='missing_order': observed.pop('order_id')
    else:
        other=row(f,p)
        with f.begin() as s:
            prior=s.get(TradeSubmissionAttempt,other)
            prior.attempt_status='ACCEPTED'
            prior.broker_position_id=observed['position_id'] if conflict=='duplicate_position' else 'another-position'
            prior.broker_order_id=observed['order_id'] if conflict=='duplicate_order' else 'another-order'
    result=reconcile_incomplete_submissions(session_factory=f,
        record_provider=lambda *args:{'ok':True,'complete':True,'records':[observed]})
    assert key in result['unresolved']
    with f() as s:
        attempt=s.query(TradeSubmissionAttempt).filter_by(idempotency_key=key).one()
        assert attempt.send_intent['state']=='UNRESOLVED'
        assert attempt.accepted_execution is None


def test_real_connector_json_enums_pass_one_use_wire_permit(integrated,monkeypatch):
    import ctrader_connector as connector
    c=integrated
    _,payload,claim=c.prepare()
    open_socket=connector.open_ctrader_json_socket
    def socket_with_environment(host,port):
        sock=open_socket(host,port)
        sock._recovery_environment=next(env for env,endpoint in connector.CTRADER_JSON_ENDPOINTS.items() if endpoint==(host,port))
        return sock
    monkeypatch.setattr(connector,'open_ctrader_json_socket',socket_with_environment)
    wire=connector.send_ctrader_request
    def transport(sock,kind,body,expected):
        if kind==2106:
            connector.websocket_send_frame(sock,1,json.dumps({'payloadType':kind,'payload':body}))
        return wire(sock,kind,body,expected)
    monkeypatch.setattr(connector,'send_ctrader_request',transport)
    result=c.dispatch(payload,claim)
    assert result['broker_result']=='ACCEPTED',result
    assert len(c.broker_orders)==1
    with c.factory() as s:
        row=s.query(TradeSubmissionAttempt).filter_by(idempotency_key=claim['idempotency_key']).one()
        assert row.accepted_execution['position_id']=='202'
        assert row.send_intent['state']=='ACCEPTED'
    c.dispatch(payload,claim)
    assert len(c.broker_orders)==1


@pytest.mark.parametrize('kind',['exact','duplicate','truncated','missing_original','wrong_account','empty'])
def test_restart_service_reconciles_durable_intent_without_retry(case,kind):
    from services.trade_submission_service import reconcile_incomplete_submissions
    f,p,key,token,observed=unresolved(case)
    items=[observed]
    if kind=='duplicate': items.append(dict(observed,position_id='303'))
    if kind=='wrong_account': items=[dict(observed,account_id='other')]
    if kind=='empty': items=[]
    if kind=='missing_original':
        with f.begin() as s:
            s.query(TradeSubmissionAttempt).filter_by(idempotency_key=key).one().execution_snapshot=None
    reads=[]
    def provider(account,claimed):
        reads.append(account)
        return {'ok':True,'complete':kind!='truncated','records':items}
    result=reconcile_incomplete_submissions(record_provider=provider,session_factory=f)
    assert reads==['account-A']
    assert result['matched_broker_orders']==([key] if kind=='exact' else [])
    assert result['unresolved']==([] if kind=='exact' else [key])
    with f() as s:
        row=s.query(TradeSubmissionAttempt).filter_by(idempotency_key=key).one()
        assert row.send_intent['state']==('ACCEPTED' if kind=='exact' else 'UNRESOLVED')
        if kind=='exact':
            assert row.accepted_execution['position_id']=='202'
            assert row.reconciliation_status=='MATCHED'
            lifecycle=s.get(StrategySetupLifecycle,row.signal_setup_id)
            assert lifecycle.execution_snapshot['entry']==1.1  # immutable requested entry stays original
            assert row.accepted_execution['entry']=='1.10001'  # actual fill is separate
    # A second startup does not submit or reclaim the resolved/unresolved entry.
    again=reconcile_incomplete_submissions(record_provider=provider,session_factory=f)
    assert again['recovered_unsent']==[]


def test_complete_submission_acknowledges_only_exact_durable_acceptance(case):
    from services.strategy_live_binding import dispatch_bound_order
    from services.trade_submission_service import complete_submission
    f,p,key,token=prepare(case)
    with f() as s: client=s.query(TradeSubmissionAttempt).filter_by(idempotency_key=key).one().broker_client_order_id
    result={'ok':True,'broker_result':'ACCEPTED','mode':'demo','raw':{
        'ctidTraderAccountId':'account-A','executionType':'ORDER_FILLED',
        'order':{'orderId':101,'clientOrderId':client},
        'position':{'positionId':202,'price':'1.10001','tradeData':{'symbolId':1,'tradeSide':1,'volume':2000000}}}}
    dispatch_bound_order(p,key,'owner-A',lambda:result,session_factory=f)
    assert complete_submission(key,result,session_factory=f)
    mutated=copy.deepcopy(result);mutated['raw']['position']['positionId']=999
    assert not complete_submission(key,mutated,session_factory=f)
    assert not complete_submission(key,{'broker_result':'ACCEPTED'},session_factory=f)
    with f() as s:
        row=s.query(TradeSubmissionAttempt).filter_by(idempotency_key=key).one()
        assert row.accepted_execution['position_id']=='202'


@pytest.mark.parametrize('kind',['exact','truncated','duplicate','conflict'])
def test_read_only_history_adapter_preserves_exact_original_order_identity(kind):
    from startup_recovery.readers import BrokerReader
    reader=BrokerReader('7','demo',{},deadline=999999999999)
    trade={'symbolId':1,'tradeSide':1,'volume':2000000}
    position={'positionId':202,'price':'1.10001','tradeData':trade}
    order={'orderId':101,'positionId':202,'clientOrderId':'original-client',
        'orderStatus':'ORDER_STATUS_FILLED','closingOrder':False,
        'executedVolume':2000000,'executionPrice':'1.10001','tradeData':trade}
    positions=[position,copy.deepcopy(position)] if kind=='duplicate' else [position]
    if kind=='conflict': order['tradeData']={**trade,'symbolId':41}
    def request(code,body):
        if code==2114: return {'symbol':[{'symbolId':1,'symbolName':'EURUSD'}]}
        if code==2124: return {'position':positions}
        if code==2137: return {'hasMore':kind=='truncated','order':[order]}
        pytest.fail('unexpected request, especially any broker mutation')
    reader._request=request
    assert hasattr(reader,'submission_records'),'missing production read-only reconciliation adapter'
    if kind!='exact':
        with pytest.raises((ValueError,RuntimeError)):
            reader.submission_records(1000,2000)
        return
    observed=reader.submission_records(1000,2000)
    assert observed==[dict(account_id='7',environment='demo',symbol='EURUSD',symbol_id=1,
        side='BUY',position_id='202',order_id='101',client_order_id='original-client',
        entry='1.10001',volume_units='20000')]


def test_reconciliation_coordination_uses_existing_postgres_account_lock(db):
    from concurrent.futures import ThreadPoolExecutor
    from startup_recovery import unit_of_work as uow
    from startup_recovery.types import AccountScope
    scope=AccountScope('ctrader','demo','7')
    assert hasattr(uow,'reconciliation_uow'),'missing short recovery-account reconciliation transaction'
    def other():
        with pytest.raises(RuntimeError,match='RECOVERY_ACCOUNT_BUSY'):
            with uow.reconciliation_uow(db,scope): pytest.fail('competing account lock acquired')
    with uow.reconciliation_uow(db,scope):
        with ThreadPoolExecutor(max_workers=1) as pool: pool.submit(other).result(3)
        with pytest.raises(RuntimeError,match='NETWORK_IN_TRANSACTION'):uow.require_network_boundary()
    with uow.reconciliation_uow(db,scope): pass


def test_startup_reconciles_before_owner_acquisition_but_never_grants_takeover(store_api,db):
    from test_recovery_startup import dependencies,prepare_cutover
    from test_recovery_reconciliation import SCOPE
    from startup_recovery.coordinator import recover
    prepare_cutover(store_api,db)
    with db.begin() as s:
        old=store_api.begin_attempt(s,SCOPE,'old-process','a'*40)
        store_api.acquire_owner(s,old)
    trace=[]; deps=dependencies(db,trace)
    deps.reconcile_submissions=lambda: trace.append('reconcile-existing-ledger') or {'ok':True}
    result=recover(deps,SCOPE,'restart','a'*40)
    assert trace==['reconcile-existing-ledger']
    assert not result.ready and result.reason=='RECOVERY_OWNER_BUSY'
    with db() as s: assert store_api.account_state(s,SCOPE).owner_attempt_id==old.attempt_id


def test_production_dependencies_route_selected_account_to_durable_reconciliation(case):
    from startup_recovery.server_adapter import ProductionDependencies
    from types import SimpleNamespace
    f,p,key,token,observed=unresolved(case)
    deps=ProductionDependencies(api_module=SimpleNamespace(),engine=f.kw['bind'],
        session_factory=f,scope=token.scope,build={'backend_git_sha':'a'*40})
    calls=[]
    def read(scope,claimed):
        assert scope==token.scope and claimed is not None
        calls.append(scope)
        assert f.kw['bind'].pool.checkedout()==0
        return {'ok':True,'complete':True,'records':[observed]}
    deps.readers=SimpleNamespace(submissions=read)
    assert hasattr(deps,'reconcile_submissions')
    assert deps.reconcile_submissions()['matched_broker_orders']==[key]
    assert len(calls)==1


def test_matched_reader_recognizes_verified_acceptance_without_double_pending_count(case):
    from services.submission_intent import finish_entry
    from startup_recovery.reconcile import DiscoverySnapshot,reconcile
    from test_recovery_reconciliation import empty
    f,p,key,token,observed=unresolved(case)
    finish_entry(f,token,key,'ACCEPTED',observed)
    with f() as s:
        row=s.query(TradeSubmissionAttempt).filter_by(idempotency_key=key).one()
        record={column.name:copy.deepcopy(getattr(row,column.name)) for column in row.__table__.columns}
    from datetime import datetime
    for k,v in record.items():
        if isinstance(v,datetime):record[k]=v.isoformat()
    d,b=empty();d['selection'].update(account_id='account-A',scope='CTRADER:DEMO:account-A');b['account_id']='account-A'
    d['submissions']=[record]
    result=reconcile(DiscoverySnapshot.create(token,d,b),{})
    assert 'RECOVERY_OPERATIONS_UNRESOLVED' not in result.conflicts
    assert result.capacity_used==0  # accepted ledger isn't an extra pending reservation
    record['accepted_execution_hash']='tampered'
    result=reconcile(DiscoverySnapshot.create(token,d,b),{})
    assert 'RECOVERY_OPERATIONS_UNRESOLVED' in result.conflicts


def test_legacy_entry_cannot_send_without_immutable_committed_intent(store_api,db):
    from test_recovery_fencing import begin,ready,create_submission
    from startup_recovery.runtime import manager_context
    from startup_recovery.admission import dispatch_claimed_order
    token=begin(store_api,db);ready(store_api,db,token);create_submission(db)
    calls=[]
    with manager_context(token):
        with pytest.raises(RuntimeError,match='RECOVERY_EXECUTION_IDENTITY_UNVERIFIED'):
            dispatch_claimed_order(db,'claim',lambda:calls.append('broker-send'))
    assert calls==[]


def test_old_private_dispatch_cannot_keep_database_transaction_during_send(case):
    from services.strategy_live_binding import _dispatch_bound_order
    from services.trade_submission_service import mark_request_started
    f,p,key,token=prepare(case)
    assert mark_request_started(key,session_factory=f)
    def broker():
        assert f.kw['bind'].pool.checkedout()==0
        with f() as s: assert s.query(TradeSubmissionAttempt).filter_by(idempotency_key=key).one().send_intent['state']=='UNRESOLVED'
        return {'ok':False,'broker_result':'AMBIGUOUS'}
    assert _dispatch_bound_order(p,key,'owner-A',broker,session_factory=f)['broker_result']=='AMBIGUOUS'


def test_restart_position_uses_actual_accepted_fill_not_requested_entry(case):
    from services.submission_intent import finish_entry
    from startup_recovery.reconcile import DiscoverySnapshot,reconcile
    from test_recovery_reconciliation import empty
    from datetime import datetime
    f,p,key,token,observed=unresolved(case)
    finish_entry(f,token,key,'ACCEPTED',observed)
    def view(row):
        return {c.name:(v.isoformat() if isinstance(v,datetime) else copy.deepcopy(v))
            for c in row.__table__.columns for v in [getattr(row,c.name)]}
    with f() as s:
        row=s.query(TradeSubmissionAttempt).filter_by(idempotency_key=key).one()
        attempt=view(row);lifecycle=view(s.get(StrategySetupLifecycle,row.signal_setup_id))
    d,b=empty();d['selection'].update(account_id='account-A',scope='CTRADER:DEMO:account-A');b['account_id']='account-A'
    d['submissions']=[attempt];d['lifecycles']=[lifecycle]
    metadata=p['studio_binding']['frozen_plan']['broker_metadata']
    b['metadata']={'EURUSD':metadata}
    b['observed_at']=b['history_to']=metadata['retrieved_at']
    b['positions']=[dict(position_id='202',symbol='EURUSD',symbol_id=1,side='BUY',
        entry='1.10001',sl='1.095',tp2='1.11',volume_units='20000')]
    result=reconcile(DiscoverySnapshot.create(token,d,b),{})
    assert result.ok,result.conflicts
    assert result.capacity_used==1
    assert result.positions[0]['execution_snapshot']['entry']=='1.10001'
    assert lifecycle['execution_snapshot']['entry']==1.1


@pytest.mark.parametrize('failure',['process_dies','response_lost','database_fails'])
def test_real_send_then_failed_finalization_never_repeats_new_order(integrated,monkeypatch,failure):
    import ctrader_connector as connector
    from services import submission_intent
    from sqlalchemy.exc import OperationalError
    c=integrated;_,payload,claim=c.prepare()
    if failure=='response_lost':
        wire=connector.send_ctrader_request
        def lost(sock,kind,body,expected):
            result=wire(sock,kind,body,expected)
            if kind==2106: raise TimeoutError('mock lost response')
            return result
        monkeypatch.setattr(connector,'send_ctrader_request',lost)
    else:
        def failed_commit(*args,**kwargs):
            if failure=='process_dies': raise SystemExit('mock process termination')
            raise OperationalError('mock transaction B',None,RuntimeError('mock disconnected'))
        monkeypatch.setattr(submission_intent,'finish_entry',failed_commit)
    try: c.dispatch(payload,claim)
    except (SystemExit,OperationalError): pass
    assert len(c.broker_orders)==1
    with c.factory() as s:
        row=s.query(TradeSubmissionAttempt).filter_by(idempotency_key=claim['idempotency_key']).one()
        assert row.send_intent['state']=='UNRESOLVED'
        assert row.accepted_execution is None
    again=c.dispatch(payload,claim)
    assert not again['ok']
    assert len(c.broker_orders)==1


def test_repeated_exact_result_commit_is_idempotent(case):
    from services.submission_intent import finish_entry
    f,p,key,token,observed=unresolved(case)
    finish_entry(f,token,key,'ACCEPTED',observed)
    finish_entry(f,token,key,'ACCEPTED',observed)
    with f() as s:
        row=s.query(TradeSubmissionAttempt).filter_by(idempotency_key=key).one()
        assert row.send_intent['state']=='ACCEPTED'
        assert row.accepted_execution['position_id']=='202'
