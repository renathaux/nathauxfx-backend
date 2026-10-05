"""Task 4 inventory only: real synthetic files and disposable PostgreSQL."""
from copy import deepcopy
import importlib
import os
from pathlib import Path
import pickle
import stat

import pytest

from tests.cutover_fixture import pg, native_inventory, generation, snapshot_db
from tests.test_cutover_files import snapshot
from tests.test_cutover_postgres import select_only
from startup_recovery.checkpoints import _parse, _digest, _encoded
from startup_recovery.cutover_files import ReadTree, CopyTarget
from startup_recovery.cutover_manifest import CutoverError, decode_manifest


def inventory(native, engine):
    service = importlib.import_module('startup_recovery.cutover_service')
    with ReadTree(native['source']) as source, CopyTarget(native['evidence']) as evidence:
        return service.inventory_state(source,native['state'],native['scope'],engine,evidence)


def test_inventory_deterministic_evidence_only_and_all_scopes(pg,native_inventory):
    n = native_inventory
    before = snapshot(n['source']),snapshot(n['external']),snapshot_db(pg[0],pg[1])
    with select_only(pg[0]) as sql:
        doc,report = inventory(n,pg[0])
    assert sum(s.startswith('select ') for s in sql) == 1
    body = _parse(doc.raw)
    assert not report.blockers and not report.copy_verified and not report.ready_for_cutover
    assert 'LEGACY_RECONCILIATION_REQUIRED' in report.operational_blockers
    assert body['db_reference_sha256'] == _digest(_encoded(body['db_references']))
    assert len(body['db_references']) == 3
    assert len({r['scope_key'] for r in body['db_references']}) == 2
    gen = [r for r in body['records'] if r['family']==12]
    assert len(gen)==5 and len({r['target_path'] for r in gen})==5
    assert {k for r in gen for k in r['db_reference_keys']} == {r['scope_key']+':'+r['kind'] for r in body['db_references']}
    evidence = n['evidence']/(doc.digest+'.json')
    with ReadTree(n['evidence']) as tree:
        assert tree.read(evidence.name).raw == doc.raw
        assert tree.entries('.') == (evidence.name,)
    assert stat.S_IMODE(evidence.stat().st_mode)==0o400
    assert decode_manifest(doc.raw,doc.digest)==doc
    all_before = snapshot(n['evidence'])
    second,_ = inventory(n,pg[0])
    assert second == doc and snapshot(n['evidence']) == all_before
    assert not n['state'].exists()
    assert (snapshot(n['source']),snapshot(n['external']),snapshot_db(pg[0],pg[1])) == before


def test_inventory_all_27_families_have_fixed_policy_records(pg,native_inventory):
    n=native_inventory
    (n['source']/'.recovery-generations'/'.tmp-old').mkdir()
    output=n['external']/'operator-report.json'
    n['scope']['operator_outputs']=[str(output)]
    doc,report=inventory(n,pg[0])
    records=_parse(doc.raw)['records']
    assert {r['family'] for r in records} == set(range(1,28))
    classifications={r['family']:r['classification'] for r in records}
    assert classifications=={**{n:'COPY_CANDIDATE' for n in (*range(1,11),12,14)},
        11:'RECONSTRUCTABLE',13:'EVIDENCE_ONLY',15:'UNKNOWN',16:'UNKNOWN',17:'EXCLUDED',
        18:'UNKNOWN',**{n:'CACHE' for n in range(19,27)},27:'UNKNOWN'}
    assert not report.blockers  # Explicitly inspected absent unknown paths are not present state.


@pytest.mark.parametrize('family',range(13,28))
def test_inventory_omitted_coverage_blocks_not_assumed_absent(pg,native_inventory,family):
    n=native_inventory
    n['scope']['family_locations'].pop(str(family))
    doc,report=inventory(n,pg[0])
    assert f'INCOMPLETE_COVERAGE:{family}' in report.blockers
    assert _parse(doc.raw)['blockers'] == list(report.blockers)
    assert not report.ready_for_cutover


def test_inventory_explicit_nondefault_binding_no_default_fallback(pg,native_inventory):
    n=native_inventory
    (n['source']/'news_trading_state.json').unlink()
    override=n['external']/'news_trading_state.json'
    override.write_bytes(b'{"synthetic":"exact override"}')
    n['scope']['binding_overrides']['news_trading_state']=str(override)
    before=snapshot(n['external'])
    doc,report=inventory(n,pg[0])
    row=next(r for r in _parse(doc.raw)['records'] if r['family']==9)
    assert row['source_path']==str(override) and row['target_path']=='news_trading_state.json'
    assert row['sha256']==_digest(b'{"synthetic":"exact override"}')
    assert not report.blockers and snapshot(n['external'])==before


def test_inventory_absent_binding_stays_absent_with_no_defaults(pg,native_inventory):
    n=native_inventory
    (n['source']/'final_signal_hold.json').unlink()
    doc,report=inventory(n,pg[0])
    row=next(r for r in _parse(doc.raw)['records'] if r['family']==3)
    assert row['presence']=='ABSENT'
    assert all(row[k] is None for k in ('size','sha256','source_mode','destination_mode'))
    assert not (n['source']/'final_signal_hold.json').exists() and not n['state'].exists()


@pytest.mark.parametrize('kind',['users','history','sqlite','output','unexpected','unexpected_directory','generation_extra'])
def test_inventory_unknown_authority_blocks_without_consuming_it(pg,native_inventory,monkeypatch,kind):
    n=native_inventory
    paths={'users':n['source']/'users.json','history':n['source']/'trade_history_store.json',
        'sqlite':Path(n['scope']['family_locations']['18'][0]),'output':n['external']/'report.json',
        'unexpected':n['source']/'unreviewed.json','unexpected_directory':n['source']/'unreviewed',
        'generation_extra':n['source']/'.recovery-generations'/pg[2][0][2][0]['manifest_hash']/'unreviewed.json'}
    target=paths[kind]
    if kind=='output': n['scope']['operator_outputs']=[str(target)]
    if kind=='unexpected_directory': target.mkdir()
    else: target.write_bytes(b'UNREVIEWED-PAYLOAD-MUST-NOT-BE-READ')
    original=ReadTree.read
    def no_unknown(self,relative):
        assert self.root/relative!=target
        return original(self,relative)
    monkeypatch.setattr(ReadTree,'read',no_unknown)
    before=snapshot(n['source']),snapshot(n['external']),snapshot_db(pg[0],pg[1])
    doc,report=inventory(n,pg[0])
    assert report.blockers and report.remaining_unknowns and not report.ready_for_cutover
    assert b'UNREVIEWED-PAYLOAD' not in doc.raw
    assert (snapshot(n['source']),snapshot(n['external']),snapshot_db(pg[0],pg[1]))==before


def test_inventory_credentials_pickle_caches_never_opened(pg,native_inventory,monkeypatch):
    n=native_inventory
    excluded=[n['source']/'.env',n['source']/'live_monthly_history.json']
    for number in range(19,27): excluded.append(Path(n['scope']['family_locations'][str(number)][0]))
    for p in excluded: p.write_bytes(b'DO-NOT-CONSUME-SYNTHETIC-SECRET')
    original=ReadTree.read
    def safe_read(self,relative):
        assert self.root/relative not in excluded
        return original(self,relative)
    monkeypatch.setattr(ReadTree,'read',safe_read)
    monkeypatch.setattr(pickle,'loads',lambda *a,**k: pytest.fail('pickle decoded'))
    monkeypatch.setattr(pickle,'load',lambda *a,**k: pytest.fail('pickle decoded'))
    doc,report=inventory(n,pg[0])
    assert not report.blockers and b'DO-NOT-CONSUME' not in doc.raw
    for row in _parse(doc.raw)['records']:
        if row['family'] in (11,17,*range(19,27)):
            assert row['presence']=='PRESENT' and row['reason']
            assert all(row[k] is None for k in ('size','sha256','source_mode','destination_mode'))


@pytest.mark.parametrize('defect',['missing_manifest','missing_payload','wrong_bytes','wrong_head','unmapped_binding'])
def test_inventory_missing_conflicting_generation_blocks_without_state_change(pg,native_inventory,defect):
    n=native_inventory
    directory=n['source']/'.recovery-generations'/pg[2][0][2][0]['manifest_hash']
    if defect=='missing_manifest': (directory/'manifest.json').unlink()
    elif defect=='missing_payload': (directory/'live_backup.json').unlink()
    elif defect=='wrong_bytes': (directory/'live_backup.json').write_bytes(b'{}')
    elif defect=='unmapped_binding': n['scope']['binding_overrides']['live_backup']=str(n['external']/'live_backup.json')
    else:
        from models import RecoveryCheckpointHead
        with pg[0].begin() as c:
            c.execute(RecoveryCheckpointHead.__table__.update().where(RecoveryCheckpointHead.kind=='live_backup').values(file_hash='0'*64))
    before=snapshot(n['source']),snapshot_db(pg[0],pg[1])
    doc,report=inventory(n,pg[0])
    assert report.blockers and not report.ready_for_cutover
    assert (snapshot(n['source']),snapshot_db(pg[0],pg[1]))==before
    assert not n['state'].exists()


def test_inventory_reconstructable_flat_does_not_exclude_referenced_generation(pg,native_inventory):
    from models import RecoveryCheckpointHead
    n=native_inventory
    raw,payloads,refs=generation('synthetic-a',('live_monthly_history',))
    with pg[0].begin() as c:
        c.execute(RecoveryCheckpointHead.__table__.insert().values(**{k:v for k,v in refs[0].items() if k not in ('broker','environment','account_id')}))
    directory=n['source']/'.recovery-generations'/refs[0]['manifest_hash']
    directory.mkdir()
    (directory/'manifest.json').write_bytes(raw)
    (directory/'live_monthly_history.json').write_bytes(payloads['live_monthly_history'])
    doc,report=inventory(n,pg[0])
    rows=_parse(doc.raw)['records']
    assert next(r for r in rows if r['family']==11)['target_path'] is None
    assert any(r['family']==12 and r['target_path'].endswith('/live_monthly_history.json') for r in rows)
    assert not report.blockers


def test_inventory_existing_destination_unchanged(pg,native_inventory):
    n=native_inventory
    n['state'].mkdir(mode=0o700)
    (n['state']/'untouched').write_bytes(b'not an inventory target')
    before=snapshot(n['state'])
    inventory(n,pg[0])
    assert snapshot(n['state'])==before


def test_inventory_evidence_no_clobber_and_durable_mode(pg,native_inventory,monkeypatch):
    n=native_inventory
    fsyncs=[]
    original=os.fsync
    def observe(fd):
        s=os.fstat(fd)
        fsyncs.append((stat.S_ISDIR(s.st_mode),stat.S_IMODE(s.st_mode)))
        original(fd)
    monkeypatch.setattr(os,'fsync',observe)
    doc,_=inventory(n,pg[0])
    assert (False,0o400) in fsyncs and any(isdir for isdir,_ in fsyncs)
    target=n['evidence']/(doc.digest+'.json')
    target.chmod(0o600)
    target.write_bytes(b'conflict')
    target.chmod(0o400)
    before=snapshot(n['evidence'])
    with pytest.raises(CutoverError,match='DESTINATION_CONFLICT'): inventory(n,pg[0])
    assert snapshot(n['evidence'])==before


@pytest.mark.parametrize('root',['source','state'])
def test_inventory_overlapping_evidence_rejected(pg,native_inventory,root):
    n=native_inventory
    n[root].mkdir(exist_ok=True,mode=0o700)
    n['evidence']=n[root]
    before=snapshot(n[root])
    with pytest.raises(CutoverError,match='ROOT_OVERLAP'): inventory(n,pg[0])
    assert snapshot(n[root])==before


@pytest.mark.parametrize('name',['users.json','app_settings.json','.env','flowsignal.db','flowsignal.db-wal',
                               'other.sqlite-journal','other.sqlite3-shm'])
def test_inventory_cache_declaration_cannot_hide_known_authority_or_credentials(pg,native_inventory,name):
    n=native_inventory
    target=n['external']/name
    target.write_bytes(b'synthetic-unknown-state')
    n['scope']['family_locations']['19']=[str(target)]
    before=snapshot(n['external'])
    with pytest.raises(CutoverError,match='SOURCE_CLASSIFICATION_CONFLICT'):
        inventory(n,pg[0])
    assert snapshot(n['external'])==before and not n['state'].exists()


@pytest.mark.parametrize('family',[11,17])
@pytest.mark.parametrize('name',['users.json','authority.sqlite3-wal','unexpected-state.json'])
def test_review_fixed_exclusions_cannot_reclassify_unknown_state(pg,native_inventory,family,name):
    n=native_inventory; target=n['external']/name
    target.write_bytes(b'unknown-authority-must-not-be-excluded')
    n['scope']['family_locations'][str(family)]=[str(target)]
    before=snapshot(n['source']),snapshot(n['external']),snapshot_db(pg[0],pg[1])
    with pytest.raises(CutoverError,match='SOURCE_CLASSIFICATION_CONFLICT'):
        inventory(n,pg[0])
    assert (snapshot(n['source']),snapshot(n['external']),snapshot_db(pg[0],pg[1]))==before
    assert not n['state'].exists()


def test_inventory_candidate_symlink_blocks_without_following(pg,native_inventory,monkeypatch):
    n=native_inventory
    candidate=n['source']/'live_backup.json'
    candidate.unlink()
    target=n['external']/'private'
    target.write_bytes(b'must-not-follow')
    candidate.symlink_to(target)
    before=snapshot(n['source']),snapshot(n['external'])
    doc,report=inventory(n,pg[0])
    assert any('UNSAFE_PATH' in reason for reason in report.blockers)
    assert b'must-not-follow' not in doc.raw
    assert (snapshot(n['source']),snapshot(n['external']))==before


def test_inventory_cache_directory_children_never_enumerated(pg,native_inventory,monkeypatch):
    n=native_inventory
    cache=Path(n['scope']['family_locations']['19'][0])
    cache.mkdir()
    (cache/'payload.json').write_bytes(b'cache-contents-not-evidence')
    original=ReadTree.entries
    def no_cache_entries(self,relative):
        assert self.root/relative != cache
        return original(self,relative)
    monkeypatch.setattr(ReadTree,'entries',no_cache_entries)
    doc,report=inventory(n,pg[0])
    assert not report.blockers and b'payload.json' not in doc.raw


def test_inventory_zero_source_state_write_attempts(pg,native_inventory,monkeypatch):
    n=native_inventory
    original=os.open
    writes=[]
    def guard(name,flags,*args,**kwargs):
        if flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC):
            parent=Path(os.readlink('/proc/self/fd/'+str(kwargs['dir_fd']))) if 'dir_fd' in kwargs else Path.cwd()
            target=parent/str(name)
            assert target.is_relative_to(n['evidence'])
            writes.append(target)
        return original(name,flags,*args,**kwargs)
    monkeypatch.setattr(os,'open',guard)
    inventory(n,pg[0])
    assert writes and not n['state'].exists()


def test_inventory_empty_database_does_not_invent_generation_files(pg,native_inventory):
    from models import RecoveryCheckpointHead
    n=native_inventory
    with pg[0].begin() as c: c.execute(RecoveryCheckpointHead.__table__.delete())
    doc,report=inventory(n,pg[0])
    body=_parse(doc.raw)
    assert body['db_references']==[]
    assert not any(r['family']==12 for r in body['records'])
    assert len([r for r in body['records'] if r['family']==13])==2
    assert not report.blockers and not report.ready_for_cutover


def test_inventory_referenced_generation_cannot_be_declared_unreferenced(pg,native_inventory):
    n=native_inventory
    n['scope']['family_locations']['13']=[str(n['source']/'.recovery-generations'/pg[2][0][2][0]['manifest_hash'])]
    before=snapshot(n['source']),snapshot_db(pg[0],pg[1])
    with pytest.raises(CutoverError,match='SOURCE_CLASSIFICATION_CONFLICT'): inventory(n,pg[0])
    assert (snapshot(n['source']),snapshot_db(pg[0],pg[1]))==before


def test_inventory_scope_order_does_not_change_digest_or_mutate_caller(pg,native_inventory):
    n=native_inventory
    n['scope']['operator_outputs']=[str(n['external']/'z'),str(n['external']/'a')]
    original=deepcopy(n['scope'])
    first,_=inventory(n,pg[0])
    assert n['scope']==original
    n['scope']['operator_outputs'].reverse()
    second,_=inventory(n,pg[0])
    assert first==second


def copy_document(document, engine, **kwargs):
    service = importlib.import_module('startup_recovery.cutover_service')
    return service.copy_state(document, engine, expected_digest=kwargs.pop('pin',document.digest), **kwargs)


def test_copy_exact_bytes_idempotent_rollback_and_no_authority(pg,native_inventory,monkeypatch):
    n=native_inventory
    # Deliberately non-JSON legacy bytes must not be decoded or upgraded.
    (n['source']/'live_backup.json').write_bytes(b'\xff legacy\n  exact bytes ')
    (n['source']/'final_signal_hold.json').unlink()
    for family in range(19,27):
        Path(n['scope']['family_locations'][str(family)][0]).write_bytes(b'never copy')
    (n['source']/'.env').write_bytes(b'synthetic-secret-do-not-read')
    doc,_=inventory(n,pg[0])
    original=ReadTree.read
    excluded={n['source']/'.env',n['source']/'live_monthly_history.json',
              *(Path(n['scope']['family_locations'][str(f)][0]) for f in range(19,27))}
    def reject_excluded_content(self,relative):
        assert self.root/relative not in excluded
        return original(self,relative)
    monkeypatch.setattr(ReadTree,'read',reject_excluded_content)
    before=snapshot(n['source']),snapshot(n['external']),snapshot_db(pg[0],pg[1])
    evidence_before=snapshot(n['evidence'])
    with select_only(pg[0]) as statements: report=copy_document(doc,pg[0])
    assert sum(s.startswith('select ') for s in statements)==2  # before + after publication
    assert report.copy_verified and not report.ready_for_cutover and not report.blockers
    assert 'LEGACY_CUTOVER_REQUIRED' in report.operational_blockers
    assert report.copied_count==16 and report.reused_count==0  # 10 flat + 5 generation + manifest
    with ReadTree(n['state']) as target, ReadTree(n['source']) as source:
        assert target.read('live_backup.json').raw==b'\xff legacy\n  exact bytes '
        assert target.read('.cutover/'+doc.digest+'.json').raw==doc.raw
        assert target.read('.cutover/'+doc.digest+'.json').stat.mode==0o400
        installed=_parse(target.read('.cutover/'+doc.digest+'.json').raw)
        assert len(installed['db_references'])==3
        assert len({r['scope_key'] for r in installed['db_references']})==2
        assert len([r for r in installed['records'] if r['family']==12])==5
        for record in _parse(doc.raw)['records']:
            if record['classification']=='COPY_CANDIDATE' and record['presence']=='PRESENT':
                assert target.read(record['target_path']).raw==source.read(str(Path(record['source_path']).relative_to(n['source']))).raw
                assert target.read(record['target_path']).stat.mode==0o600
        assert set(target.entries('.'))=={'.cutover','.recovery-generations','live_backup.json',
            'paper_backup.json','fifteen_m_swing_watch.json','app_settings.json','feature_flags.json',
            'market_data_source.json','ctrader_accounts.json','news_trading_state.json','visits.json','news_trading_audit.jsonl'}
        assert target.entries('.cutover')==tuple(sorted(('lock',doc.digest+'.json')))
    destination_before=snapshot(n['state'])
    with select_only(pg[0]): again=copy_document(doc,pg[0])
    assert again.copy_verified and again.copied_count==0 and again.reused_count==16
    assert snapshot(n['state'])==destination_before
    assert snapshot(n['evidence'])==evidence_before
    assert (snapshot(n['source']),snapshot(n['external']),snapshot_db(pg[0],pg[1]))==before
    # Pre-ownership rollback is simply non-use, not a destructive tool command.
    assert not report.ready_for_cutover
    assert all(r['trust_status']=='LEGACY_UNTRUSTED' for r in _parse(doc.raw)['records'] if r['family'] in range(1,11))


@pytest.mark.parametrize('defect',['pin_missing','pin_wrong','document_digest','schema','policy','coverage','omitted_record',
                                 'omitted_generation','blocker','root_overlap','source_traversal'])
def test_copy_untrusted_manifest_never_provisions_destination(pg,native_inventory,defect):
    from startup_recovery.cutover_manifest import ManifestDocument, canonical_manifest
    n=native_inventory
    doc,_=inventory(n,pg[0]); pin=doc.digest
    body=_parse(doc.raw)
    if defect=='pin_missing': pin=None
    elif defect=='pin_wrong': pin='0'*64
    elif defect=='document_digest': doc=ManifestDocument(doc.raw,'0'*64)
    else:
        if defect=='schema': body['schema']=2
        elif defect=='policy': body['policy_version']='other'
        elif defect=='coverage': body['scope']['family_locations'].pop('27')
        elif defect=='omitted_record': body['records']=[r for r in body['records'] if r['family']!=1]
        elif defect=='omitted_generation': body['records']=[r for r in body['records'] if r['family']!=12]
        elif defect=='blocker': body['blockers']=['UNKNOWN_AUTHORITY_STATE']
        elif defect=='root_overlap': body['state_root']=body['source_root']
        elif defect=='source_traversal': body['records'][0]['source_path']+='/../escape'
        raw=_encoded(body); doc=ManifestDocument(raw,_digest(raw)); pin=doc.digest
    before=snapshot(n['source']),snapshot_db(pg[0],pg[1])
    with pytest.raises(CutoverError): copy_document(doc,pg[0],pin=pin)
    assert not n['state'].exists()
    assert (snapshot(n['source']),snapshot_db(pg[0],pg[1]))==before


@pytest.mark.parametrize('defect',['bytes','mode','deleted','absent_appeared','unknown_appeared','generation_missing','reference'])
def test_copy_source_and_reference_drift_before_publication(pg,native_inventory,defect):
    n=native_inventory; doc,_=inventory(n,pg[0])
    if defect=='bytes': (n['source']/'visits.json').write_bytes(b'changed')
    elif defect=='mode': (n['source']/'visits.json').chmod(0o600)
    elif defect=='deleted': (n['source']/'visits.json').unlink()
    elif defect=='absent_appeared': (n['source']/'users.json').write_bytes(b'new authority')
    elif defect=='unknown_appeared': (n['source']/'unknown.json').write_bytes(b'unknown')
    elif defect=='generation_missing': (n['source']/'.recovery-generations'/pg[2][0][2][0]['manifest_hash']/'paper_backup.json').unlink()
    else:
        from models import RecoveryCheckpointHead
        with pg[0].begin() as c: c.execute(RecoveryCheckpointHead.__table__.delete())
    before=snapshot(n['source']),snapshot_db(pg[0],pg[1])
    with pytest.raises(CutoverError): copy_document(doc,pg[0])
    assert not n['state'].exists()
    assert (snapshot(n['source']),snapshot_db(pg[0],pg[1]))==before


@pytest.mark.parametrize('defect',['late_conflict','wrong_mode','symlink','directory','fifo','hardlink','unknown','absent','extra_generation','wrong_owner','unsafe_directory'])
def test_copy_whole_set_destination_preflight_no_canonical_publication(pg,native_inventory,defect):
    n=native_inventory
    (n['source']/'final_signal_hold.json').unlink()
    doc,_=inventory(n,pg[0]); n['state'].mkdir(mode=0o700)
    target=n['state']/'visits.json'
    if defect=='late_conflict': target.write_bytes(b'conflict')
    elif defect=='wrong_mode': target.write_bytes(b'{"synthetic_legacy":true}\n'); target.chmod(0o400)
    elif defect=='symlink': target.symlink_to(n['source']/'visits.json')
    elif defect=='directory': target.mkdir(mode=0o700)
    elif defect=='fifo': os.mkfifo(target,0o600)
    elif defect=='hardlink': os.link(n['source']/'visits.json',target)
    elif defect=='unknown': (n['state']/'unreviewed.json').write_bytes(b'unknown')
    elif defect=='absent': (n['state']/'final_signal_hold.json').write_bytes(b'not absent')
    elif defect=='extra_generation':
        d=n['state']/'.recovery-generations'; d.mkdir(mode=0o700); (d/('0'*64)).mkdir(mode=0o700)
    elif defect=='wrong_owner':
        import subprocess
        target.write_bytes(b'foreign'); subprocess.run(['sudo','chown','0',str(target)],check=True)
    else: (n['state']/'.recovery-generations').mkdir(mode=0o755)
    before=snapshot(n['source']),snapshot_db(pg[0],pg[1])
    with pytest.raises(CutoverError): copy_document(doc,pg[0])
    assert not (n['state']/'app_settings.json').exists()
    assert not (n['state']/'.cutover'/(doc.digest+'.json')).exists()
    assert (snapshot(n['source']),snapshot_db(pg[0],pg[1]))==before


@pytest.mark.parametrize('after',[0,1,8,17])
def test_copy_interruption_partial_not_verified_safe_resume_rollback(pg,native_inventory,monkeypatch,after):
    n=native_inventory; doc,_=inventory(n,pg[0])
    before=snapshot(n['source']),snapshot_db(pg[0],pg[1])
    original=CopyTarget.install; calls=[]
    def crash(self,relative,raw,mode):
        if len(calls)==after: raise RuntimeError('synthetic crash')
        result=original(self,relative,raw,mode); calls.append(relative)
        if len(calls)==17 and after==17: raise RuntimeError('crash after final installation')
        return result
    monkeypatch.setattr(CopyTarget,'install',crash)
    with pytest.raises(RuntimeError): copy_document(doc,pg[0])
    assert (snapshot(n['source']),snapshot_db(pg[0],pg[1]))==before
    with ReadTree(n['state']) as tree:
        assert set(tree.entries('.cutover')) <= {'lock',doc.digest+'.json'}
    monkeypatch.setattr(CopyTarget,'install',original)
    report=copy_document(doc,pg[0])
    assert report.copy_verified and not report.ready_for_cutover
    assert report.reused_count==after and report.copied_count==17-after
    assert (snapshot(n['source']),snapshot_db(pg[0],pg[1]))==before


@pytest.mark.parametrize('drift',['source','same_bytes_rewritten','same_bytes_replaced','source_absent','unknown','db','destination'])
@pytest.mark.parametrize('after',[1,17])
def test_copy_drift_during_publication_never_verified(pg,native_inventory,monkeypatch,drift,after):
    n=native_inventory; doc,_=inventory(n,pg[0]); original=CopyTarget.install; calls=[]
    def mutate(self,relative,raw,mode):
        result=original(self,relative,raw,mode)
        calls.append(relative)
        if len(calls)==after:
            if drift in ('source','same_bytes_rewritten','same_bytes_replaced'):
                p=n['source']/'live_backup.json'
                if drift=='same_bytes_replaced':
                    replacement=n['source']/'replacement'
                    replacement.write_bytes(b'{"synthetic_legacy":true}\n')
                    os.replace(replacement,p)
                else:
                    p.write_bytes(b'changed' if drift=='source' else b'{"synthetic_legacy":true}\n')
            elif drift=='source_absent': (n['source']/'users.json').write_bytes(b'appeared')
            elif drift=='unknown': (n['source']/'unknown.json').write_bytes(b'appeared')
            elif drift=='destination': (n['state']/'live_backup.json').write_bytes(b'changed')
            else:
                from models import RecoveryCheckpointHead
                with pg[0].begin() as c: c.execute(RecoveryCheckpointHead.__table__.delete())
        return result
    monkeypatch.setattr(CopyTarget,'install',mutate)
    with pytest.raises(CutoverError) as error: copy_document(doc,pg[0])
    if drift=='destination' and after==1:
        assert len(calls)<17  # no-clobber stops at the conflicting file, not at final validation
    else:
        assert len(calls)==(after if drift in ('source','same_bytes_rewritten','same_bytes_replaced') else 17)
    with ReadTree(n['state']) as tree:
        assert tree.entries('.cutover')==tuple(sorted(('lock',doc.digest+'.json')))
        if drift=='destination':
            assert error.value.code=='DESTINATION_CONFLICT'
            assert tree.read('live_backup.json').raw==b'changed'


@pytest.mark.parametrize('race',['identical','conflict','symlink'])
def test_copy_publication_race_no_overwrite(pg,native_inventory,monkeypatch,race):
    n=native_inventory; doc,_=inventory(n,pg[0]); original=os.link; raced=[]
    def competing(src,dst,*args,**kwargs):
        if not raced:
            raced.append(dst)
            fd=kwargs['dst_dir_fd']
            if race=='symlink': os.symlink(str(n['source']/'live_backup.json'),dst,dir_fd=fd)
            else:
                source_fd=os.open(src,os.O_RDONLY|os.O_NOATIME,dir_fd=kwargs['src_dir_fd'])
                try: raw=os.read(source_fd,1000000); mode=stat.S_IMODE(os.fstat(source_fd).st_mode)
                finally: os.close(source_fd)
                out=os.open(dst,os.O_CREAT|os.O_EXCL|os.O_WRONLY,mode,dir_fd=fd)
                try: os.write(out,raw if race=='identical' else b'competitor')
                finally: os.close(out)
        return original(src,dst,*args,**kwargs)
    monkeypatch.setattr(os,'link',competing)
    before=snapshot(n['source']),snapshot_db(pg[0],pg[1])
    if race=='identical':
        report=copy_document(doc,pg[0]); assert report.copy_verified and report.reused_count==1
    else:
        with pytest.raises(CutoverError): copy_document(doc,pg[0])
    assert raced and (snapshot(n['source']),snapshot_db(pg[0],pg[1]))==before


def test_copy_diagnostic_lock_busy_no_publication(pg,native_inventory):
    n=native_inventory; doc,_=inventory(n,pg[0])
    with CopyTarget(n['state']) as target, target.exclusive():
        with pytest.raises(CutoverError,match='BUSY'): copy_document(doc,pg[0])
        assert target.entries('.')==('.cutover',)


def test_copy_explicit_binding_preserves_original_and_no_default(pg,native_inventory):
    n=native_inventory
    (n['source']/'news_trading_state.json').unlink()
    relocated=n['external']/'news_trading_state.json'
    relocated.write_bytes(b'nondefault raw legacy\n')
    n['scope']['binding_overrides']['news_trading_state']=str(relocated)
    doc,_=inventory(n,pg[0])
    before=snapshot(n['source']),snapshot(n['external']),snapshot_db(pg[0],pg[1])
    with select_only(pg[0]): report=copy_document(doc,pg[0])
    assert report.copy_verified and not report.ready_for_cutover
    with ReadTree(n['state']) as tree: assert tree.read('news_trading_state.json').raw==b'nondefault raw legacy\n'
    assert not (n['source']/'news_trading_state.json').exists()
    assert (snapshot(n['source']),snapshot(n['external']),snapshot_db(pg[0],pg[1]))==before


from contextlib import contextmanager


@contextmanager
def verify_tripwires(monkeypatch, source_root):
    """Deny attempts, not just persistent effects. Fixture/snapshot IO is outside."""
    import builtins
    import io
    from startup_recovery import checkpoints
    with monkeypatch.context() as m:
        def forbidden(*a,**k): raise AssertionError('VERIFY_MUTATION_ATTEMPT')
        original_open=os.open; original_builtin=builtins.open; original_io=io.open
        original_tree=ReadTree.__init__
        def read_open(name, flags, *a, **k):
            assert not flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND), 'VERIFY_MUTATION_ATTEMPT'
            return original_open(name,flags,*a,**k)
        def read_builtin(name,mode='r',*a,**k):
            assert not any(v in mode for v in 'wax+'), 'VERIFY_MUTATION_ATTEMPT'
            return original_builtin(name,mode,*a,**k)
        def read_io(name,mode='r',*a,**k):
            assert not any(v in mode for v in 'wax+'), 'VERIFY_MUTATION_ATTEMPT'
            return original_io(name,mode,*a,**k)
        def tree(self,root):
            assert not Path(root).is_relative_to(source_root), 'VERIFY_SOURCE_ACCESS'
            return original_tree(self,root)
        m.setattr(os,'open',read_open); m.setattr(builtins,'open',read_builtin); m.setattr(io,'open',read_io)
        m.setattr(ReadTree,'__init__',tree)
        for name in ('mkdir','makedirs','unlink','remove','rmdir','rename','replace','link','symlink',
                     'chmod','fchmod','chown','fchown','lchown','utime','truncate','ftruncate','write','pwrite'):
            m.setattr(os,name,forbidden)
        for name in ('__init__','install','exclusive'): m.setattr(CopyTarget,name,forbidden)
        for name in ('write_generation','produced_envelope','read_candidate','read_committed','checked_envelope'):
            m.setattr(checkpoints,name,forbidden)
        yield


def verify_document(n,doc,engine,**kwargs):
    service=importlib.import_module('startup_recovery.cutover_service')
    return service.verify_state(n['evidence']/(doc.digest+'.json'),engine,
        expected_digest=kwargs.pop('pin',doc.digest),**kwargs)


def prepared_verify(n,engine):
    (n['source']/'final_signal_hold.json').unlink()
    doc,_=inventory(n,engine)
    copy_document(doc,engine)
    return doc


@pytest.mark.parametrize('offline',[False,True])
def test_verify_destination_only_never_accesses_source_or_mutates(pg,native_inventory,monkeypatch,offline):
    n=native_inventory; doc=prepared_verify(n,pg[0])
    source=n['source']
    if offline:
        source=source.with_name('offline-native'); n['source'].rename(source)
    before=tuple(snapshot(p) for p in (source,n['external'],n['state'],n['evidence'])),snapshot_db(pg[0],pg[1])
    with verify_tripwires(monkeypatch,n['source']),select_only(pg[0]) as statements:
        report=verify_document(n,doc,pg[0])
    assert report.operation=='verify' and report.copy_verified and not report.ready_for_cutover
    assert not report.blockers and not report.remaining_unknowns and report.copied_count==0
    assert 'LEGACY_RECONCILIATION_REQUIRED' in report.operational_blockers
    assert 'LEGACY_CUTOVER_REQUIRED' in report.operational_blockers
    assert sum(s.startswith('select ') for s in statements)==2
    assert (tuple(snapshot(p) for p in (source,n['external'],n['state'],n['evidence'])),snapshot_db(pg[0],pg[1]))==before


@pytest.mark.parametrize('defect',['missing_pin','wrong_pin','uppercase_pin','bad_manifest','missing_manifest','manifest_mode',
    'missing_candidate','corrupt_candidate','mode','absence','unknown','missing_generation','corrupt_generation',
    'db_drift','missing_lock','missing_state','symlink','generation_symlink','extra_generation','installed_manifest'])
def test_verify_every_failure_preserves_files_and_pg(pg,native_inventory,monkeypatch,defect):
    n=native_inventory; doc=prepared_verify(n,pg[0]); pin=doc.digest
    evidence=n['evidence']/(doc.digest+'.json')
    target=n['state']/'live_backup.json'
    gen=n['state']/'.recovery-generations'/pg[2][0][2][0]['manifest_hash']/'live_backup.json'
    if defect=='missing_pin': pin=None
    elif defect=='wrong_pin': pin='0'*64
    elif defect=='uppercase_pin': pin=doc.digest.upper()
    elif defect=='bad_manifest': evidence.chmod(0o600); evidence.write_bytes(b'bad'); evidence.chmod(0o400)
    elif defect=='missing_manifest': evidence.unlink()
    elif defect=='manifest_mode': evidence.chmod(0o600)
    elif defect=='missing_candidate': target.unlink()
    elif defect=='corrupt_candidate': target.write_bytes(b'wrong')
    elif defect=='mode': target.chmod(0o400)
    elif defect=='absence': (n['state']/'final_signal_hold.json').write_bytes(b'not absent')
    elif defect=='unknown': (n['state']/'users.json').write_bytes(b'unknown')
    elif defect=='missing_generation': gen.unlink()
    elif defect=='corrupt_generation': gen.write_bytes(b'{}')
    elif defect=='db_drift':
        from models import RecoveryCheckpointHead
        with pg[0].begin() as c: c.execute(RecoveryCheckpointHead.__table__.delete())
    elif defect=='missing_lock': (n['state']/'.cutover'/'lock').unlink()
    elif defect=='missing_state': n['state'].rename(n['state'].with_name('offline-state'))
    elif defect=='symlink': target.unlink(); target.symlink_to(n['source']/'live_backup.json')
    elif defect=='generation_symlink': gen.unlink(); gen.symlink_to(n['source']/'live_backup.json')
    elif defect=='extra_generation': (gen.parent/'unexpected.json').write_bytes(b'unknown')
    else:
        target=n['state']/'.cutover'/(doc.digest+'.json'); target.chmod(0o600); target.write_bytes(b'bad'); target.chmod(0o400)
    before=snapshot(n['source'].parent),snapshot_db(pg[0],pg[1])
    with verify_tripwires(monkeypatch,n['source']),select_only(pg[0]),pytest.raises(CutoverError):
        verify_document(n,doc,pg[0],pin=pin)
    assert (snapshot(n['source'].parent),snapshot_db(pg[0],pg[1]))==before


@pytest.mark.parametrize('defect',['coverage','omitted_candidate','omitted_generation','blocked','unknown_present','generation_keys','schema','policy'])
def test_verify_pinned_but_invalid_evidence_never_promotes(pg,native_inventory,monkeypatch,defect):
    from startup_recovery.cutover_manifest import ManifestDocument
    n=native_inventory; old=prepared_verify(n,pg[0]); body=_parse(old.raw)
    if defect=='coverage': body['scope']['family_locations'].pop('27')
    elif defect=='omitted_candidate': body['records']=[r for r in body['records'] if r['family']!=3]  # absent record removed
    elif defect=='omitted_generation': body['records']=[r for r in body['records'] if r['family']!=12]
    elif defect=='blocked': body['blockers']=['INCOMPLETE_COVERAGE']
    elif defect=='unknown_present': next(r for r in body['records'] if r['family']==15)['presence']='PRESENT'
    elif defect=='generation_keys':
        for r in body['records']:
            if r['family']==12: r['db_reference_keys']=r['db_reference_keys'][:1]
    elif defect=='schema': body['schema']=2
    else: body['policy_version']='other'
    raw=_encoded(body); doc=ManifestDocument(raw,_digest(raw))
    # Synthetic evidence mutation, outside verifier; no product repair.
    for parent in (n['evidence'],n['state']/'.cutover'):
        (parent/(old.digest+'.json')).unlink()
        p=parent/(doc.digest+'.json'); p.write_bytes(raw); p.chmod(0o400)
    before=snapshot(n['source'].parent),snapshot_db(pg[0],pg[1])
    with verify_tripwires(monkeypatch,n['source']),select_only(pg[0]),pytest.raises(CutoverError):
        verify_document(n,doc,pg[0])
    assert (snapshot(n['source'].parent),snapshot_db(pg[0],pg[1]))==before


def test_verify_propagates_pure_generation_rejection_without_repair(pg,native_inventory,monkeypatch):
    from startup_recovery import cutover_validation
    from startup_recovery.checkpoints import RejectedCheckpoint
    n=native_inventory; doc=prepared_verify(n,pg[0])
    before=snapshot(n['source'].parent),snapshot_db(pg[0],pg[1])
    monkeypatch.setattr(cutover_validation,'validate_checkpoint',lambda *a,**k: RejectedCheckpoint('synthetic rejection'))
    with verify_tripwires(monkeypatch,n['source']),select_only(pg[0]),pytest.raises(CutoverError):
        verify_document(n,doc,pg[0])
    assert (snapshot(n['source'].parent),snapshot_db(pg[0],pg[1]))==before


@pytest.mark.parametrize('operation',['open','mkdir','unlink','rename','link','chmod','chown','utime','truncate','provision','writer'])
def test_verify_write_tripwires_reject_injected_mutation(pg,native_inventory,monkeypatch,operation):
    service=importlib.import_module('startup_recovery.cutover_service')
    from startup_recovery import checkpoints
    n=native_inventory; doc=prepared_verify(n,pg[0]); p=n['state']/'live_backup.json'
    attempts={
        'open':lambda: os.open(p,os.O_WRONLY), 'mkdir':lambda: os.mkdir(n['state']/'new'),
        'unlink':lambda: os.unlink(p), 'rename':lambda: os.rename(p,n['state']/'new'),
        'link':lambda: os.link(p,n['state']/'new'), 'chmod':lambda: os.chmod(p,0o600),
        'chown':lambda: os.chown(p,os.geteuid(),os.getegid()), 'utime':lambda: os.utime(p,None),
        'truncate':lambda: os.truncate(p,0), 'provision':lambda: CopyTarget(n['state']),
        'writer':lambda: checkpoints.write_generation(None,None)}
    def injected(*a,**k): attempts[operation](); raise AssertionError('tripwire did not fire')
    before=snapshot(n['source'].parent),snapshot_db(pg[0],pg[1])
    monkeypatch.setattr(service,'read_heads',injected)
    with verify_tripwires(monkeypatch,n['source']),pytest.raises(AssertionError,match='VERIFY_MUTATION_ATTEMPT'):
        verify_document(n,doc,pg[0])
    assert (snapshot(n['source'].parent),snapshot_db(pg[0],pg[1]))==before


@pytest.mark.parametrize('corrupt',[False,True])
def test_verify_fresh_process_kernel_readonly_source_offline(pg,native_inventory,corrupt):
    import subprocess
    import sys
    n=native_inventory; doc=prepared_verify(n,pg[0])
    n['source'].rename(n['source'].with_name('offline-native'))
    if corrupt: (n['state']/'live_backup.json').write_bytes(b'corrupt')
    before=snapshot(n['source'].parent),snapshot_db(pg[0],pg[1])
    # Private mount namespace, read-only bind mounts: no chmod or timestamp repair.
    shell='''
set -eu
mount --bind "$1" "$1"
mount -o remount,bind,ro "$1"
mount --bind "$2" "$2"
mount -o remount,bind,ro "$2"
shift 2
exec setpriv --reuid=501 --regid=1000 --clear-groups env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC PYTHONDONTWRITEBYTECODE=1 PYTHON_DOTENV_DISABLED=1 "$@"
'''
    child=r'''
import errno, importlib.abc, os, sys
from pathlib import Path
state,evidence,pin,dsn,schema,source,corrupt=sys.argv[1:]
os.environ['CUTOVER_DATABASE_URL']=dsn
assert not Path(source).exists()
for p,flags in ((Path(state)/'live_backup.json',os.O_WRONLY),(Path(evidence)/'forbidden-create',os.O_WRONLY|os.O_CREAT|os.O_EXCL)):
 try: os.open(p,flags,0o600)
 except OSError as exc: assert exc.errno==errno.EROFS
 else: raise AssertionError('kernel write denial missing')
class Guard(importlib.abc.MetaPathFinder):
 def find_spec(self,fullname,path=None,target=None):
  if fullname in {'db','models','api','ctrader_connector','startup_recovery.store','startup_recovery.coordinator','startup_recovery.checkpoint_store','startup_recovery.publication','startup_recovery.producer'}:
   raise AssertionError('forbidden writable import '+fullname)
sys.meta_path.insert(0,Guard())
def audit(event,args):
 if event=='open':
  name,mode,flags=args
  assert not flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND), 'write open'
  if isinstance(name,str): assert not Path(name).is_relative_to(source), 'source accessed'
 if event in {'os.mkdir','os.remove','os.rename','os.link','os.symlink','os.chmod','os.chown','os.utime','os.truncate','socket.connect','socket.bind','socket.getaddrinfo'}:
  raise AssertionError('forbidden operation '+event)
sys.addaudithook(audit)
from sqlalchemy import create_engine
from startup_recovery.cutover_service import verify_state
from startup_recovery.cutover_manifest import CutoverError
engine=create_engine(dsn).execution_options(schema_translate_map={None:schema})
try:
 try: result=verify_state(Path(evidence)/(pin+'.json'),engine,expected_digest=pin)
 except CutoverError:
  assert corrupt=='True'
  print('READ_ONLY_BLOCKED')
 else:
  assert corrupt=='False' and result.copy_verified and not result.ready_for_cutover
  print('READ_ONLY_VERIFIED')
finally: engine.dispose()
'''
    result=subprocess.run(['sudo','unshare','--mount','--propagation','private','--','sh','-c',shell,'task6',
        str(n['state']),str(n['evidence']),sys.executable,'-B','-c',child,str(n['state']),str(n['evidence']),doc.digest,
        os.environ['CUTOVER_DATABASE_URL'],pg[1],str(n['source']),str(corrupt)],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert result.stdout.strip()==('READ_ONLY_BLOCKED' if corrupt else 'READ_ONLY_VERIFIED')
    assert (snapshot(n['source'].parent),snapshot_db(pg[0],pg[1]))==before


@pytest.mark.parametrize('sql',[
    'INSERT INTO {s}.auth_canary(last_seen_at) VALUES (now())',
    'UPDATE {s}.auth_canary SET last_seen_at=now()',
    'DELETE FROM {s}.auth_canary',
    'CREATE TABLE {s}.forbidden(id int)',
    "SELECT nextval('{s}.auth_canary_id_seq')",
    "SELECT setval('{s}.auth_canary_id_seq',999)",
    "UPDATE {s}.recovery_accounts SET phase='NEW_ENTRIES_READY'",
])
def test_verify_actual_postgres_denies_injected_writes(pg,native_inventory,monkeypatch,sql):
    from sqlalchemy import text
    from startup_recovery import cutover_db
    n=native_inventory; doc=prepared_verify(n,pg[0])
    original=cutover_db.read_only; codes=[]
    @contextmanager
    def attempted(engine):
        with original(engine) as c:
            assert c.execute(text('SHOW transaction_read_only')).scalar_one()=='on'
            try: c.execute(text(sql.format(s=pg[1])))
            except Exception as exc:
                codes.append(exc.orig.pgcode)
                raise
            yield c
    monkeypatch.setattr(cutover_db,'read_only',attempted)
    before=snapshot(n['source'].parent),snapshot_db(pg[0],pg[1])
    with verify_tripwires(monkeypatch,n['source']),pytest.raises(CutoverError,match='CUTOVER_DATABASE_READ_FAILED'):
        verify_document(n,doc,pg[0])
    assert codes==['25006']
    assert (snapshot(n['source'].parent),snapshot_db(pg[0],pg[1]))==before


def test_verify_reference_recheck_at_end_blocks_out_of_band_drift(pg,native_inventory,monkeypatch):
    from models import RecoveryCheckpointHead
    service=importlib.import_module('startup_recovery.cutover_service')
    n=native_inventory; doc=prepared_verify(n,pg[0]); original=service.read_heads
    reads=[]; after_external=[]
    def concurrent_change(engine):
        reads.append(True)
        if len(reads)==2:
            # Deliberate external actor, not verifier SQL. Final DB state must
            # equal this external mutation; verifier may not repair it.
            with engine.begin() as c: c.execute(RecoveryCheckpointHead.__table__.delete())
            after_external.append(snapshot_db(engine,pg[1]))
        return original(engine)
    monkeypatch.setattr(service,'read_heads',concurrent_change)
    before=snapshot(n['source'].parent)
    with verify_tripwires(monkeypatch,n['source']),pytest.raises(CutoverError,match='REFERENCE_CHANGED'):
        verify_document(n,doc,pg[0])
    assert len(reads)==2 and snapshot_db(pg[0],pg[1])==after_external[0]
    assert snapshot(n['source'].parent)==before
