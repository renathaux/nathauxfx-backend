"""Two-proof administrative drain, on synthetic disposable authority only."""
from copy import deepcopy
from dataclasses import asdict
from time import time

import pytest
from sqlalchemy import event

from models import RecoveryAttempt, RecoveryCheckpointHead, RecoveryLegacyAdoption
from startup_recovery import owner_handoff, store
from startup_recovery.checkpoints import _encoded, _digest
from startup_recovery.types import RecoveryError, ManagerToken
from test_owner_handoff_ready import ready_args, pin_document
from test_legacy_adoption_postgres import prepared, db, migrated_database


def test_runtime_adapter_is_separate():
    assert callable(getattr(owner_handoff, 'relinquish_terminated_runtime_owner', None))


@pytest.fixture
def runtime_args(ready_args):
    args = dict(ready_args)
    original = args['safety_reader']
    factory = args['session_factory']
    class Reader:
        def database(self, session, scope):
            value = original.database(session, scope)
            value['checkpoint_heads'] = [{c.name: getattr(h, c.name) for c in h.__table__.columns}
                for h in session.query(RecoveryCheckpointHead).filter_by(scope_key=store.scope_key(scope))]
            return value
        def __call__(self, scope):
            value = original(scope)
            with factory() as session:
                value['database'] = self.database(session, scope)
            value['broker']['observed_at'] = value['broker']['history_to'] = time()
            return value
    args['safety_reader'] = Reader()
    args['expected_historical_admission_hash'] = args.pop('expected_accepted_manifest_hash')
    args.pop('expected_checkpoint_authority_sha256')
    return args


def repin(args):
    """Separate read-only evidence collection before the mutation call."""
    from startup_recovery.cutover_evidence import _VerificationTree
    token = ManagerToken(args['scope'], args['expected_attempt_id'], args['expected_epoch'], args['predecessor_boot_id'])
    observation = args['safety_reader'](args['scope'])
    with _VerificationTree(args['state_root']) as tree, tree.shared(), args['session_factory']() as session:
        projection = owner_handoff._runtime_authority(tree, session, token,
            args['predecessor_build_id'], args['expected_historical_admission_hash'], observation)
    args['expected_current_runtime_authority_sha256'] = _digest(
        b'flowsignal-runtime-owner-authority-v1\0' + _encoded(projection))
    doc = dict(schema=1, kind='graceful-drain', scope=asdict(args['scope']),
        attempt_id=args['expected_attempt_id'], epoch=args['expected_epoch'],
        boot_id=args['predecessor_boot_id'], build_id=args['predecessor_build_id'],
        deployment_id=args['predecessor_deployment_id'], successor_release_identity=args['successor_release_identity'],
        predecessor_status='TERMINATED', predecessor_phase='NEW_ENTRIES_READY', operations_reconciled=True,
        termination_record_sha256='b'*64, historical_admission_hash=args['expected_historical_admission_hash'],
        current_runtime_authority_sha256=args['expected_current_runtime_authority_sha256'])
    args['evidence_document'] = _encoded(doc)
    args['expected_evidence_sha256'] = _digest(args['evidence_document'])
    return projection


def release(args):
    return owner_handoff.relinquish_terminated_runtime_owner(**args)


def test_zero_runtime_publications_and_historical_hash_not_current(runtime_args):
    projection = repin(runtime_args)
    assert projection['historical']['historical_admission_hash'] == 'd'*64
    assert projection['authority']['current_reconciliation_hash'] != 'd'*64
    assert {h['classification'] for h in projection['authority']['heads']} == {'ADOPTED'}
    assert release(runtime_args).status == 'RELINQUISHED'


@pytest.mark.parametrize('field,value', [('accepted_manifest_hash', None), ('accepted_manifest_hash', 'e'*64),
    ('phase_evidence_hash', 'e'*64), ('conflict_reason', 'conflict')])
def test_historical_durable_binding_required(runtime_args, field, value):
    repin(runtime_args)
    with runtime_args['session_factory'].begin() as session:
        row = (store.account_state(session, runtime_args['scope']) if field == 'accepted_manifest_hash'
               else session.get(RecoveryAttempt, runtime_args['expected_attempt_id']))
        setattr(row, field, value)
    with pytest.raises(RecoveryError): release(runtime_args)


@pytest.mark.parametrize('field,value', [('broker','other'), ('environment','live'), ('account_id','other')])
def test_exact_durable_account_scope(runtime_args, field, value):
    repin(runtime_args)
    with runtime_args['session_factory'].begin() as session:
        setattr(store.account_state(session, runtime_args['scope']), field, value)
    with pytest.raises(RecoveryError): release(runtime_args)


@pytest.mark.parametrize('phase', ['POSITION_MANAGEMENT_READY', 'RECOVERY_BLOCKED', 'RELINQUISHED', 'STATE_RECONCILED'])
@pytest.mark.parametrize('target', ['account', 'attempt'])
def test_runtime_path_requires_both_ready_phases(runtime_args, phase, target):
    repin(runtime_args)
    with runtime_args['session_factory'].begin() as session:
        row = (store.account_state(session, runtime_args['scope']) if target == 'account'
               else session.get(RecoveryAttempt, runtime_args['expected_attempt_id']))
        row.phase = phase
    with pytest.raises(RecoveryError): release(runtime_args)


def test_unreferenced_durable_bytes_are_not_authority(runtime_args):
    projection = repin(runtime_args)
    orphan = runtime_args['state_root']/'.recovery-generations'/('f'*64)
    orphan.mkdir(mode=0o700)
    (orphan/'manifest.json').write_bytes(b'not selected')
    assert repin(runtime_args) == projection
    assert release(runtime_args).status == 'RELINQUISHED'
    assert (orphan/'manifest.json').read_bytes() == b'not selected'


def test_committed_but_unacknowledged_is_not_repeated(runtime_args, monkeypatch):
    repin(runtime_args)
    factory = runtime_args['session_factory']
    calls = []
    original = store.relinquish
    def observed(*args):
        calls.append(1)
        return original(*args)
    def lost_ack(session):
        raise RuntimeError('synthetic lost acknowledgement')
    monkeypatch.setattr(store, 'relinquish', observed)
    event.listen(factory.class_, 'after_commit', lost_ack)
    try:
        with pytest.raises(RecoveryError, match='COMMIT_INSPECT_REQUIRED'): release(runtime_args)
    finally:
        event.remove(factory.class_, 'after_commit', lost_ack)
    assert calls == [1]
    with pytest.raises(RecoveryError): release(runtime_args)
    assert calls == [1]


def publish(args, kind, owner_id='owner'):
    from startup_recovery.checkpoints import produced_envelope
    from startup_recovery.checkpoint_store import publish as commit_head
    from startup_recovery.reconcile import checkpoint_dependencies
    from test_legacy_adoption_semantics import inputs
    token = ManagerToken(args['scope'], args['expected_attempt_id'], args['expected_epoch'], args['predecessor_boot_id'])
    payload = inputs()[0].get(kind, {})
    observation = args['safety_reader'](args['scope'])
    with args['session_factory']() as session:
        head = session.query(RecoveryCheckpointHead).filter_by(scope_key=store.scope_key(token.scope), kind=kind).one_or_none()
        parent = head.manifest_hash if head else None
        sequence = head.generation + 1 if head else 1
    identity = dict(account_scope='CTRADER:DEMO:7', owner_id=owner_id, symbol=None, strategy_id=None,
        config_hash=None, position_id=None, epoch=token.epoch, boot_id=token.boot_id,
        build_id=args['predecessor_build_id'], generation=sequence,
        dependencies_hash=checkpoint_dependencies(kind, observation['database'], payload))
    envelope = produced_envelope(kind, payload, identity, parent_hash=parent,
        admission_hash=args['expected_historical_admission_hash'], produced_event_id='synthetic',
        timestamp='2026-01-01T00:00:00Z')
    return commit_head(args['session_factory'], token, args['state_root'], envelope, expected_parent=parent)


def successor(args):
    from startup_recovery.types import HandoffEvidence
    token = ManagerToken(args['scope'], args['expected_attempt_id'], args['expected_epoch'], args['predecessor_boot_id'])
    with args['session_factory'].begin() as session:
        store.relinquish(session, token, HandoffEvidence('graceful-drain', 'e'*64, token.boot_id, True, True))
    with args['session_factory'].begin() as session:
        new = store.begin_attempt(session, token.scope, 'successor-boot', 'successor-build')
    with args['session_factory'].begin() as session:
        store.acquire_owner(session, new)
        for old, phase in [('BOOTSTRAP','DB_READY'), ('DB_READY','BROKER_AUTHENTICATED'),
            ('BROKER_AUTHENTICATED','STATE_DISCOVERED'), ('STATE_DISCOVERED','STATE_RECONCILED'),
            ('STATE_RECONCILED','POSITION_MANAGEMENT_READY'), ('POSITION_MANAGEMENT_READY','NEW_ENTRIES_READY')]:
            store.advance(session, new, old, phase, 'c'*64)
    args.update(expected_attempt_id=new.attempt_id, expected_epoch=new.epoch,
        predecessor_boot_id=new.boot_id, predecessor_build_id='successor-build',
        expected_historical_admission_hash='c'*64)


@pytest.mark.parametrize('scenario', ['mixed_six', 'new_kind', 'updated', 'inherited_adopted', 'inherited_produced'])
def test_current_and_inherited_runtime_evolution(runtime_args, scenario):
    if scenario == 'mixed_six':
        successor(runtime_args)
        for kind in ('live_backup', 'ctrader_accounts', 'live_monthly_history'): publish(runtime_args, kind)
    elif scenario == 'new_kind': publish(runtime_args, 'app_settings')
    elif scenario == 'updated':
        publish(runtime_args, 'live_backup')
        publish(runtime_args, 'live_backup')
    elif scenario == 'inherited_adopted': successor(runtime_args)
    else:
        publish(runtime_args, 'live_backup')
        successor(runtime_args)
    projection = repin(runtime_args)
    heads = projection['authority']['heads']
    if scenario == 'mixed_six':
        assert len(heads) == 6
        assert sum(h['classification'] == 'PRODUCED_CURRENT' for h in heads) == 3
        assert sum(h['classification'] == 'ADOPTED' for h in heads) == 3
    if scenario == 'inherited_produced':
        inherited = [h for h in heads if h['classification'] == 'PRODUCED_INHERITED']
        assert len(inherited) == 1
        assert inherited[0]['producer']['phase_evidence_hash'] == 'd'*64
        assert inherited[0]['claim'] == 'CURRENTLY_ACCEPTED_INHERITED_AUTHORITY'
    assert release(runtime_args).status == 'RELINQUISHED'


def test_publisher_valid_owner_evolution_preserves_historical_ancestor(runtime_args):
    publish(runtime_args, 'live_monthly_history')
    # Synthetic one-kind authority shape; the adapter has no fixed head count.
    with runtime_args['session_factory'].begin() as session:
        session.query(RecoveryCheckpointHead).filter(RecoveryCheckpointHead.kind != 'live_monthly_history').delete()
    successor(runtime_args)
    original = runtime_args['safety_reader']
    class NewOwner:
        def database(self, session, scope):
            value = original.database(session, scope)
            value['runtime_owners'] = ['successor-owner']
            return value
        def __call__(self, scope):
            value = original(scope)
            value['database']['runtime_owners'] = ['successor-owner']
            return value
    runtime_args['safety_reader'] = NewOwner()
    publish(runtime_args, 'live_monthly_history', owner_id='successor-owner')
    projection = repin(runtime_args)
    assert len(projection['authority']['heads']) == 1
    assert len(projection['authority']['heads'][0]['lineage']) == 2
    assert release(runtime_args).status == 'RELINQUISHED'


@pytest.mark.parametrize('field,value', [('boot_id','wrong'), ('build_id','wrong'),
    ('phase_evidence_hash','f'*64), ('phase','RECOVERY_BLOCKED'), ('epoch',90)])
def test_inherited_producer_durable_correspondence(runtime_args, field, value):
    publish(runtime_args, 'live_backup')
    producer = runtime_args['expected_attempt_id']
    successor(runtime_args)
    repin(runtime_args)
    with runtime_args['session_factory'].begin() as session:
        setattr(session.get(RecoveryAttempt, producer), field, value)
    with pytest.raises(RecoveryError): release(runtime_args)


@pytest.mark.parametrize('fault', ['missing_generation', 'manifest', 'envelope', 'file_hash', 'identity',
    'admission', 'receipt', 'unknown', 'missing_parent', 'corrupt_parent', 'symlink', 'mode'])
def test_selected_authority_corruption(runtime_args, fault):
    publish(runtime_args, 'live_backup')
    repin(runtime_args)
    with runtime_args['session_factory'].begin() as session:
        head = session.query(RecoveryCheckpointHead).filter_by(kind='live_backup').one()
        path = runtime_args['state_root']/'.recovery-generations'/head.manifest_hash
        receipt = session.query(RecoveryLegacyAdoption).one()
        ancestor = runtime_args['state_root']/'.recovery-generations'/receipt.generation_manifest_hash
        if fault == 'file_hash': head.file_hash = 'f'*64
        elif fault == 'identity': head.identity = {**head.identity, 'boot_id':'wrong'}
        elif fault == 'admission': head.admission_hash = 'f'*64
        elif fault == 'receipt': receipt.receipt_digest = 'f'*64
        elif fault == 'unknown': head.kind = 'unknown'
    if fault in {'missing_generation', 'manifest'}:
        if fault == 'missing_generation': (path/'manifest.json').unlink()
        else: (path/'manifest.json').write_bytes(b'{}')
    elif fault == 'envelope': (path/'live_backup.json').write_bytes(b'{}')
    elif fault == 'missing_parent': (ancestor/'manifest.json').unlink()
    elif fault == 'corrupt_parent': (ancestor/'live_backup.json').write_bytes(b'{}')
    elif fault == 'symlink':
        (path/'live_backup.json').unlink()
        (path/'live_backup.json').symlink_to(runtime_args['state_root']/'live_backup.json')
    elif fault == 'mode': path.chmod(0o750)
    with pytest.raises(RecoveryError): release(runtime_args)


@pytest.mark.parametrize('fault', ['live', 'position', 'pending', 'auth', 'complete', 'submission',
    'lifecycle', 'protection', 'expired', 'future', 'missing_time', 'bool_time', 'nan_time', 'duplicate', 'omitted'])
def test_unsafe_observations(runtime_args, fault):
    repin(runtime_args)
    original = runtime_args['safety_reader']
    class Reader:
        database = original.database
        def __call__(self, scope):
            value = original(scope)
            d, b = value['database'], value['broker']
            if fault == 'live': d['settings'][0]['setting_value'] = 'true'
            elif fault == 'position': b['positions'] = [{}]
            elif fault == 'pending': b['orders'] = [{}]
            elif fault == 'auth': b['authenticated'] = False
            elif fault == 'complete': b['complete'] = False
            elif fault == 'submission': d['submissions'] = [{'attempt_status':'SUBMITTING'}]
            elif fault == 'lifecycle': d['lifecycles'] = [{'status':'RECONCILIATION_REQUIRED'}]
            elif fault == 'protection': d['lifecycles'] = [{'management_state':{'protection_state':'PENDING'}}]
            elif fault == 'expired': b['observed_at'] -= 21
            elif fault == 'future': b['observed_at'] += 21
            elif fault == 'missing_time': del b['observed_at']
            elif fault == 'bool_time': b['observed_at'] = True
            elif fault == 'nan_time': b['observed_at'] = float('nan')
            elif fault == 'duplicate': d['checkpoint_heads'].append(deepcopy(d['checkpoint_heads'][0]))
            elif fault == 'omitted': d['checkpoint_heads'].pop()
            return value
    runtime_args['safety_reader'] = Reader()
    with pytest.raises(RecoveryError): release(runtime_args)


@pytest.mark.parametrize('fault', ['not_ok', 'missing_accepted', 'quarantined'])
def test_fresh_reconcile_must_accept_every_selected_kind(runtime_args, monkeypatch, fault):
    from dataclasses import replace
    from startup_recovery import reconcile as module
    repin(runtime_args)
    original = module.reconcile
    def rejected(*args):
        result = original(*args)
        if fault == 'not_ok': return replace(result, conflicts=('synthetic',))
        if fault == 'quarantined': return replace(result, quarantined=('news_trading_state',))
        return replace(result, _checkpoints=module.freeze({}))
    monkeypatch.setattr(module, 'reconcile', rejected)
    with pytest.raises(RecoveryError): release(runtime_args)


@pytest.mark.parametrize('field,value', [('predecessor_status','RUNNING'),
    ('successor_release_identity','f'*64), ('extra', True)])
def test_pinned_document_cannot_override_requirements(runtime_args, field, value):
    repin(runtime_args)
    pin_document(runtime_args, **{field:value})
    with pytest.raises(RecoveryError): release(runtime_args)


@pytest.mark.parametrize('field,value', [('account_scope','CTRADER:DEMO:8'), ('epoch',99),
    ('boot_id','other'), ('build_id','other'), ('admission_hash','f'*64)])
def test_even_hash_consistent_current_envelope_cannot_forge_provenance(runtime_args, field, value):
    from startup_recovery.checkpoints import produced_envelope, write_generation, _parse
    publish(runtime_args, 'live_backup')
    with runtime_args['session_factory'].begin() as session:
        h = session.query(RecoveryCheckpointHead).filter_by(kind='live_backup').one()
        path = runtime_args['state_root']/'.recovery-generations'/h.manifest_hash/'live_backup.json'
        body = _parse(path.read_bytes())
        if field == 'admission_hash': body[field] = value
        else: body['identity'][field] = value
        envelope = produced_envelope('live_backup', body['payload'], body['identity'],
            parent_hash=body['parent_hash'], admission_hash=body['admission_hash'],
            produced_event_id='synthetic-forged', timestamp=body['persisted_at'])
        manifest = write_generation(runtime_args['state_root'], {'live_backup':envelope}, body['parent_hash'])
        h.manifest_hash, h.file_hash = manifest.manifest_hash, manifest.file_hashes['live_backup']
        h.identity, h.admission_hash = body['identity'], body['admission_hash']
    with pytest.raises(RecoveryError): repin(runtime_args)


@pytest.mark.parametrize('field,value', [('account_scope', 'CTRADER:DEMO:8'),
    ('epoch', 99), ('boot_id', 'other'), ('generation', 99)])
def test_selected_generation_rejects_hash_consistent_incompatible_sibling(runtime_args, field, value):
    from startup_recovery.checkpoints import produced_envelope, _parse
    publish(runtime_args, 'live_backup')
    with runtime_args['session_factory'].begin() as session:
        head = session.query(RecoveryCheckpointHead).filter_by(kind='live_backup').one()
        root = runtime_args['state_root']/'.recovery-generations'
        selected = (root/head.manifest_hash/'live_backup.json').read_bytes()
        body = _parse(selected)
        identity = dict(body['identity'])
        identity[field] = value
        sibling = produced_envelope('live_monthly_history', {}, identity,
            parent_hash=body['parent_hash'], admission_hash=body['admission_hash'],
            produced_event_id='synthetic-sibling', timestamp=body['persisted_at']).raw
        # Deliberately bypass write_generation's coherence guard to model
        # corrupted evidence with independently valid envelopes and hashes.
        raw = _encoded(dict(manifest_schema=1, parent_hash=body['parent_hash'],
            files={'live_backup': _digest(selected), 'live_monthly_history': _digest(sibling)}))
        directory = root/_digest(raw)
        directory.mkdir(mode=0o700)
        for name, content in [('manifest.json', raw), ('live_backup.json', selected),
                              ('live_monthly_history.json', sibling)]:
            path = directory/name
            path.write_bytes(content)
            path.chmod(0o600)
        head.manifest_hash = _digest(raw)
    with pytest.raises(RecoveryError): repin(runtime_args)


def test_actual_failed_head_commit_orphan_ignored(runtime_args):
    before = repin(runtime_args)
    factory = runtime_args['session_factory']
    def fail(session): raise RuntimeError('synthetic commit failure')
    event.listen(factory.class_, 'before_commit', fail)
    try:
        with pytest.raises(RuntimeError, match='synthetic'): publish(runtime_args, 'app_settings')
    finally:
        event.remove(factory.class_, 'before_commit', fail)
    assert len(list((runtime_args['state_root']/'.recovery-generations').iterdir())) == 2
    assert repin(runtime_args) == before
    assert release(runtime_args).status == 'RELINQUISHED'


def test_missing_durable_producer_blocks(runtime_args):
    successor(runtime_args)
    publish(runtime_args, 'live_monthly_history')
    producer = runtime_args['expected_attempt_id']
    successor(runtime_args)
    with runtime_args['session_factory'].begin() as session:
        session.delete(session.get(RecoveryAttempt, producer))
    with pytest.raises(RecoveryError): repin(runtime_args)


def test_parent_cycle_rejected_without_selecting_orphan(runtime_args, monkeypatch):
    from startup_recovery.cutover_evidence import _VerificationTree
    publish(runtime_args, 'live_backup')
    with runtime_args['session_factory']() as session:
        head = session.query(RecoveryCheckpointHead).filter_by(kind='live_backup').one()
        selected = {c.name:getattr(head,c.name) for c in head.__table__.columns}
        token = ManagerToken(runtime_args['scope'], runtime_args['expected_attempt_id'],
                             runtime_args['expected_epoch'], runtime_args['predecessor_boot_id'])
        original = owner_handoff._runtime_generation
        calls = []
        def cycle(*args):
            manifest, bodies, candidates, receipt = original(*args)
            calls.append(1)
            # Hash-consistent cycles are computationally infeasible; inject at
            # the reader boundary to exercise the explicit cycle fence as well.
            manifest['parent_hash'] = selected['manifest_hash']
            return manifest, bodies, candidates, receipt
        monkeypatch.setattr(owner_handoff, '_runtime_generation', cycle)
        with _VerificationTree(runtime_args['state_root']) as tree, tree.shared():
            with pytest.raises(RecoveryError):
                owner_handoff._runtime_lineage(tree, session, token, 'build', 'd'*64, selected)
        assert calls == [1]


def test_digest_order_independent_and_no_flat_file_equality(runtime_args):
    before = repin(runtime_args)
    original = runtime_args['safety_reader']
    class Reordered:
        def database(self, session, scope):
            value = original.database(session, scope)
            value['checkpoint_heads'].reverse()
            return value
        def __call__(self, scope):
            value = original(scope)
            value['database']['checkpoint_heads'].reverse()
            return value
    runtime_args['safety_reader'] = Reordered()
    for path in runtime_args['state_root'].glob('*.json'): path.write_bytes(b'{"runtime":"changed"}')
    assert repin(runtime_args) == before
    assert release(runtime_args).status == 'RELINQUISHED'


def test_safety_expires_while_waiting_for_lock(runtime_args, monkeypatch):
    import time as clock
    repin(runtime_args)
    original = clock.monotonic
    calls = []
    def elapsed():
        calls.append(1)
        return original() + (21 if len(calls) >= 4 else 0)
    monkeypatch.setattr(clock, 'monotonic', elapsed)
    monkeypatch.setattr(store, 'relinquish', lambda *a: pytest.fail('stale evidence cannot mutate'))
    with pytest.raises(RecoveryError): release(runtime_args)


@pytest.mark.parametrize('fault', ['head_set', 'receipt', 'producer', 'digest'])
def test_locked_authority_drift_blocks(runtime_args, fault, monkeypatch):
    publish(runtime_args, 'live_backup')
    old = runtime_args['expected_attempt_id']
    successor(runtime_args)
    repin(runtime_args)
    if fault == 'digest':
        runtime_args['expected_current_runtime_authority_sha256'] = 'f'*64
        pin_document(runtime_args, current_runtime_authority_sha256='f'*64)
    else:
        with runtime_args['session_factory'].begin() as session:
            if fault == 'head_set': session.query(RecoveryCheckpointHead).filter_by(kind='live_backup').delete()
            elif fault == 'receipt': session.query(RecoveryLegacyAdoption).one().receipt_digest = 'f'*64
            else: session.get(RecoveryAttempt, old).phase_evidence_hash = 'f'*64
    monkeypatch.setattr(store, 'relinquish', lambda *a: pytest.fail('changed authority cannot mutate'))
    with pytest.raises(RecoveryError): release(runtime_args)


def test_network_outside_locks_and_filesystem_unchanged(runtime_args):
    from startup_recovery.unit_of_work import current_session, require_network_boundary
    from tests.test_cutover_files import snapshot
    repin(runtime_args)
    original = runtime_args['safety_reader']
    calls = []
    class Reader:
        def __call__(self, scope):
            require_network_boundary()
            assert current_session() is None
            calls.append('network')
            return original(scope)
        def database(self, session, scope):
            assert current_session() is session
            calls.append('locked')
            return original.database(session, scope)
    runtime_args['safety_reader'] = Reader()
    before = snapshot(runtime_args['state_root'])
    assert release(runtime_args).status == 'RELINQUISHED'
    assert calls == ['network', 'network', 'locked']
    assert snapshot(runtime_args['state_root']) == before
