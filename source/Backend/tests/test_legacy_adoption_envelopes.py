"""Adoption provenance is distinct from ordinary producer admission."""
import importlib
from dataclasses import replace
from hashlib import sha256

import pytest
from startup_recovery import checkpoints as files
from startup_recovery.types import RecoveryError
from test_recovery_store import db, store_api


def api():
    try:
        return importlib.import_module('startup_recovery.adoption_evidence')
    except ModuleNotFoundError:
        pytest.fail('Explicit adoption envelope boundary missing')


def bundle():
    a = api()
    kinds = sorted(a.KINDS)
    payloads = {k: {} for k in kinds}  # Serialization test, not semantic admission.
    binding = a.AdoptionBinding.create(dict(
        version='legacy-adoption-v1', cutover_manifest_hash='a'*64, scope_key='b'*64,
        account_scope='CTRADER:DEMO:7', owner_id='owner', attempt_id='attempt',
        epoch=3, boot_id='boot', build_id='build', handoff_evidence_hash='c'*64,
        source_hashes={k: 'd'*64 for k in kinds},
        payload_hashes={k: sha256(b'{}').hexdigest() for k in kinds},
        dependency_hashes={k: 'e'*64 for k in kinds}))
    envelopes = {}
    for k in kinds:
        identity = dict(account_scope='CTRADER:DEMO:7', owner_id='owner', symbol=None,
                        strategy_id=None, config_hash=None, position_id=None, epoch=3,
                        boot_id='boot', build_id='build', dependencies_hash='e'*64, generation=1)
        envelopes[k] = a.adopted_envelope(k, payloads[k], identity, binding, timestamp='2026-10-10T00:00:00Z')
    hashes = {k: sha256(e.raw).hexdigest() for k, e in envelopes.items()}
    manifest = files._encoded(dict(manifest_schema=1, parent_hash=None, files=hashes))
    receipt = a.make_receipt(binding, sha256(manifest).hexdigest(), hashes)
    return a, binding, envelopes, receipt


def test_binding_and_receipt_domains_are_canonical_and_acyclic():
    a, b, envs, receipt = bundle()
    reversed_binding = a.AdoptionBinding.create(dict(reversed(list(b.value.items()))))
    assert b.digest == reversed_binding.digest
    assert b.digest == sha256(b'flowsignal-legacy-adoption-binding-v1\0' + b.raw).hexdigest()
    content = {k: v for k, v in receipt.items() if k != 'receipt_digest'}
    assert receipt['receipt_digest'] == sha256(b'flowsignal-legacy-adoption-receipt-v1\0' + files._encoded(content)).hexdigest()
    assert a.validate_adoption_receipt(receipt, envs, 'b'*64) == b
    for e in envs.values():
        assert files._parse(e.raw)['payload'] == {}
        assert not isinstance(e, files.ProducedEnvelope)


def test_raw_adoption_candidate_denied_without_receipt_capability():
    a, b, envs, receipt = bundle()
    e = envs['live_backup']
    c = files.CheckpointCandidate(e.kind, e.raw, False)
    identity = files._parse(e.raw)['identity']
    accepted_hash = sha256(e.raw).hexdigest()
    assert isinstance(files.validate_checkpoint(c, identity, accepted_hash=accepted_hash), files.RejectedCheckpoint)
    # A boolean caller claim must not substitute for receipt verification.
    forged = replace(c, adoption_proof=True)
    assert isinstance(files.validate_checkpoint(forged, identity, accepted_hash=accepted_hash), files.RejectedCheckpoint)
    proof = a.validate_adoption_receipt(receipt, envs, 'b'*64)
    candidate = a.receipt_validated_candidates(receipt, envs, 'b'*64)[e.kind]
    assert isinstance(files.validate_checkpoint(candidate, identity, accepted_hash=accepted_hash), files.AcceptedCheckpoint)


@pytest.mark.parametrize('field,value', [('scope_key', 'f'*64), ('epoch', 4), ('boot_id', 'wrong'),
    ('build_id', 'wrong'), ('receipt_digest', 'f'*64), ('generation_manifest_hash', 'f'*64),
    ('source_hashes', {}), ('envelope_hashes', {}), ('dependency_hashes', {})])
def test_receipt_tampering_blocks(field, value):
    a, b, envs, receipt = bundle()
    receipt[field] = value
    with pytest.raises(RecoveryError):
        a.validate_adoption_receipt(receipt, envs, 'b'*64)


@pytest.mark.parametrize('mutation', ['strip', 'payload', 'epoch', 'extra'])
def test_envelope_tampering_blocks_even_with_recomputed_file_hash(mutation):
    a, b, envs, receipt = bundle()
    body = files._parse(envs['live_backup'].raw)
    if mutation == 'strip':
        body.pop('adoption')
    elif mutation == 'payload':
        body['payload'] = {'fabricated': True}
    elif mutation == 'epoch':
        body['identity']['epoch'] = 4
    else:
        body['authority'] = True
    envs['live_backup'] = a.AdoptedEnvelope('live_backup', files._encoded(body))
    with pytest.raises(RecoveryError):
        a.validate_adoption_receipt(receipt, envs, 'b'*64)


def test_runtime_type_cannot_launder_adopted_envelope():
    a, _, envs, _ = bundle()
    with pytest.raises(RecoveryError):
        files.checked_envelope(files.ProducedEnvelope('live_backup', envs['live_backup'].raw))


def test_existing_generation_format_and_no_orphan_discovery(tmp_path):
    a, _, envs, receipt = bundle()
    generated = files.write_generation(tmp_path, envs, None)
    assert generated.manifest_hash == receipt['generation_manifest_hash']
    assert isinstance(files.read_committed(tmp_path, None), files.Absent)
    candidates = files.read_committed(tmp_path, generated.manifest_hash)
    assert set(candidates) == set(envs)
    for c in candidates.values():
        identity = files._parse(c.raw)['identity']
        assert isinstance(files.validate_checkpoint(c, identity, accepted_hash=generated.file_hashes[c.kind]), files.RejectedCheckpoint)


def test_runtime_publisher_rejects_adopted_type_before_ownership_io():
    from startup_recovery.checkpoint_store import publish
    _, _, envs, _ = bundle()
    class NoSession:
        def begin(self):
            pytest.fail('Runtime publisher reached DB for adoption envelope')
    with pytest.raises(RecoveryError):
        publish(NoSession(), None, None, envs['live_backup'], expected_parent=None)


def test_database_reader_requires_actual_matching_receipt(db, tmp_path):
    from datetime import datetime, timezone
    from models import RecoveryAccount, RecoveryAttempt, RecoveryCheckpointHead, RecoveryLegacyAdoption
    from startup_recovery.types import AccountScope
    from startup_recovery.store import scope_key
    from startup_recovery.checkpoint_store import load
    a, binding, envs, _ = bundle()
    scope = AccountScope('ctrader', 'demo', '7')
    value = binding.value
    value['scope_key'] = scope_key(scope)
    binding = a.AdoptionBinding.create(value)
    for kind, e in list(envs.items()):
        body = files._parse(e.raw)
        envs[kind] = a.adopted_envelope(kind, body['payload'], body['identity'], binding, timestamp=body['persisted_at'])
    generation = files.write_generation(tmp_path, envs, None)
    receipt = a.make_receipt(binding, generation.manifest_hash, dict(generation.file_hashes))
    with db.begin() as s:
        s.add(RecoveryAccount(scope_key=scope_key(scope), broker='ctrader', environment='demo', account_id='7', allocated_epoch=3))
        s.flush()
        s.add(RecoveryAttempt(attempt_id='attempt', scope_key=scope_key(scope), epoch=3, boot_id='boot', build_id='build', phase='STATE_DISCOVERED', created_at=datetime.now(timezone.utc)))
        s.flush()
        for kind, e in envs.items():
            body = files._parse(e.raw)
            s.add(RecoveryCheckpointHead(scope_key=scope_key(scope), kind=kind, manifest_hash=generation.manifest_hash,
                file_hash=generation.file_hashes[kind], generation=1, identity=body['identity'], admission_hash=body['admission_hash']))
    with pytest.raises(RecoveryError):
        load(db, scope, tmp_path, 'live_backup')
    with db.begin() as s:
        s.add(RecoveryLegacyAdoption(**receipt, created_at=datetime.now(timezone.utc)))
    assert isinstance(load(db, scope, tmp_path, 'live_backup'), files.AcceptedCheckpoint)
    with db.begin() as s:
        s.get(RecoveryLegacyAdoption, (scope_key(scope), 'a'*64)).receipt_digest = 'f'*64
    with pytest.raises(RecoveryError):
        load(db, scope, tmp_path, 'live_backup')
