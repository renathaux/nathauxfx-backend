"""Synthetic administrative evidence, disposable PostgreSQL only."""
import importlib
from dataclasses import asdict
from copy import deepcopy
import pytest
from sqlalchemy import event
from test_legacy_adoption_postgres import prepared, db, migrated_database
from startup_recovery import store
from startup_recovery.checkpoints import _encoded, _digest
from startup_recovery.types import RecoveryError
from models import RecoveryAttempt, RecoveryCheckpointHead, RecoveryLegacyAdoption


@pytest.fixture
def request_args(prepared):
    p=prepared
    token=p['token']
    with p['session_factory'].begin() as session:
        account,attempt=store.require_owner(session,token)
        account.phase=attempt.phase='RECOVERY_BLOCKED'
        attempt.conflict_reason='RECOVERY_RECONCILIATION_BLOCKED'
    class Safety:
        def __call__(self,scope):
            snap=p['snapshot_reader'](token)
            return {'database':snap.database,'broker':snap.broker}
        def database(self,session,scope):
            return p['snapshot_reader'].database(session,token)
    evidence=dict(schema=1,kind='operator-termination',scope=asdict(token.scope),
        attempt_id=token.attempt_id,epoch=token.epoch,boot_id=token.boot_id,build_id='build',
        deployment_id='synthetic-predecessor',successor_release_identity='a'*64,
        predecessor_status='TERMINATED',operations_reconciled=True,
        termination_record_sha256='d'*64)
    raw=_encoded(evidence)
    return dict(session_factory=p['session_factory'],scope=token.scope,
        expected_attempt_id=token.attempt_id,expected_epoch=token.epoch,
        predecessor_boot_id=token.boot_id,predecessor_build_id='build',
        predecessor_deployment_id='synthetic-predecessor',successor_release_identity='a'*64,
        evidence_document=raw,expected_evidence_sha256=_digest(raw),safety_reader=Safety())


def release(args):
    try:
        module=importlib.import_module('startup_recovery.owner_handoff')
    except ModuleNotFoundError:
        pytest.fail('Termination-only adapter missing')
    return module.relinquish_terminated_owner(**args)


def test_blocked_owner_relinquishes_only_then_normal_new_attempt_advances_epoch(request_args,prepared):
    args=request_args
    result=release(args)
    assert result.status=='RELINQUISHED'
    with args['session_factory']() as s:
        row=store.account_state(s,args['scope'])
        assert row.owner_attempt_id is None and row.phase=='RELINQUISHED'
        assert row.owner_epoch==row.allocated_epoch==args['expected_epoch']
        assert row.handoff_evidence['evidence_hash']==args['expected_evidence_sha256']
        assert s.query(RecoveryCheckpointHead).count()==s.query(RecoveryLegacyAdoption).count()==0
    with pytest.raises(RecoveryError): release(args)
    with args['session_factory'].begin() as s:
        new=store.begin_attempt(s,args['scope'],'successor-boot','b'*40)
        assert new.epoch==args['expected_epoch']+1
        store.acquire_owner(s,new)
    with args['session_factory']() as s:
        with pytest.raises(RecoveryError): store.require_owner(s,prepared['token'])
        assert not store.entries_ready(s,new)


@pytest.mark.parametrize('field,value',[
    ('expected_attempt_id','wrong'),('expected_epoch',99),('predecessor_boot_id','wrong'),
    ('predecessor_build_id','wrong'),('predecessor_deployment_id','wrong'),
    ('successor_release_identity','b'*64),('expected_evidence_sha256','f'*64),
    ('expected_evidence_sha256',None),
])
def test_mismatched_independent_expectations_block(request_args,field,value):
    request_args[field]=value
    with pytest.raises(RecoveryError): release(request_args)


@pytest.mark.parametrize('field,value',[
    ('predecessor_status','RUNNING'),('operations_reconciled',False),
    ('kind','graceful-drain'),('termination_record_sha256',''),
])
def test_invalid_evidence_is_not_authority_even_if_pinned(request_args,field,value):
    import json
    evidence=json.loads(request_args['evidence_document'])
    evidence[field]=value
    raw=_encoded(evidence)
    request_args.update(evidence_document=raw,expected_evidence_sha256=_digest(raw))
    with pytest.raises(RecoveryError): release(request_args)


@pytest.mark.parametrize('fault',['live_on','account','pending','position','incomplete','unsettled','phase','old_pin'])
def test_fresh_safety_and_owner_gates(request_args,fault):
    original=request_args['safety_reader']
    class Unsafe:
        database=original.database
        def __call__(self,scope):
            state=deepcopy(original(scope))
            d,b=state['database'],state['broker']
            if fault=='live_on': d['settings'][0]['setting_value']='true'
            elif fault=='account': d['selection']['account_id']='other'
            elif fault=='pending': b['orders']=[{'id':'synthetic'}]
            elif fault=='position': b['positions']=[{'id':'synthetic'}]
            elif fault=='incomplete': b['complete']=False
            elif fault=='unsettled': d['submissions']=[{'attempt_status':'SUBMITTING'}]
            return state
    request_args['safety_reader']=Unsafe()
    if fault in {'phase','old_pin'}:
        with request_args['session_factory'].begin() as s:
            row=store.account_state(s,request_args['scope'])
            if fault=='phase':
                row.phase=s.get(RecoveryAttempt,request_args['expected_attempt_id']).phase='STATE_DISCOVERED'
            else:
                row.handoff_evidence={**row.handoff_evidence,'evidence_hash':request_args['expected_evidence_sha256']}
    with pytest.raises(RecoveryError): release(request_args)
    with request_args['session_factory']() as s:
        assert store.account_state(s,request_args['scope']).owner_attempt_id==request_args['expected_attempt_id']


def test_revoked_token_cannot_be_reset_inside_adapter(request_args,prepared):
    from startup_recovery.runtime import invalidate_token,assert_token_usable
    invalidate_token(prepared['token'])
    with pytest.raises(RecoveryError): release(request_args)
    with pytest.raises(RecoveryError): assert_token_usable(prepared['token'])


def test_ambiguous_commit_is_not_retried(request_args):
    factory=request_args['session_factory']
    def lost_ack(session): raise RuntimeError('synthetic private detail')
    event.listen(factory.class_,'after_commit',lost_ack)
    try:
        with pytest.raises(RecoveryError,match='HANDOFF_COMMIT_INSPECT_REQUIRED'):
            release(request_args)
    finally: event.remove(factory.class_,'after_commit',lost_ack)
    with factory() as s:
        assert store.account_state(s,request_args['scope']).owner_attempt_id is None


def test_fresh_admin_process_does_not_reset_old_process_revocation(request_args,prepared):
    import json,subprocess,sys
    from startup_recovery.runtime import invalidate_token,assert_token_usable
    invalidate_token(prepared['token'])
    transport={k:v for k,v in request_args.items() if k not in {'session_factory','safety_reader','scope','evidence_document'}}
    transport.update(scope=asdict(request_args['scope']),
        evidence_document=request_args['evidence_document'].decode(),
        dsn=request_args['session_factory'].kw['bind'].url.render_as_string(hide_password=False),
        snapshot=request_args['safety_reader'](request_args['scope']))
    child='''
import json,sys
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from startup_recovery.types import AccountScope
from startup_recovery.owner_handoff import relinquish_terminated_owner
from models import RuntimeSetting
data=json.load(sys.stdin)
snapshot=data.pop('snapshot')
engine=create_engine(data.pop('dsn'))
factory=sessionmaker(engine)
class Safety:
    def database(self,session,scope):
        result=dict(snapshot['database'])
        row=session.get(RuntimeSetting,'live_auto_trade_enabled')
        result['settings']=[dict(setting_name=row.setting_name,setting_value=row.setting_value)]
        return result
    def __call__(self,scope):
        with factory() as session:
            return dict(database=self.database(session,scope),broker=snapshot['broker'])
data['scope']=AccountScope(**data['scope'])
data['evidence_document']=data['evidence_document'].encode()
try:
    result=relinquish_terminated_owner(session_factory=factory,safety_reader=Safety(),**data)
    assert result.status=='RELINQUISHED'
finally:
    engine.dispose()
'''
    result=subprocess.run([sys.executable,'-B','-c',child],input=json.dumps(transport),text=True,capture_output=True)
    assert result.returncode==0,result.stderr
    with pytest.raises(RecoveryError): assert_token_usable(prepared['token'])
    with request_args['session_factory']() as session:
        assert store.account_state(session,request_args['scope']).owner_attempt_id is None
