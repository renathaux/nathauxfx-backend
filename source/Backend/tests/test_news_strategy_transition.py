"""Administrative transition guards on disposable PostgreSQL, never production."""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import importlib.util

import pytest
from sqlalchemy import event

from models import RecoveryAccount, RecoveryAttempt
from startup_recovery.types import ManagerToken, HandoffEvidence, Phase, RecoveryError
from startup_recovery.store import scope_key
from test_recovery_store import db, store_api
from test_news_strategy_compatibility import fixture
from test_recovery_reconciliation import SCOPE


def api():
    assert importlib.util.find_spec('startup_recovery.news_strategy_transition'), 'Administrative transition missing'
    from startup_recovery import news_strategy_transition
    return news_strategy_transition


@pytest.mark.parametrize('statement', [
    'from startup_recovery.news_strategy_transition import require_relinquished',
    'from startup_recovery.news_strategy_transition import transition as adopt',
    'from .news_strategy_transition import *',
    'from startup_recovery import news_strategy_transition as authority',
    'import startup_recovery.news_strategy_transition',
    'importlib.import_module("startup_recovery." + "news_strategy_transition")',
    'name = "news_strategy_" + "transition"',
])
def test_startup_cannot_import_administrative_identity_transition(statement):
    from tests.cutover_startup_guard import check_startup
    with pytest.raises(AssertionError): check_startup(statement,'server_adapter')


def test_administrative_authority_reader_can_reuse_only_pure_safety_projection():
    from pathlib import Path
    from tests.cutover_startup_guard import check_startup
    check_startup(Path('startup_recovery/news_strategy_transition.py').read_text(),'news_strategy_transition')


@pytest.mark.parametrize('symbol',['relinquish_terminated_owner','relinquish_terminated_runtime_owner',
                                  'relinquish_terminated_ready_owner','_runtime_authority'])
def test_identity_transition_cannot_import_owner_mutation_adapters(symbol):
    from tests.cutover_startup_guard import check_startup
    code='def validate_current_authority():\n    from startup_recovery.owner_handoff import '+symbol+'\n'
    with pytest.raises(AssertionError): check_startup(code,'news_strategy_transition')


@pytest.mark.parametrize('statement', [
    'from startup_recovery.store import acquire_owner as _runtime_safety',
    '_runtime_safety = dangerous',
    'import dangerous as _runtime_safety',
])
@pytest.mark.parametrize('placement',['module','function'])
def test_transition_safety_name_cannot_be_rebound(statement,placement):
    from tests.cutover_startup_guard import check_startup
    code=('def validate_current_authority():\n    '+statement+'\n    _runtime_safety(value,scope)'
        if placement=='function' else statement+'\ndef validate_current_authority():\n    _runtime_safety(value,scope)')
    with pytest.raises(AssertionError): check_startup(code,'news_strategy_transition')


@pytest.mark.parametrize('code', [
    'def validate_current_authority(_runtime_safety):\n    _runtime_safety()',
    'def _runtime_safety():\n    dangerous()\ndef validate_current_authority():\n    _runtime_safety()',
    'class _runtime_safety: pass\ndef validate_current_authority():\n    _runtime_safety()',
])
def test_transition_safety_binding_forms_are_closed(code):
    from tests.cutover_startup_guard import check_startup
    with pytest.raises(AssertionError): check_startup(code,'news_strategy_transition')


def test_startup_package_alias_cannot_reach_transition():
    from tests.cutover_startup_guard import check_startup
    with pytest.raises(AssertionError):
        check_startup('import startup_recovery as recovery\nrecovery.news_strategy_transition.apply_transition()',
                      'server_adapter')


def isolation(document):
    return dict(schema=1,kind='news-strategy-maintenance-isolation',scope_key=document['scope_key'],
        predecessor_attempt_id=document['predecessor_attempt_id'],predecessor_epoch=document['predecessor_epoch'],
        predecessor_boot_id='prior-boot',predecessor_build_id='prior-build',
        handoff_evidence_hash=document['handoff_evidence_hash'],maintenance_deployment_id='maintenance-deployment',
        maintenance_image_digest='sha256:'+'1'*64,maintenance_build_identity='2'*64,
        predecessor_terminated=True,maintenance_only=True,live_auto=False,operator_record_sha256='3'*64)


@pytest.mark.parametrize('field,value', [
    ('maintenance_only',False),('predecessor_terminated',False),('live_auto',True),
    ('predecessor_epoch',6),('predecessor_attempt_id','historical'),
    ('handoff_evidence_hash','4'*64),('maintenance_image_digest','latest'),
    ('maintenance_build_identity','4'*64),('operator_record_sha256',None),
    ('extra',True),
])
def test_independently_pinned_isolation_rejects_invalid_claim(monkeypatch,field,value):
    from startup_recovery.checkpoints import _encoded,_digest
    _,document,*_=fixture(monkeypatch)
    proof=isolation(document)
    proof[field]=value
    raw=_encoded(proof)
    with pytest.raises(RecoveryError):
        api().validate_isolation(raw,_digest(raw),document,
            maintenance_image_digest='sha256:'+'1'*64,maintenance_build_identity='2'*64)


def test_isolation_evidence_needs_independent_pin_and_exact_release(monkeypatch):
    from startup_recovery.checkpoints import _encoded,_digest
    _,document,*_=fixture(monkeypatch)
    raw=_encoded(isolation(document))
    assert api().validate_isolation(raw,_digest(raw),document,
        maintenance_image_digest='sha256:'+'1'*64,maintenance_build_identity='2'*64)==isolation(document)
    with pytest.raises(RecoveryError):
        api().validate_isolation(raw,'f'*64,document,
            maintenance_image_digest='sha256:'+'1'*64,maintenance_build_identity='2'*64)


@pytest.fixture
def relinquished(db, monkeypatch):
    _, document, *_ = fixture(monkeypatch)
    token = ManagerToken(SCOPE, document['predecessor_attempt_id'], document['predecessor_epoch'], 'prior-boot')
    with db.begin() as session:
        session.add(RecoveryAccount(scope_key=scope_key(SCOPE), **asdict(SCOPE),
            allocated_epoch=token.epoch, owner_epoch=token.epoch, owner_attempt_id=None,
            phase=Phase.RELINQUISHED, accepted_manifest_hash=None,
            handoff_evidence=asdict(HandoffEvidence('graceful-drain',
                document['handoff_evidence_hash'], token.boot_id, True, True))))
        session.flush()
        session.add(RecoveryAttempt(attempt_id=token.attempt_id, scope_key=scope_key(SCOPE),
            epoch=token.epoch, boot_id=token.boot_id, build_id='prior-build',
            phase=Phase.RELINQUISHED, phase_evidence_hash=document['accepted_manifest_hash'],
            conflict_reason=None, created_at=datetime.now(timezone.utc)))
    return db, document, token


def test_predecessor_guard_reads_exact_relinquished_identity_without_mutation(relinquished):
    factory, document, token = relinquished
    statements=[]
    engine=factory.kw['bind']
    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(engine, 'before_cursor_execute', record)
    try:
        with factory.begin() as session:
            assert api().require_relinquished(session, SCOPE, document,
                predecessor_boot_id=token.boot_id, predecessor_build_id='prior-build') == token
            assert not session.new and not session.dirty and not session.deleted
    finally:
        event.remove(engine, 'before_cursor_execute', record)
    assert statements and all(s.lstrip().upper().startswith('SELECT') for s in statements)


@pytest.mark.parametrize('model,field,value', [
    ('account','owner_attempt_id','unexpected-owner'),
    ('account','phase','NEW_ENTRIES_READY'),
    ('account','owner_epoch',9), ('account','allocated_epoch',9),
    ('account','accepted_manifest_hash','a'*64),
    ('account','handoff_evidence',None),
    ('attempt','phase','NEW_ENTRIES_READY'),
    ('attempt','boot_id','other-boot'), ('attempt','build_id','other-build'),
    ('attempt','phase_evidence_hash','e'*64),
    ('attempt','conflict_reason','RECOVERY_BLOCKED'),
])
def test_predecessor_guard_blocks_any_identity_drift(relinquished,model,field,value):
    factory, document, token = relinquished
    with factory.begin() as session:
        row=session.get(RecoveryAccount,scope_key(SCOPE)) if model=='account' else session.get(RecoveryAttempt,token.attempt_id)
        setattr(row,field,value)
    with factory.begin() as session, pytest.raises(RecoveryError):
        api().require_relinquished(session,SCOPE,document,
            predecessor_boot_id=token.boot_id,predecessor_build_id='prior-build')


def test_predecessor_guard_nowait_blocks_competing_account_lock(relinquished):
    from startup_recovery import store
    factory, document, token = relinquished
    with factory.begin() as first:
        store.account_state(first,SCOPE,lock=True)
        with factory.begin() as second, pytest.raises(RecoveryError,match='RECOVERY_OWNER_BUSY'):
            api().require_relinquished(second,SCOPE,document,
                predecessor_boot_id=token.boot_id,predecessor_build_id='prior-build')


@pytest.fixture
def selected(relinquished):
    from services.strategy_studio_models import SavedStrategy, StrategyStudioSelection
    from models import StrategyStudioLiveState
    factory, document, token = relinquished
    with factory.begin() as session:
        for field in ('predecessor_row','successor_row'):
            r=deepcopy(document[field])
            r['updated_at']=datetime.fromisoformat(r['updated_at'])
            session.add(SavedStrategy(**r,name=field,created_at=r['updated_at']))
        session.flush()
        old=document['predecessor_row']
        stamp=datetime.fromisoformat(document['studio_before']['updated_at'])
        session.add(StrategyStudioSelection(owner_id=old['owner_id'],strategy_id=old['strategy_id'],
            activated_at=stamp,updated_at=stamp))
        studio=deepcopy(document['studio_before'])
        for name in ('enabled_at','updated_at'): studio[name]=datetime.fromisoformat(studio[name])
        session.add(StrategyStudioLiveState(**studio))
    return factory, document, token


def test_strategy_locks_preserve_rows_and_normal_customer_selection(selected):
    factory, document, _ = selected
    with factory.begin() as session:
        selection, studio = api().lock_strategy_identity(session,document)
        assert selection.strategy_id == document['predecessor_row']['strategy_id']
        assert studio.enabled is True and studio.enabled_strategy_id == selection.strategy_id
        assert not session.new and not session.dirty and not session.deleted


@pytest.mark.parametrize('change', ['definition','saved_timestamp','selection','disabled','studio_pointer','enabled_at'])
def test_strategy_locks_block_concurrent_identity_edits(selected,change):
    from services.strategy_studio_models import SavedStrategy, StrategyStudioSelection
    from models import StrategyStudioLiveState
    factory, document, _ = selected
    with factory.begin() as session:
        old=session.get(SavedStrategy,'old')
        selection=session.get(StrategyStudioSelection,'owner')
        studio=session.get(StrategyStudioLiveState,'owner')
        if change=='definition': old.definition_json={}
        if change=='saved_timestamp': old.updated_at=datetime.now(timezone.utc)
        if change=='selection': selection.strategy_id='new'
        if change=='disabled': studio.enabled=False
        if change=='studio_pointer': studio.enabled_strategy_id='new'
        if change=='enabled_at': studio.enabled_at=datetime.now(timezone.utc)
    with factory.begin() as session, pytest.raises(RecoveryError):
        api().lock_strategy_identity(session,document)


def test_strategy_locks_block_competing_edit_without_wait(selected):
    from services.strategy_studio_models import SavedStrategy
    from sqlalchemy import select
    factory, document, _ = selected
    with factory.begin() as first:
        first.scalar(select(SavedStrategy).where(SavedStrategy.strategy_id=='new').with_for_update())
        with factory.begin() as second, pytest.raises(RecoveryError,match='NEWS_STRATEGY_TRANSITION_BUSY'):
            api().lock_strategy_identity(second,document)


@pytest.fixture(params=['adopted_five', 'runtime_six'])
def authority(selected,monkeypatch,tmp_path,request):
    import os
    from time import time
    from models import RecoveryLegacyAdoption, RecoveryCheckpointHead
    from startup_recovery import adoption_evidence as adoption, checkpoints as files
    from test_legacy_adoption_semantics import inputs
    assert hasattr(os,'O_NOATIME'), 'Strict Linux storage is required'
    factory, document, token=selected
    _, _, before, _, _, receipt=fixture(monkeypatch)
    payloads,_,broker=inputs()
    binding=adoption.AdoptionBinding.create(document['source_envelope']['adoption']['binding'])
    envelopes={}
    for kind,payload in payloads.items():
        identity=deepcopy(document['source_envelope']['identity'])
        identity['dependencies_hash']=binding.value['dependency_hashes'][kind]
        envelopes[kind]=adoption.adopted_envelope(kind,payload,identity,binding,
            timestamp='2020-01-02T00:00:00Z')
    root=tmp_path/'state'
    root.mkdir(mode=0o700)
    (root/'.cutover').mkdir(mode=0o700)
    lock=os.open(root/'.cutover'/'lock',os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    os.close(lock)
    manifest=files.write_generation(root,envelopes,None)
    assert manifest.manifest_hash==document['source_head']['manifest_hash']
    heads=[]
    with factory.begin() as session:
        session.add(RecoveryAttempt(attempt_id=receipt['attempt_id'],scope_key=scope_key(SCOPE),
            epoch=receipt['epoch'],boot_id=receipt['boot_id'],build_id=receipt['build_id'],
            phase=Phase.RELINQUISHED,created_at=datetime.now(timezone.utc)))
        session.flush()
        session.add(RecoveryLegacyAdoption(**receipt,created_at=datetime.now(timezone.utc)))
        for kind,envelope in envelopes.items():
            body=files._parse(envelope.raw)
            head=dict(scope_key=scope_key(SCOPE),kind=kind,manifest_hash=manifest.manifest_hash,
                file_hash=files._digest(envelope.raw),generation=1,identity=body['identity'],
                admission_hash=body['admission_hash'])
            heads.append(head)
            session.add(RecoveryCheckpointHead(**head))
        if request.param == 'runtime_six':
            from startup_recovery.reconcile import checkpoint_dependencies
            # Disposable fixture only: retained adoption plus the normal runtime
            # monthly stream, with its own explicit generation and admission pin.
            kind='live_monthly_history'
            identity=dict(account_scope='CTRADER:DEMO:7',owner_id='owner',symbol=None,
                strategy_id=None,config_hash=None,position_id=None,epoch=token.epoch,
                boot_id=token.boot_id,build_id='prior-build',generation=1,
                dependencies_hash=checkpoint_dependencies(kind,before,{}))
            envelope=files.produced_envelope(kind,{},identity,parent_hash=None,
                admission_hash=document['accepted_manifest_hash'],produced_event_id='fixture',
                timestamp='2026-10-16T00:00:00Z')
            runtime_manifest=files.write_generation(root,{kind:envelope},None)
            head=dict(scope_key=scope_key(SCOPE),kind=kind,manifest_hash=runtime_manifest.manifest_hash,
                file_hash=files._digest(envelope.raw),generation=1,identity=identity,
                admission_hash=document['accepted_manifest_hash'])
            heads.append(head)
            session.add(RecoveryCheckpointHead(**head))
    before['checkpoint_heads']=heads
    before['news_strategy_compatibility']=[]
    broker['observed_at']=broker['history_to']=time()
    return factory,document,token,root,dict(database=before,broker=broker)


def test_relinquished_current_authority_preserves_historical_news(authority):
    from startup_recovery.cutover_evidence import _VerificationTree
    factory,doc,token,root,observation=authority
    with factory.begin() as session, _VerificationTree(root) as tree, tree.shared():
        candidates, projection=api().validate_current_authority(tree,session,token,'prior-build',doc,observation)
        from startup_recovery.checkpoints import _parse
        assert _parse(candidates['news_trading_state'].raw)==doc['source_envelope']
        assert len(projection['heads'])==len(observation['database']['checkpoint_heads'])
        assert len(projection['heads']) in (5,6)
        assert not session.new and not session.dirty and not session.deleted


@pytest.mark.parametrize('change',['missing_file','wrong_head','other_snapshot','live_on','position'])
def test_relinquished_authority_requires_complete_current_evidence(authority,change):
    from startup_recovery.cutover_evidence import _VerificationTree
    from models import RecoveryCheckpointHead
    factory,doc,token,root,observation=authority
    if change=='missing_file':
        (root/'.recovery-generations'/doc['source_head']['manifest_hash']/'live_backup.json').unlink()
    if change=='wrong_head':
        with factory.begin() as session:
            session.get(RecoveryCheckpointHead,(scope_key(SCOPE),'news_trading_state')).file_hash='f'*64
    if change=='other_snapshot': observation['database']['checkpoint_heads']=[]
    if change=='live_on': observation['database']['settings'][0]['setting_value']='true'
    if change=='position': observation['broker']['positions']=[{'position_id':'unexpected'}]
    with factory.begin() as session, _VerificationTree(root) as tree, tree.shared(), pytest.raises(RecoveryError):
        api().validate_current_authority(tree,session,token,'prior-build',doc,observation)


def test_detached_successor_projection_preserves_all_historical_checkpoint_bytes(authority):
    from startup_recovery.cutover_evidence import _VerificationTree
    factory,doc,token,root,observation=authority
    before=deepcopy(observation)
    with factory.begin() as session, _VerificationTree(root) as tree, tree.shared():
        candidates,_=api().validate_current_authority(tree,session,token,'prior-build',doc,observation)
        raw={k:c.raw for k,c in candidates.items()}
        after=api().project_successor(doc,observation,candidates)
    assert observation==before
    assert {k:c.raw for k,c in candidates.items()}==raw
    assert after['strategy_selections'][0]['strategy_id']=='new'
    assert after['studio_live_state']==[doc['studio_after']]
    assert after['settings']==before['database']['settings']
    assert after['news_strategy_compatibility']==[doc]


@pytest.fixture
def transition_args(authority):
    from time import time
    from sqlalchemy import select,cast,Text
    from models import StrategyStudioLiveState, RecoveryNewsStrategyCompatibility
    from services.strategy_studio_models import StrategyStudioSelection
    from startup_recovery.checkpoints import _parse,_encoded,_digest
    from startup_recovery.cutover_evidence import _VerificationTree
    from startup_recovery.news_strategy_compatibility import validate_document, receipt_digest
    from startup_recovery.reconcile import digest
    factory,doc,token,root,base=authority
    class Reader:
        calls=0
        def database(self,session,scope):
            d=deepcopy(base['database'])
            selection=session.get(StrategyStudioSelection,'owner',populate_existing=True)
            d['strategy_selections']=[dict(owner_id='owner',strategy_id=selection.strategy_id,
                updated_at=selection.updated_at.isoformat(),activated_at=selection.activated_at.isoformat())]
            studio=session.get(StrategyStudioLiveState,'owner',populate_existing=True)
            d['studio_live_state']=[{k:(v.isoformat() if isinstance(v,datetime) else v)
                for k in doc['studio_before'] for v in [getattr(studio,k)]}]
            documents=session.scalars(select(cast(RecoveryNewsStrategyCompatibility.document,Text))).all()
            d['news_strategy_compatibility']=[validate_document(_parse(raw.encode())) for raw in documents]
            return d
        def __call__(self,scope):
            from startup_recovery.unit_of_work import _active
            assert _active.get() is None, 'Broker observation attempted under ownership locks'
            self.calls+=1
            b=deepcopy(base['broker'])
            b['observed_at']=b['history_to']=time()
            with factory() as session: d=self.database(session,scope)
            return dict(database=d,broker=b)
    reader=Reader()
    observation=reader(token.scope)
    with factory() as session,_VerificationTree(root) as tree,tree.shared():
        _,projection=api().validate_current_authority(tree,session,token,'prior-build',doc,observation)
    reader.calls=0
    raw=_encoded(isolation(doc))
    doc['transition_authority_sha256']=digest(projection)
    doc['maintenance_evidence_sha256']=_digest(raw)
    doc['receipt_digest']=receipt_digest(doc)
    return dict(session_factory=factory,scope=token.scope,document=doc,
        expected_receipt_digest=doc['receipt_digest'],expected_authority_digest=digest(projection),
        isolation_document=raw,expected_isolation_sha256=_digest(raw),
        maintenance_image_digest='sha256:'+'1'*64,maintenance_build_identity='2'*64,
        state_root=root,safety_reader=reader)


def test_atomic_transition_preserves_owner_heads_files_and_live(transition_args):
    from models import RecoveryCheckpointHead, RecoveryLegacyAdoption, RecoveryNewsStrategyCompatibility
    from services.strategy_studio_models import SavedStrategy,StrategyStudioSelection
    from models import StrategyStudioLiveState
    from tests.test_cutover_files import snapshot
    args=transition_args
    before=snapshot(args['state_root'])
    factory=args['session_factory']
    with factory() as session:
        heads=[{c.name:getattr(r,c.name) for c in r.__table__.columns} for r in session.query(RecoveryCheckpointHead)]
        saved=[{c.name:getattr(r,c.name) for c in r.__table__.columns} for r in session.query(SavedStrategy)]
    result=api().apply_transition(**args)
    assert result==dict(status='COMMITTED',receipt_digest=args['expected_receipt_digest'])
    assert args['safety_reader'].calls==2
    assert snapshot(args['state_root'])==before
    with factory() as session:
        assert session.query(RecoveryNewsStrategyCompatibility).count()==1
        assert session.get(StrategyStudioSelection,'owner').strategy_id=='new'
        studio=session.get(StrategyStudioLiveState,'owner')
        assert studio.enabled is True and studio.enabled_strategy_id=='new'
        assert studio.enabled_at.isoformat()==args['document']['studio_before']['enabled_at']
        account=session.get(RecoveryAccount,args['document']['scope_key'])
        assert account.phase==Phase.RELINQUISHED and account.owner_attempt_id is None
        assert account.allocated_epoch==account.owner_epoch==8
        assert session.query(RecoveryAttempt).count()==2
        assert session.query(RecoveryLegacyAdoption).count()==1
        assert [{c.name:getattr(r,c.name) for c in r.__table__.columns} for r in session.query(RecoveryCheckpointHead)]==heads
        assert [{c.name:getattr(r,c.name) for c in r.__table__.columns} for r in session.query(SavedStrategy)]==saved


@pytest.mark.parametrize('pin',['expected_receipt_digest','expected_authority_digest','expected_isolation_sha256'])
def test_wrong_independent_pin_never_commits(transition_args,pin):
    from models import RecoveryNewsStrategyCompatibility
    from services.strategy_studio_models import StrategyStudioSelection
    transition_args[pin]='f'*64
    with pytest.raises(RecoveryError): api().apply_transition(**transition_args)
    with transition_args['session_factory']() as session:
        assert session.query(RecoveryNewsStrategyCompatibility).count()==0
        assert session.get(StrategyStudioSelection,'owner').strategy_id=='old'


@pytest.mark.parametrize('stage',['before_commit','after_commit'])
def test_ambiguous_commit_never_retries_and_leaves_atomic_result(transition_args,stage):
    from models import RecoveryNewsStrategyCompatibility,StrategyStudioLiveState
    from services.strategy_studio_models import StrategyStudioSelection
    factory=transition_args['session_factory']
    calls=[]
    def fail(session):
        calls.append(stage)
        raise RuntimeError('synthetic commit transport failure')
    event.listen(factory.class_,stage,fail)
    try:
        with pytest.raises(RecoveryError,match='COMMIT_INSPECT_REQUIRED'):
            api().apply_transition(**transition_args)
    finally:
        event.remove(factory.class_,stage,fail)
    assert calls==[stage]
    expected='old' if stage=='before_commit' else 'new'
    with factory() as session:
        assert session.get(StrategyStudioSelection,'owner').strategy_id==expected
        assert session.get(StrategyStudioLiveState,'owner').enabled_strategy_id==expected
        assert session.query(RecoveryNewsStrategyCompatibility).count()==(stage=='after_commit')


def test_duplicate_transition_does_not_insert_or_activate_twice(transition_args):
    from models import RecoveryNewsStrategyCompatibility
    assert api().apply_transition(**transition_args)['status']=='COMMITTED'
    with pytest.raises(RecoveryError): api().apply_transition(**transition_args)
    with transition_args['session_factory']() as session:
        assert session.query(RecoveryNewsStrategyCompatibility).count()==1


def test_commit_inspection_is_idempotent_and_has_no_mutation(transition_args):
    args=transition_args
    assert api().inspect_transition(**args)['status']=='NOT_COMMITTED'
    assert api().apply_transition(**args)['status']=='COMMITTED'
    statements=[]
    engine=args['session_factory'].kw['bind']
    def record(conn,cursor,statement,parameters,context,executemany): statements.append(statement)
    event.listen(engine,'before_cursor_execute',record)
    try:
        for _ in range(2):
            assert api().inspect_transition(**args)==dict(status='COMMITTED_COMPLETE',
                receipt_digest=args['expected_receipt_digest'])
    finally:
        event.remove(engine,'before_cursor_execute',record)
    assert statements and all(s.lstrip().upper().startswith('SELECT') for s in statements)


def test_partial_committed_identity_never_classifies_complete(transition_args):
    from services.strategy_studio_models import StrategyStudioSelection
    api().apply_transition(**transition_args)
    with transition_args['session_factory'].begin() as session:
        session.get(StrategyStudioSelection,'owner').strategy_id='old'
    with pytest.raises(RecoveryError): api().inspect_transition(**transition_args)


@pytest.mark.parametrize('receipt_present',[True,False])
def test_successor_coordinator_requires_durable_compatibility(transition_args,receipt_present):
    from types import SimpleNamespace
    from sqlalchemy import delete
    from models import RecoveryNewsStrategyCompatibility
    from startup_recovery import coordinator
    from startup_recovery.checkpoint_lineage import read_lineage,read_news_checkpoint
    from startup_recovery.cutover_evidence import _VerificationTree
    from tests.test_cutover_files import snapshot as file_snapshot
    args=transition_args
    api().apply_transition(**args)
    factory,scope=args['session_factory'],args['scope']
    if not receipt_present:
        with factory.begin() as session:
            session.execute(delete(RecoveryNewsStrategyCompatibility))
    before=file_snapshot(args['state_root'])
    def database(_):
        with factory() as session: return args['safety_reader'].database(session,scope)
    def checkpoints(snapshot):
        result={}
        with factory() as session:
            result['news_trading_state']=read_news_checkpoint(session,snapshot,args['state_root'],'a'*40)
            with _VerificationTree(args['state_root']) as tree,tree.shared():
                for head in snapshot.database['checkpoint_heads']:
                    if head['kind']!='news_trading_state':
                        result[head['kind']]=read_lineage(tree,session,snapshot.token,'a'*40,None,head)[0]
        return result
    published=[]
    dependencies=SimpleNamespace(session_factory=factory,verify_database=lambda:dict(schema='0032-fixture'),
        readers=SimpleNamespace(database=database,
            broker=lambda _:args['safety_reader'](scope)['broker'],
            authenticate=lambda _:dict(authenticated=True,account_id=scope.account_id,environment=scope.environment)),
        checkpoints=checkpoints,publish_reconciled=lambda *values:published.append(values),
        start_management=lambda _:None,start_entry_evaluation=lambda _:None)
    outcome=coordinator.recover(dependencies,scope,'new-boot','a'*40)
    assert outcome.token.epoch==9  # allocated by the real store, never forced
    assert outcome.ready is receipt_present, (outcome.reason,
        outcome.result.conflicts if outcome.result else None)
    assert len(published)==int(receipt_present)
    if receipt_present:
        assert outcome.phases[-1]=='NEW_ENTRIES_READY'
        assert outcome.result.ok and not outcome.result.conflicts
    else:
        assert outcome.reason=='NEWS_STRATEGY_COMPATIBILITY_INVALID'
    assert file_snapshot(args['state_root'])==before


@pytest.mark.parametrize('change',['authority','isolation'])
def test_commit_inspection_requires_original_reviewed_pins(transition_args,change):
    from startup_recovery.checkpoints import _encoded,_parse,_digest
    args=transition_args
    api().apply_transition(**args)
    if change=='authority': args['expected_authority_digest']='f'*64
    else:
        proof=_parse(args['isolation_document'])
        proof['maintenance_deployment_id']='another-reviewed-but-different-deployment'
        args['isolation_document']=_encoded(proof)
        args['expected_isolation_sha256']=_digest(args['isolation_document'])
    with pytest.raises(RecoveryError): api().inspect_transition(**args)


@pytest.mark.parametrize('change',['live','position','pending','expired','account','unresolved'])
def test_second_observation_safety_drift_blocks_without_mutation(transition_args,change):
    from models import RecoveryNewsStrategyCompatibility
    original=transition_args['safety_reader']
    class Reader:
        calls=0
        database=original.database
        def __call__(self,scope):
            self.calls+=1
            value=original(scope)
            if self.calls==2:
                if change=='live': value['database']['settings'][0]['setting_value']='true'
                if change=='position': value['broker']['positions']=[{'position_id':'unsafe'}]
                if change=='pending': value['broker']['orders']=[{'order_id':'unsafe'}]
                if change=='expired': value['broker']['observed_at']-=21
                if change=='account': value['broker']['account_id']='other'
                if change=='unresolved': value['database']['submissions']=[{'attempt_status':'SENT_UNKNOWN'}]
            return value
    transition_args['safety_reader']=Reader()
    with pytest.raises(RecoveryError): api().apply_transition(**transition_args)
    with transition_args['session_factory']() as session:
        assert session.query(RecoveryNewsStrategyCompatibility).count()==0


def test_locked_database_drift_blocks_without_commit(transition_args):
    from models import RecoveryNewsStrategyCompatibility
    original=transition_args['safety_reader']
    class Reader:
        def __call__(self,scope): return original(scope)
        def database(self,session,scope):
            value=original.database(session,scope)
            value['settings'][0]['setting_value']='true'
            return value
    transition_args['safety_reader']=Reader()
    with pytest.raises(RecoveryError): api().apply_transition(**transition_args)
    with transition_args['session_factory']() as session:
        assert session.query(RecoveryNewsStrategyCompatibility).count()==0


def test_hosted_saved_identity_and_studio_handoff_follow_atomic_transition(transition_args):
    from sqlalchemy import text
    from live_integrity.snapshots import saved,handoff
    from live_integrity.binding import fingerprint
    args=transition_args
    factory=args['session_factory']
    engine=factory.kw['bind']
    schema=engine.get_execution_options()['schema_translate_map'][None]
    quoted=engine.dialect.identifier_preparer.quote(schema)
    with factory.begin() as session:
        connection=session.connection()
        connection.execute(text('SET LOCAL search_path TO '+quoted))
        with pytest.raises(ValueError,match='STRATEGY_DEFINITION_NOT_CANONICAL'):
            saved(connection,'owner','old')
        with pytest.raises(ValueError,match='WAIT_STUDIO_LIVE_STRATEGY_MISMATCH'):
            handoff(connection,'owner','new')
    api().apply_transition(**args)
    with factory.begin() as session:
        connection=session.connection()
        connection.execute(text('SET LOCAL search_path TO '+quoted))
        result=saved(connection,'owner','new')
        assert result['identity']['config_hash']==fingerprint(args['document']['successor_row']['definition_json'])
        assert handoff(connection,'owner','new')['enabled_strategy_id']=='new'


@pytest.mark.parametrize('expired_check',[3,4])
def test_final_deadline_before_and_after_flush_remains_enforced(transition_args,monkeypatch,expired_check):
    import time
    from models import RecoveryNewsStrategyCompatibility
    from services.strategy_studio_models import StrategyStudioSelection
    from startup_recovery import owner_handoff
    original=owner_handoff._runtime_fresh
    clock=time.monotonic
    calls=[]
    def check(broker,started):
        calls.append(1)
        if len(calls)==expired_check:
            monkeypatch.setattr(time,'monotonic',lambda:clock()+21)
        return original(broker,started)
    monkeypatch.setattr(owner_handoff,'_runtime_fresh',check)
    with pytest.raises(RecoveryError): api().apply_transition(**transition_args)
    assert len(calls)==expired_check
    with transition_args['session_factory']() as session:
        assert session.query(RecoveryNewsStrategyCompatibility).count()==0
        assert session.get(StrategyStudioSelection,'owner').strategy_id=='old'
