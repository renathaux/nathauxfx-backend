"""Synthetic, exact-pair proofs; never production authority or broker IO."""
import copy
import importlib.util
import json
from decimal import Decimal
from datetime import datetime, timezone, timedelta

import pytest

from live_integrity.binding import fingerprint
from startup_recovery import checkpoints as files
from startup_recovery import adoption_evidence as adoption
from startup_recovery.reconcile import checkpoint_references, checkpoint_dependencies
from startup_recovery.store import scope_key
from startup_recovery.types import RecoveryError
from test_canonical_strategy_successor import pair
from test_legacy_adoption_semantics import inputs
from test_recovery_reconciliation import SCOPE


def api():
    assert importlib.util.find_spec('startup_recovery.news_strategy_compatibility'), 'Explicit compatibility proof missing'
    from startup_recovery import news_strategy_compatibility
    return news_strategy_compatibility


def fixture(monkeypatch):
    module = api()
    old, new = pair()
    # Synthetic strategies exercise the same proof, without committing private
    # saved definitions. Production has a fixed pair, never caller-selected pins.
    monkeypatch.setattr(module, 'PREDECESSOR_SHA', fingerprint(old))
    monkeypatch.setattr(module, 'SUCCESSOR_SHA', fingerprint(new))
    payloads, before, _ = inputs()
    before['strategy_selections'] = [dict(owner_id='owner', strategy_id='old')]
    original = dict(owner_id='owner', strategy_id='old', schema_version=1,
                    updated_at='2020-01-01T00:00:00+00:00', definition_json=old)
    successor = dict(owner_id='owner', strategy_id='new', schema_version=1,
                     updated_at='2026-10-16T00:00:00+00:00', definition_json=new)
    before['saved_strategies'] = [original, successor]
    after = copy.deepcopy(before)
    after['strategy_selections'][0]['strategy_id'] = 'new'
    before['studio_live_state']=[dict(owner_id='owner',enabled=True,enabled_strategy_id='old',
        enabled_at='2020-01-01T00:00:00+00:00',updated_at='2020-01-01T00:00:00+00:00')]
    after['studio_live_state']=[dict(before['studio_live_state'][0],enabled_strategy_id='new',
        updated_at='2026-10-16T00:00:00+00:00')]
    dependencies = {k: checkpoint_dependencies(k, before, p) for k, p in payloads.items()}
    binding = adoption.AdoptionBinding.create(dict(version=adoption.POLICY,
        scope_key=scope_key(SCOPE), account_scope='CTRADER:DEMO:7', owner_id='owner',
        attempt_id='adoption-attempt', epoch=1, boot_id='boot', build_id='build',
        cutover_manifest_hash='a'*64, handoff_evidence_hash='b'*64,
        source_hashes={k: files._digest(files._encoded(p)) for k,p in payloads.items()},
        payload_hashes={k: files._digest(files._encoded(p)) for k,p in payloads.items()},
        dependency_hashes=dependencies))
    envelopes = {}
    for kind, payload in payloads.items():
        identity = dict(account_scope='CTRADER:DEMO:7', owner_id='owner', symbol=None,
            strategy_id=None, config_hash=None, position_id=None, epoch=1, boot_id='boot',
            build_id='build', dependencies_hash=dependencies[kind], generation=1)
        envelopes[kind] = adoption.adopted_envelope(kind, payload, identity, binding,
                                                   timestamp='2020-01-02T00:00:00Z')
    hashes = {k: files._digest(e.raw) for k,e in envelopes.items()}
    monkeypatch.setattr(module, 'NEWS_SOURCE_SHA',
        files._digest(files._encoded(payloads['news_trading_state'])))
    manifest = files._digest(files._encoded(dict(manifest_schema=1,parent_hash=None,files=hashes)))
    receipt = adoption.make_receipt(binding, manifest, hashes)
    candidate = adoption.receipt_validated_candidates(receipt,envelopes,scope_key(SCOPE))['news_trading_state']
    body = files._parse(candidate.raw)
    head = dict(scope_key=scope_key(SCOPE),kind='news_trading_state',manifest_hash=manifest,
        file_hash=hashes['news_trading_state'],generation=1,identity=body['identity'],
        admission_hash=body['admission_hash'])
    document = dict(policy=module.POLICY, scope_key=scope_key(SCOPE),
        predecessor_attempt_id='terminated-attempt', predecessor_epoch=8,
        handoff_evidence_hash='c'*64, accepted_manifest_hash='d'*64,
        transition_authority_sha256='e'*64, maintenance_evidence_sha256='f'*64,
        source_head=head, adoption_receipt_digest=receipt['receipt_digest'],
        source_envelope=body, predecessor_row=original, successor_row=successor,
        studio_before=before['studio_live_state'][0],studio_after=after['studio_live_state'][0],
        before_references=checkpoint_references('news_trading_state',before,body['payload']),
        after_references=checkpoint_references('news_trading_state',after,body['payload']),
        observed_at='2026-10-16T00:00:00+00:00')
    document['receipt_digest'] = module.receipt_digest(document)
    # Durable evidence and a subsequent DB read cannot share mutable objects.
    return module, copy.deepcopy(document), before, after, candidate, receipt


def test_exact_proof_preserves_original_bytes_and_distinct_dependency_hashes(monkeypatch):
    m, doc, before, after, candidate, _ = fixture(monkeypatch)
    original = copy.deepcopy((doc,before,after,candidate.raw))
    m.validate_document(doc)
    m.validate_original(doc, after, candidate, scope_key(SCOPE),
                        observed_at=datetime(2026,10,16,tzinfo=timezone.utc).timestamp())
    assert checkpoint_dependencies('news_trading_state',before,doc['source_envelope']['payload']) != checkpoint_dependencies('news_trading_state',after,doc['source_envelope']['payload'])
    assert (doc,before,after,candidate.raw) == original


def test_exact_decimal_database_roundtrip_preserves_proof(monkeypatch):
    m, doc, _, after, candidate, _ = fixture(monkeypatch)
    stored = json.loads(json.dumps(doc),parse_float=Decimal)
    db = json.loads(json.dumps(after),parse_float=Decimal)
    assert m.receipt_digest(stored) == doc['receipt_digest']
    m.validate_original(stored,db,candidate,scope_key(SCOPE),
        observed_at=datetime(2026,10,16,tzinfo=timezone.utc).timestamp())


def test_mixed_numeric_representations_match_without_rounding_changed_semantics(monkeypatch):
    m,doc,_,after,candidate,_=fixture(monkeypatch)
    stored=json.loads(json.dumps(doc),parse_float=Decimal)
    m.validate_original(stored,after,candidate,scope_key(SCOPE),
        observed_at=datetime(2026,10,16,tzinfo=timezone.utc).timestamp())
    after['saved_strategies'][1]['definition_json']['tp1']['protection_r']=Decimal('0.2000000000000000000000000001')
    with pytest.raises(RecoveryError):
        m.validate_original(stored,after,candidate,scope_key(SCOPE),
            observed_at=datetime(2026,10,16,tzinfo=timezone.utc).timestamp())


def test_different_original_news_capture_cannot_use_exact_strategy_pair(monkeypatch):
    m, doc, *_ = fixture(monkeypatch)
    # Even a structurally valid, receipt-bound adoption from another capture
    # must not broaden this release-specific compatibility authority.
    monkeypatch.setattr(m, 'NEWS_SOURCE_SHA', 'f'*64)
    with pytest.raises(RecoveryError):
        m.validate_document(doc)


@pytest.mark.parametrize('field', ['scope_key','source_head','source_envelope','predecessor_row',
    'successor_row','before_references','after_references','observed_at','receipt_digest',
    'transition_authority_sha256','maintenance_evidence_sha256'])
def test_any_receipt_corruption_blocks(monkeypatch,field):
    m, doc, *_ = fixture(monkeypatch)
    doc[field] = None
    with pytest.raises(RecoveryError): m.validate_document(doc)


@pytest.mark.parametrize('change', ['other_strategy','semantic_change','old_version','stream',
    'selection_revision','missing_old','wrong_scope','clock_before_expiry','no_adoption_proof'])
def test_no_generic_dependency_or_identity_bypass(monkeypatch,change):
    m, doc, _, after, candidate, _ = fixture(monkeypatch)
    key=scope_key(SCOPE)
    observed=datetime(2026,10,16,tzinfo=timezone.utc).timestamp()
    if change=='other_strategy': after['strategy_selections'][0]['strategy_id']='unrelated'
    if change=='semantic_change': after['saved_strategies'][1]['definition_json']['risk']['risk_percent']=50
    if change=='old_version': after['saved_strategies'][0]['updated_at']='2026-10-17T00:00:00Z'
    if change=='stream': after['stream_generations']=[dict(public_symbol='EURUSD',status='ACTIVE',root_key='new',timeframe='M5',generation=1)]
    if change=='selection_revision': after['selection']['revision']='new'
    if change=='missing_old': after['saved_strategies'].pop(0)
    if change=='wrong_scope': key='f'*64
    if change=='clock_before_expiry': observed=datetime(2020,1,1,12,59,tzinfo=timezone.utc).timestamp()
    if change=='no_adoption_proof': candidate=files.CheckpointCandidate(candidate.kind,candidate.raw,False)
    with pytest.raises(RecoveryError):
        m.validate_original(doc,after,candidate,key,observed_at=observed)


@pytest.mark.parametrize('change', ['ready','executed','missing','new_release','new_identity','plan'])
def test_retained_event_cannot_acquire_executable_authority(monkeypatch,change):
    m, doc, _, after, *_ = fixture(monkeypatch)
    payload=copy.deepcopy(doc['source_envelope']['payload'])
    key='synthetic-event|EURUSD'
    record=payload['opportunities'][key]
    if change=='ready': record['status']='READY'
    if change=='executed': record.update(status='EXECUTED',consumed=True)
    if change=='missing': del payload['opportunities'][key]
    if change=='new_release': record['release_time']='2026-10-16T00:00:00Z'
    if change=='new_identity': record['event_id']='other'
    if change=='plan': record['details']['entry']=2000
    with pytest.raises(RecoveryError): m.validate_retained_events(doc,after,payload)


@pytest.mark.parametrize('status', ['PENDING','EXPIRED','CANCELLED'])
def test_observation_and_normal_terminal_lifecycle_remain_supported(monkeypatch,status):
    m, doc, _, after, *_ = fixture(monkeypatch)
    payload=copy.deepcopy(doc['source_envelope']['payload'])
    record=payload['opportunities']['synthetic-event|EURUSD']
    if status!='PENDING':
        record.update(status=status,consumed=True,updated_at='2026-10-16T00:00:01Z',
            reason='NEWS_OPPORTUNITY_EXPIRED' if status=='EXPIRED' else 'NEWS_MODE_CHANGED_TO_OFF')
    m.validate_retained_events(doc,after,payload)
    assert doc['source_envelope']['payload']['opportunities']['synthetic-event|EURUSD']['status']=='PENDING'


def test_real_publication_gate_rejects_revived_news(monkeypatch):
    from startup_recovery.publication import validate_produced_checkpoint
    _,doc,_,after,*_=fixture(monkeypatch)
    after['news_strategy_compatibility']=[doc]
    payload=copy.deepcopy(doc['source_envelope']['payload'])
    payload['opportunities']['synthetic-event|EURUSD']['status']='READY'
    with pytest.raises(RecoveryError):
        validate_produced_checkpoint('news_trading_state',payload,after)


def test_real_evaluator_cannot_return_plan_when_provider_reuses_retained_event(monkeypatch):
    from services import news_trading as news_runtime
    from startup_recovery.publication import validate_produced_checkpoint
    _,doc,_,after,*_=fixture(monkeypatch)
    after['news_strategy_compatibility']=[doc]
    state=copy.deepcopy(doc['source_envelope']['payload'])
    committed=[]
    monkeypatch.setattr(news_runtime,'get_mode',lambda:'TRADE_CONFIRMED')
    monkeypatch.setattr(news_runtime,'load_state',lambda path=None:copy.deepcopy(state))
    def save(payload,path=None):
        validate_produced_checkpoint('news_trading_state',payload,after)
        committed.append(copy.deepcopy(payload))
    monkeypatch.setattr(news_runtime,'save_state',save)
    release=datetime(2026,10,16,tzinfo=timezone.utc)
    event=dict(event_id='synthetic-event',event_name='CPI',currency='USD',impact='HIGH',
        release_time=release.isoformat(),source='jblanked_live',actual=0,forecast=1)
    candles=[dict(time=(release-timedelta(minutes=5*i)).isoformat(),
        open=1.1000,high=1.1005,low=1.0995,close=1.1000) for i in range(16,0,-1)]
    candles.append(dict(time=(release+timedelta(minutes=5)).isoformat(),
        open=1.1000,high=1.1015,low=1.1000,close=1.1015))
    now=release+timedelta(minutes=10)
    # Real confirmation, not an injected READY/plan outcome.
    confirmation=news_runtime.evaluate_confirmation(candles,event,'BUY','EURUSD',now)
    assert confirmation['ok'] is True, confirmation
    with pytest.raises(RecoveryError):
        news_runtime.evaluate([event],'EURUSD',candles_5m=candles,now=now)
    assert committed==[]
    assert state==doc['source_envelope']['payload']


def test_real_reconciler_requires_exact_durable_transition(monkeypatch):
    from startup_recovery import reconcile as recovery
    from test_recovery_reconciliation import empty
    from startup_recovery.types import ManagerToken
    def snapshot(api,db,broker):
        return api.DiscoverySnapshot.create(ManagerToken(SCOPE,'successor',9,'new-boot'),db,broker)
    _,doc,before,after,candidate,_=fixture(monkeypatch)
    _,broker=empty()
    broker['observed_at']=datetime(2026,10,16,tzinfo=timezone.utc).timestamp()
    broker['history_to']=broker['observed_at']
    before['checkpoint_heads']=[copy.deepcopy(doc['source_head'])]
    after['checkpoint_heads']=[copy.deepcopy(doc['source_head'])]
    candidates={'news_trading_state':candidate}
    assert recovery.reconcile(snapshot(recovery,before,broker),candidates).ok
    blocked=recovery.reconcile(snapshot(recovery,after,broker),candidates)
    assert 'RECOVERY_CHECKPOINT_INCOMPATIBLE:news_trading_state' in blocked.conflicts
    after['news_strategy_compatibility']=[doc]
    accepted=recovery.reconcile(snapshot(recovery,after,broker),candidates)
    assert accepted.ok,accepted.conflicts
    assert accepted.checkpoints['news_trading_state']==doc['source_envelope']['payload']
    assert after['checkpoint_heads']==before['checkpoint_heads']
    repeated=recovery.reconcile(snapshot(recovery,after,broker),candidates)
    assert repeated==accepted
    after['news_strategy_compatibility']=[doc,doc]
    assert not recovery.reconcile(snapshot(recovery,after,broker),candidates).ok
    after['news_strategy_compatibility']=[None]
    malformed=recovery.reconcile(snapshot(recovery,after,broker),candidates)
    assert 'RECOVERY_CHECKPOINT_INCOMPATIBLE:news_trading_state' in malformed.conflicts


def test_durable_row_projection_must_match_document_and_scope(monkeypatch):
    m,doc,*_=fixture(monkeypatch)
    assert hasattr(m,'documents_from_rows'), 'Durable snapshot proof projection missing'
    row=dict(scope_key=doc['scope_key'],predecessor_attempt_id=doc['predecessor_attempt_id'],
        predecessor_epoch=8,original_checkpoint_hash=doc['source_head']['file_hash'],
        predecessor_strategy_id='old',successor_strategy_id='new',receipt_digest=doc['receipt_digest'],
        document=doc,created_at='2026-10-16T00:00:00Z')
    assert m.documents_from_rows([row],scope_key(SCOPE))==[doc]
    assert m.documents_from_rows([],scope_key(SCOPE))==[]
    for field in set(row)-{'document','created_at'}:
        changed=copy.deepcopy(row)
        changed[field]='wrong'
        with pytest.raises(RecoveryError): m.documents_from_rows([changed],scope_key(SCOPE))
    with pytest.raises(RecoveryError): m.documents_from_rows([row,row],scope_key(SCOPE))


def test_retained_guard_cannot_cross_selected_account(monkeypatch):
    m,doc,_,after,*_=fixture(monkeypatch)
    after['selection'].update(account_id='other',scope='CTRADER:DEMO:other')
    m.validate_document(doc)
    with pytest.raises(RecoveryError):
        m.validate_retained_events(doc,after,doc['source_envelope']['payload'])


def test_descendant_requires_receipt_and_exact_adopted_root(monkeypatch):
    m,doc,_,after,candidate,receipt=fixture(monkeypatch)
    assert hasattr(m,'validate_lineage'), 'Authenticated ancestor proof missing'
    projection=dict(lineage=[dict(manifest_hash='e'*64,file_hash='f'*64,
        classification='PRODUCED_CURRENT'),dict(manifest_hash=doc['source_head']['manifest_hash'],
        file_hash=doc['source_head']['file_hash'],classification='ADOPTED',receipt=receipt)])
    payload=doc['source_envelope']['payload']
    with pytest.raises(RecoveryError):
        m.validate_lineage(after,projection,payload,scope_key(SCOPE))
    after['news_strategy_compatibility']=[doc]
    assert m.validate_lineage(after,projection,payload,scope_key(SCOPE))==doc['receipt_digest']
    for field in ('manifest_hash','file_hash','receipt'):
        broken=copy.deepcopy(projection)
        broken['lineage'][-1][field]=None
        with pytest.raises(RecoveryError):
            m.validate_lineage(after,broken,payload,scope_key(SCOPE))
    with pytest.raises(RecoveryError):
        m.validate_lineage(after,projection,payload,'f'*64)


def test_unrelated_fresh_news_lineage_does_not_require_migration_receipt(monkeypatch):
    m,_,_,after,*_=fixture(monkeypatch)
    projection=dict(lineage=[dict(manifest_hash='e'*64,file_hash='f'*64,
        classification='PRODUCED_CURRENT')])
    assert m.validate_lineage(after,projection,{},scope_key(SCOPE)) is None


@pytest.mark.parametrize('field,value', [('enabled',False),('enabled_strategy_id','old'),
    ('enabled_at',None),('owner_id','other')])
def test_compatibility_requires_unchanged_studio_gate_and_exact_successor_pointer(monkeypatch,field,value):
    m,doc,_,after,*_=fixture(monkeypatch)
    m.validate_document(doc)
    after['studio_live_state'][0][field]=value
    with pytest.raises(RecoveryError):
        m.validate_retained_events(doc,after,doc['source_envelope']['payload'])


def test_live_producer_cannot_lose_admitted_compatibility_receipt(monkeypatch):
    from startup_recovery.server_adapter import ProductionDependencies
    from types import SimpleNamespace
    _,doc,_,after,*_=fixture(monkeypatch)
    after['news_strategy_compatibility']=[doc]
    current=copy.deepcopy(after)
    server=object.__new__(ProductionDependencies)
    server._baseline_database=after
    server.scope=SCOPE
    server.readers=SimpleNamespace(database=lambda scope:copy.deepcopy(current))
    payload=doc['source_envelope']['payload']
    assert server.produced_dependencies('news_trading_state',payload)
    current['news_strategy_compatibility']=[]
    with pytest.raises(RecoveryError):
        server.produced_dependencies('news_trading_state',payload)
