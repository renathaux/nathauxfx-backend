"""Terminated ready-owner gate: synthetic authority on disposable PostgreSQL."""
from copy import deepcopy
from dataclasses import asdict
import json

import pytest
from sqlalchemy import event

from models import RecoveryAttempt, RecoveryCheckpointHead, RecoveryLegacyAdoption
from startup_recovery import store, owner_handoff
from startup_recovery.checkpoints import _encoded, _digest
from startup_recovery.types import RecoveryError
from test_legacy_adoption_postgres import prepared, db, migrated_database, adopt
from test_owner_handoff_submissions import submission_reader


def authority_projection(p):
    """Independent, explicit evidence contract; never serialize ORM repr."""
    with p['session_factory']() as s:
        r = s.query(RecoveryLegacyAdoption).one()
        receipt = {c.name: getattr(r, c.name) for c in r.__table__.columns if c.name != 'created_at'}
        heads = [{c.name: getattr(h, c.name) for c in h.__table__.columns}
                 for h in s.query(RecoveryCheckpointHead)]
    generation = receipt['generation_manifest_hash']
    envelopes = []
    for kind in sorted(receipt['envelope_hashes']):
        raw = (p['state_root']/'.recovery-generations'/generation/(kind+'.json')).read_bytes()
        body = json.loads(raw)
        envelopes.append(dict(kind=kind, sha256=_digest(raw), identity=body['identity'],
                              adoption=body['adoption']))
    return dict(schema=1, scope=asdict(p['token'].scope), accepted_manifest_hash='d'*64,
                receipt=receipt, heads=sorted(heads, key=lambda h: h['kind']),
                generation_manifest_hash=generation, envelopes=envelopes)


def pin_document(args, **changes):
    doc = json.loads(args['evidence_document'])
    doc.update(changes)
    args['evidence_document'] = _encoded(doc)
    args['expected_evidence_sha256'] = _digest(args['evidence_document'])


@pytest.fixture
def ready_args(prepared):
    p = prepared
    assert adopt(p).status == 'COMMITTED'
    token = p['token']
    with p['session_factory'].begin() as s:
        for old, new in [('STATE_DISCOVERED', 'STATE_RECONCILED'),
                         ('STATE_RECONCILED', 'POSITION_MANAGEMENT_READY'),
                         ('POSITION_MANAGEMENT_READY', 'NEW_ENTRIES_READY')]:
            store.advance(s, token, old, new, 'd'*64)
    class Safety:
        def __call__(self, scope):
            snap = p['snapshot_reader'](token)
            return dict(database=snap.database, broker=snap.broker)
        def database(self, session, scope):
            return p['snapshot_reader'].database(session, token)
    authority = _digest(b'flowsignal-ready-owner-authority-v1\0' + _encoded(authority_projection(p)))
    doc = dict(schema=1, kind='graceful-drain', scope=asdict(token.scope),
        attempt_id=token.attempt_id, epoch=token.epoch, boot_id=token.boot_id, build_id='build',
        deployment_id='synthetic-predecessor', successor_release_identity='a'*64,
        predecessor_status='TERMINATED', predecessor_phase='NEW_ENTRIES_READY',
        operations_reconciled=True, termination_record_sha256='b'*64,
        accepted_manifest_hash='d'*64, checkpoint_authority_sha256=authority)
    return dict(session_factory=p['session_factory'], scope=token.scope,
        expected_attempt_id=token.attempt_id, expected_epoch=token.epoch,
        predecessor_boot_id=token.boot_id, predecessor_build_id='build',
        predecessor_deployment_id='synthetic-predecessor', successor_release_identity='a'*64,
        evidence_document=_encoded(doc), expected_evidence_sha256=_digest(_encoded(doc)),
        expected_accepted_manifest_hash='d'*64, expected_checkpoint_authority_sha256=authority,
        state_root=p['state_root'], safety_reader=Safety())


def release(args):
    return owner_handoff.relinquish_terminated_ready_owner(**args)


def test_ready_owner_relinquishes_once_preserving_authority(ready_args, prepared, monkeypatch):
    before = authority_projection(prepared)
    calls = []
    original = store.relinquish
    def observed(session, token, proof):
        calls.append(proof.kind)
        return original(session, token, proof)
    monkeypatch.setattr(store, 'relinquish', observed)
    assert release(ready_args).status == 'RELINQUISHED'
    assert calls == ['graceful-drain']
    assert authority_projection(prepared) == before
    with ready_args['session_factory']() as s:
        a = store.account_state(s, ready_args['scope'])
        attempt = s.get(RecoveryAttempt, ready_args['expected_attempt_id'])
        assert a.phase == attempt.phase == 'RELINQUISHED'
        assert a.owner_attempt_id is None and a.accepted_manifest_hash is None
        assert a.allocated_epoch == a.owner_epoch == ready_args['expected_epoch']
        assert a.handoff_evidence['kind'] == 'graceful-drain'
    with pytest.raises(RecoveryError):
        release(ready_args)
    assert calls == ['graceful-drain']


@pytest.mark.parametrize('phase', ['BOOTSTRAP', 'DB_READY', 'BROKER_AUTHENTICATED',
    'STATE_DISCOVERED', 'STATE_RECONCILED', 'POSITION_MANAGEMENT_READY', 'RECOVERY_BLOCKED', 'RELINQUISHED'])
@pytest.mark.parametrize('target', ['account', 'attempt'])
def test_only_exact_ready_phase_for_both_rows(ready_args, phase, target):
    with ready_args['session_factory'].begin() as s:
        row = (store.account_state(s, ready_args['scope']) if target == 'account'
               else s.get(RecoveryAttempt, ready_args['expected_attempt_id']))
        row.phase = phase
    with pytest.raises(RecoveryError): release(ready_args)


@pytest.mark.parametrize('field,value', [
    ('expected_attempt_id', 'other'), ('expected_epoch', 99), ('predecessor_boot_id', 'other'),
    ('predecessor_build_id', 'other'), ('predecessor_deployment_id', 'other'),
    ('successor_release_identity', 'f'*64), ('expected_evidence_sha256', 'f'*64),
    ('expected_accepted_manifest_hash', None), ('expected_accepted_manifest_hash', 'f'*64),
    ('expected_checkpoint_authority_sha256', None), ('expected_checkpoint_authority_sha256', 'f'*64),
])
def test_independent_pins_required(ready_args, field, value):
    ready_args[field] = value
    with pytest.raises(RecoveryError): release(ready_args)


@pytest.mark.parametrize('field,value', [
    ('kind', 'operator-termination'), ('predecessor_status', 'RUNNING'),
    ('predecessor_phase', 'RECOVERY_BLOCKED'), ('operations_reconciled', False),
    ('termination_record_sha256', None), ('extra', True),
])
def test_invalid_pinned_document_still_blocks(ready_args, field, value):
    pin_document(ready_args, **{field:value})
    with pytest.raises(RecoveryError): release(ready_args)


@pytest.mark.parametrize('value', [None, 'f'*64])
def test_durable_accepted_manifest_required(ready_args, value):
    with ready_args['session_factory'].begin() as s:
        store.account_state(s, ready_args['scope']).accepted_manifest_hash = value
    with pytest.raises(RecoveryError): release(ready_args)


@pytest.mark.parametrize('fault', ['live_on', 'missing_live', 'malformed_live', 'position', 'pending',
    'incomplete', 'unauthenticated', 'account', 'environment', 'runtime_owner', 'submitting',
    'reconcile', 'tp1', 'protection', 'lifecycle_position'])
@pytest.mark.parametrize('locked', [False, True])
def test_fresh_and_locked_safety_checks(ready_args, fault, locked):
    original = ready_args['safety_reader']
    def corrupt(state):
        d, b = state['database'], state['broker']
        if fault == 'live_on': d['settings'][0]['setting_value'] = 'true'
        elif fault == 'missing_live': d['settings'] = []
        elif fault == 'malformed_live': d['settings'][0]['setting_value'] = False
        elif fault == 'position': b['positions'] = [{'id':'synthetic'}]
        elif fault == 'pending': b['orders'] = [{'id':'synthetic'}]
        elif fault == 'incomplete': b['complete'] = False
        elif fault == 'unauthenticated': b['authenticated'] = False
        elif fault == 'account': d['selection']['account_id'] = 'other'
        elif fault == 'environment': d['selection']['environment'] = 'live'
        elif fault == 'runtime_owner': d['runtime_owners'] = ['other']
        elif fault == 'submitting': d['submissions'] = [dict(attempt_status='SUBMITTING')]
        elif fault == 'reconcile': d['lifecycles'] = [dict(status='RECONCILIATION_REQUIRED')]
        elif fault == 'tp1': d['lifecycles'] = [dict(tp1_requested_at='now', tp1_completed_at=None)]
        elif fault == 'protection': d['lifecycles'] = [dict(management_state={'protection_state':'PENDING'})]
        elif fault == 'lifecycle_position': d['lifecycles'] = [dict(broker_position_id='synthetic')]
        return state
    # Broker-only corruption belongs exclusively to the unlocked observations.
    if locked and fault in {'position', 'pending', 'incomplete', 'unauthenticated'}:
        locked = False
    class Reader:
        def __call__(self, scope):
            value = deepcopy(original(scope))
            return value if locked else corrupt(value)
        def database(self, session, scope):
            value = dict(database=deepcopy(original.database(session, scope)), broker={})
            return corrupt(value)['database'] if locked else value['database']
    ready_args['safety_reader'] = Reader()
    with pytest.raises(RecoveryError): release(ready_args)


@pytest.mark.parametrize('fault', ['sixth_head', 'missing_head', 'head_hash', 'head_identity',
    'head_generation', 'missing_receipt', 'receipt_digest', 'second_receipt',
    'missing_envelope', 'changed_envelope', 'missing_manifest', 'changed_manifest',
    'extra_envelope', 'extra_generation', 'unknown_file'])
def test_authority_layout_must_be_exact(ready_args, fault):
    with ready_args['session_factory'].begin() as s:
        r = s.query(RecoveryLegacyAdoption).one()
        h = s.query(RecoveryCheckpointHead).first()
        generation = r.generation_manifest_hash
        if fault == 'sixth_head':
            s.add(RecoveryCheckpointHead(**{**{c.name:getattr(h,c.name) for c in h.__table__.columns}, 'kind':'sixth'}))
        elif fault == 'missing_head': s.delete(h)
        elif fault == 'head_hash': h.file_hash = 'f'*64
        elif fault == 'head_identity': h.identity = {**h.identity, 'owner_id':'other'}
        elif fault == 'head_generation': h.generation = 2
        elif fault == 'missing_receipt': s.delete(r)
        elif fault == 'receipt_digest': r.receipt_digest = 'f'*64
        elif fault == 'second_receipt':
            s.add(RecoveryLegacyAdoption(**{**{c.name:getattr(r,c.name) for c in r.__table__.columns},
                'cutover_manifest_hash':'f'*64, 'receipt_digest':'e'*64}))
    root = ready_args['state_root']
    gen = root/'.recovery-generations'/generation
    if fault == 'missing_envelope': (gen/'live_backup.json').unlink()
    elif fault == 'changed_envelope': (gen/'live_backup.json').write_bytes(b'{}')
    elif fault == 'missing_manifest': (gen/'manifest.json').unlink()
    elif fault == 'changed_manifest': (gen/'manifest.json').write_bytes(b'{}')
    elif fault == 'extra_envelope': (gen/'sixth.json').write_bytes(b'{}')
    elif fault == 'extra_generation': (root/'.recovery-generations'/('f'*64)).mkdir(mode=0o700)
    elif fault == 'unknown_file': (root/'unexplained.json').write_bytes(b'{}')
    with pytest.raises(RecoveryError): release(ready_args)


def test_flat_runtime_bytes_are_not_original_cutover_equality_gate(ready_args):
    for path in ready_args['state_root'].glob('*.json'):
        path.write_bytes(b'{"synthetic_runtime_update":true}')
    assert release(ready_args).status == 'RELINQUISHED'


def test_ambiguous_commit_never_retries(ready_args):
    factory = ready_args['session_factory']
    def lost_ack(session): raise RuntimeError('synthetic lost ack')
    event.listen(factory.class_, 'after_commit', lost_ack)
    try:
        with pytest.raises(RecoveryError, match='HANDOFF_COMMIT_INSPECT_REQUIRED'): release(ready_args)
    finally:
        event.remove(factory.class_, 'after_commit', lost_ack)
    with factory() as s:
        assert store.account_state(s, ready_args['scope']).owner_attempt_id is None


@pytest.mark.parametrize('state', ['REQUEST_STARTED', 'ACCEPTANCE_AMBIGUOUS', 'UNRESOLVED', 'ACCEPTED'])
def test_real_pg_unsettled_submission_blocks(ready_args, state):
    submission_reader(ready_args, dict(send_intent={'state':state}))
    with pytest.raises(RecoveryError): release(ready_args)


def test_network_observations_outside_lock_and_filesystem_unchanged(ready_args):
    from startup_recovery.unit_of_work import current_session, require_network_boundary
    from tests.test_cutover_files import snapshot
    original = ready_args['safety_reader']
    calls = []
    class Reader:
        def __call__(self, scope):
            require_network_boundary()
            assert current_session() is None
            calls.append('outside')
            return original(scope)
        def database(self, session, scope):
            assert current_session() is session
            with pytest.raises(RecoveryError, match='NETWORK_IN_TRANSACTION'):
                require_network_boundary()
            calls.append('locked')
            return original.database(session, scope)
    ready_args['safety_reader'] = Reader()
    before = snapshot(ready_args['state_root'])
    assert release(ready_args).status == 'RELINQUISHED'
    assert calls == ['outside', 'outside', 'locked']
    assert snapshot(ready_args['state_root']) == before


def test_canonical_digest_is_independent_of_db_head_order(ready_args, monkeypatch):
    from startup_recovery import legacy_adoption
    original = legacy_adoption._heads
    monkeypatch.setattr(legacy_adoption, '_heads', lambda *a, **kw: list(reversed(original(*a, **kw))))
    assert release(ready_args).status == 'RELINQUISHED'


def test_second_observation_drift_blocks_before_mutation(ready_args, monkeypatch):
    original = ready_args['safety_reader']
    class Reader:
        calls = 0
        database = original.database
        def __call__(self, scope):
            self.calls += 1
            state = original(scope)
            if self.calls == 2: state['database']['runtime_owners'] = ['other']
            return state
    ready_args['safety_reader'] = Reader()
    monkeypatch.setattr(store, 'relinquish', lambda *a: pytest.fail('must not mutate'))
    with pytest.raises(RecoveryError): release(ready_args)


def test_revoked_ready_token_cannot_be_reset(ready_args, prepared):
    from startup_recovery.runtime import invalidate_token, assert_token_usable
    invalidate_token(prepared['token'])
    with pytest.raises(RecoveryError): release(ready_args)
    with pytest.raises(RecoveryError): assert_token_usable(prepared['token'])


@pytest.mark.parametrize('fault', ['stale_owner_epoch', 'wrong_owner', 'wrong_build', 'revoked_attempt'])
def test_locked_owner_identity_cannot_be_forged(ready_args, fault):
    with ready_args['session_factory'].begin() as s:
        a = store.account_state(s, ready_args['scope'])
        attempt = s.get(RecoveryAttempt, ready_args['expected_attempt_id'])
        if fault == 'stale_owner_epoch': a.owner_epoch += 1
        elif fault == 'wrong_owner': a.owner_attempt_id = None
        elif fault == 'wrong_build': attempt.build_id = 'other'
        elif fault == 'revoked_attempt': attempt.boot_id = 'other'
    with pytest.raises(RecoveryError): release(ready_args)


@pytest.mark.parametrize('fault', ['receipt_provenance', 'head_admission', 'envelope_symlink',
                                  'generation_mode', 'cutover_mode', 'lock_mode'])
def test_authority_provenance_and_filesystem_guards(ready_args, fault):
    root = ready_args['state_root']
    with ready_args['session_factory'].begin() as s:
        r = s.query(RecoveryLegacyAdoption).one()
        gen = root/'.recovery-generations'/r.generation_manifest_hash
        cutover = root/'.cutover'/(r.cutover_manifest_hash+'.json')
        if fault == 'receipt_provenance': r.dependency_hashes = {**r.dependency_hashes, 'live_backup':'f'*64}
        elif fault == 'head_admission': s.query(RecoveryCheckpointHead).first().admission_hash = 'f'*64
    if fault == 'envelope_symlink':
        (gen/'live_backup.json').unlink()
        (gen/'live_backup.json').symlink_to(root/'live_backup.json')
    elif fault == 'generation_mode': gen.chmod(0o750)
    elif fault == 'cutover_mode': cutover.chmod(0o600)
    elif fault == 'lock_mode': (root/'.cutover'/'lock').chmod(0o644)
    with pytest.raises(RecoveryError): release(ready_args)
