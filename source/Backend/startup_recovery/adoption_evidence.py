"""Pure provenance encoding. A receipt binds evidence, never execution admission.

Readers must obtain the receipt and head from the durable database. This module
validates those values; it does not discover receipts or orphan generations.
"""
from dataclasses import dataclass
from datetime import datetime

from startup_recovery import checkpoints as files
from startup_recovery.legacy_semantics import KINDS
from startup_recovery.types import RecoveryError

POLICY = 'legacy-adoption-v1'
_BINDING_FIELDS = {'version', 'cutover_manifest_hash', 'scope_key', 'account_scope',
    'owner_id', 'attempt_id', 'epoch', 'boot_id', 'build_id', 'handoff_evidence_hash',
    'source_hashes', 'payload_hashes', 'dependency_hashes'}
_RECEIPT_FIELDS = {'scope_key', 'cutover_manifest_hash', 'attempt_id', 'epoch',
    'boot_id', 'build_id', 'handoff_evidence_hash', 'adoption_policy_version',
    'source_hashes', 'dependency_hashes', 'generation_manifest_hash', 'envelope_hashes',
    'receipt_digest'}
_SEAL = object()


def require(condition):
    if not condition:
        raise RecoveryError('LEGACY_ADOPTION_PROVENANCE_INVALID')


def hash_map(value):
    require(isinstance(value, dict) and set(value) == KINDS)
    require(all(files._valid_hash(v) for v in value.values()))


def domain_hash(domain, value):
    return files._digest(domain + b'\0' + files._encoded(value))


@dataclass(frozen=True)
class AdoptionBinding:
    raw: bytes

    @classmethod
    def create(cls, value):
        require(isinstance(value, dict) and set(value) == _BINDING_FIELDS)
        require(value['version'] == POLICY)
        require(type(value['epoch']) is int and value['epoch'] > 0)
        for k in ('cutover_manifest_hash', 'scope_key', 'handoff_evidence_hash'):
            require(files._valid_hash(value[k]))
        for k in ('owner_id', 'attempt_id', 'boot_id', 'build_id'):
            require(isinstance(value[k], str) and bool(value[k]))
        require(isinstance(value['account_scope'], str))
        parts = value['account_scope'].split(':')
        require(len(parts) == 3 and parts[0] == 'CTRADER' and parts[1] in {'DEMO', 'LIVE'} and bool(parts[2]))
        for k in ('source_hashes', 'payload_hashes', 'dependency_hashes'):
            hash_map(value[k])
        return cls(files._encoded(value))

    @property
    def value(self):
        return files._parse(self.raw)

    @property
    def digest(self):
        return domain_hash(b'flowsignal-legacy-adoption-binding-v1', self.value)


@dataclass(frozen=True)
class AdoptedEnvelope:
    kind: str
    raw: bytes


def adopted_envelope(kind, payload, identity, binding, *, timestamp):
    require(kind in KINDS and isinstance(payload, dict) and isinstance(binding, AdoptionBinding))
    binding = AdoptionBinding.create(binding.value)
    bound = binding.value
    require(isinstance(identity, dict) and set(identity) == files._IDENTITY)
    for k in ('account_scope', 'owner_id', 'epoch', 'boot_id', 'build_id'):
        require(identity[k] == bound[k] and type(identity[k]) is type(bound[k]))
    require(identity['generation'] == 1 and type(identity['generation']) is int)
    require(all(identity[k] is None for k in ('symbol', 'strategy_id', 'config_hash', 'position_id')))
    require(identity['dependencies_hash'] == bound['dependency_hashes'][kind])
    payload_hash = files._digest(files._encoded(payload))
    require(payload_hash == bound['payload_hashes'][kind])
    try:
        when = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        require(when.utcoffset() is not None and when.utcoffset().total_seconds() == 0)
    except (TypeError, ValueError, AttributeError):
        raise RecoveryError('LEGACY_ADOPTION_PROVENANCE_INVALID') from None
    body = dict(checkpoint_schema=1, kind=kind, identity=identity, parent_hash=None,
        admission_hash=binding.digest, persisted_at=timestamp, payload=payload,
        payload_hash=payload_hash, adoption=dict(binding=bound, binding_hash=binding.digest))
    return AdoptedEnvelope(kind, files._encoded(body))


def checked_adopted_envelope(envelope):
    require(isinstance(envelope, AdoptedEnvelope))
    try:
        body = files._parse(envelope.raw)
        binding = AdoptionBinding.create(body['adoption']['binding'])
        checked = adopted_envelope(envelope.kind, body['payload'], body['identity'], binding,
                                   timestamp=body['persisted_at'])
        require(checked.raw == envelope.raw)
        return body
    except (ValueError, TypeError, KeyError, AttributeError):
        raise RecoveryError('LEGACY_ADOPTION_PROVENANCE_INVALID') from None


def make_receipt(binding, generation_manifest_hash, envelope_hashes):
    value = AdoptionBinding.create(binding.value).value
    require(files._valid_hash(generation_manifest_hash))
    hash_map(envelope_hashes)
    receipt = {k: value[k] for k in ('scope_key', 'cutover_manifest_hash', 'attempt_id',
        'epoch', 'boot_id', 'build_id', 'handoff_evidence_hash', 'source_hashes', 'dependency_hashes')}
    receipt.update(adoption_policy_version=POLICY, generation_manifest_hash=generation_manifest_hash,
                   envelope_hashes=dict(envelope_hashes))
    receipt['receipt_digest'] = domain_hash(b'flowsignal-legacy-adoption-receipt-v1', receipt)
    return receipt


def validate_adoption_receipt(receipt, envelopes, expected_scope):
    try:
        require(isinstance(receipt, dict) and set(receipt) == _RECEIPT_FIELDS)
        require(receipt['scope_key'] == expected_scope and files._valid_hash(expected_scope))
        require(isinstance(envelopes, dict) and set(envelopes) == KINDS)
        bindings, hashes = [], {}
        for kind in sorted(KINDS):
            envelope = envelopes[kind]
            require(envelope.kind == kind)
            body = checked_adopted_envelope(AdoptedEnvelope(kind, envelope.raw))
            bindings.append(AdoptionBinding.create(body['adoption']['binding']))
            hashes[kind] = files._digest(envelope.raw)
        require(all(b == bindings[0] for b in bindings))
        manifest_hash = files._digest(files._encoded(dict(manifest_schema=1, parent_hash=None, files=hashes)))
        require(receipt == make_receipt(bindings[0], manifest_hash, hashes))
        return bindings[0]
    except (ValueError, TypeError, KeyError, AttributeError):
        raise RecoveryError('LEGACY_ADOPTION_PROVENANCE_INVALID') from None


@dataclass(frozen=True)
class _ReceiptProof:
    seal: object
    kind: str
    file_hash: str
    binding_hash: str


def receipt_validated_candidates(receipt, envelopes, expected_scope):
    binding = validate_adoption_receipt(receipt, envelopes, expected_scope)
    return {k: files.CheckpointCandidate(k, e.raw, False, adoption_proof=
        _ReceiptProof(_SEAL, k, files._digest(e.raw), binding.digest)) for k, e in envelopes.items()}


def candidate_has_receipt(candidate):
    proof = candidate.adoption_proof
    if not isinstance(proof, _ReceiptProof) or proof.seal is not _SEAL:
        return False
    try:
        body = checked_adopted_envelope(AdoptedEnvelope(candidate.kind, candidate.raw))
        return (proof.kind == candidate.kind and proof.file_hash == files._digest(candidate.raw)
                and proof.binding_hash == body['adoption']['binding_hash'])
    except RecoveryError:
        return False
