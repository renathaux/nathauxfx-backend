"""Explicit legacy-adoption retry classification; not a recovery admission."""
from startup_recovery import checkpoints as files
from startup_recovery.adoption_evidence import (
    KINDS, AdoptionBinding, checked_adopted_envelope, make_receipt,
    validate_adoption_receipt,
)
from startup_recovery.types import RecoveryError
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from sqlalchemy import select


@dataclass(frozen=True)
class AdoptionResult:
    status: str
    receipt_digest: str
    generation_manifest_hash: str


def classify_retry(receipt, heads, generations, binding):
    """Classify supplied evidence only; the caller must verify IO and fresh gates.

    This does not search the filesystem or make an orphan authoritative. It is
    deliberately stricter than normal runtime successor loading.
    """
    try:
        if not isinstance(generations, dict) or len(generations) != 1:
            return 'PARTIAL_OR_CONFLICTING'
        manifest_hash, envelopes = next(iter(generations.items()))
        if set(envelopes) != KINDS:
            return 'PARTIAL_OR_CONFLICTING'
        hashes = {k: files._digest(e.raw) for k, e in envelopes.items()}
        expected = make_receipt(binding, manifest_hash, hashes)
        if validate_adoption_receipt(expected, envelopes, binding.value['scope_key']) != binding:
            return 'PARTIAL_OR_CONFLICTING'
        if receipt is None and heads == []:
            return 'UNCOMMITTED_ORPHAN'
        if receipt != expected or len(heads) != 5 or {h['kind'] for h in heads} != KINDS:
            return 'PARTIAL_OR_CONFLICTING'
        for head in heads:
            body = checked_adopted_envelope(envelopes[head['kind']])
            required = dict(scope_key=binding.value['scope_key'], kind=head['kind'],
                manifest_hash=manifest_hash, file_hash=hashes[head['kind']], generation=1,
                identity=body['identity'], admission_hash=binding.digest)
            if head != required:
                return 'PARTIAL_OR_CONFLICTING'
        return 'COMMITTED_COMPLETE'
    except (RecoveryError, ValueError, KeyError, TypeError, AttributeError):
        return 'PARTIAL_OR_CONFLICTING'


def _require(condition, code='LEGACY_ADOPTION_BLOCKED'):
    if not condition:
        raise RecoveryError(code)


def _receipt(row):
    return None if row is None else {c.name: getattr(row, c.name)
        for c in row.__table__.columns if c.name != 'created_at'}


def _heads(session, key, generations=()):
    from models import RecoveryCheckpointHead
    from sqlalchemy import or_
    return [{c.name: getattr(row, c.name) for c in row.__table__.columns}
            for row in session.execute(select(RecoveryCheckpointHead).where(
                or_(RecoveryCheckpointHead.scope_key == key,
                    RecoveryCheckpointHead.manifest_hash.in_(generations))).execution_options(populate_existing=True)).scalars()]


def _transport(tree, root, pin, manifest_path):
    """Adoption-only namespace accounting; never relax generic cutover verify."""
    from startup_recovery.cutover_evidence import read_original_transport, _destination_preflight
    from startup_recovery.adoption_evidence import AdoptedEnvelope
    document, original, expected = read_original_transport(tree, manifest_path, pin)
    body = files._parse(document.raw)
    _require(body['state_root'] == str(root) and body['db_references'] == [])
    _require(body['db_reference_sha256'] == files._digest(b'[]'))
    records = [r for r in body['records'] if r['presence'] == 'PRESENT']
    _require(len(records) == 7 and {r['kind'] for r in records} == KINDS | {'paper_backup', 'visits'})
    payloads, source_hashes = {}, {}
    for record in records:
        k = record['kind']
        _require(record['classification'] == 'COPY_CANDIDATE' and record['target_path'] == k + '.json')
        current = original[record['target_path']]
        if k in KINDS:
            payloads[k], source_hashes[k] = current.raw, record['sha256']
    generations = {}
    if '.recovery-generations' in tree.entries('.'):
        names = tree.entries('.recovery-generations')
        _require(len(names) == 1 and files._valid_hash(names[0]), 'LEGACY_ADOPTION_ORPHAN_CONFLICT')
        digest = names[0]
        prefix = '.recovery-generations/' + digest + '/'
        _require(set(tree.entries(prefix[:-1])) == {'manifest.json', *(k+'.json' for k in KINDS)})
        raw = tree.read(prefix+'manifest.json')
        _require(raw.stat.mode == 0o600 and files._digest(raw.raw) == digest)
        expected[prefix+'manifest.json'] = (raw.raw, 0o600)
        envelopes = {}
        for k in sorted(KINDS):
            raw = tree.read(prefix+k+'.json')
            _require(raw.stat.mode == 0o600)
            envelopes[k] = AdoptedEnvelope(k, raw.raw)
            expected[prefix+k+'.json'] = (raw.raw, 0o600)
        manifest = files._encoded(dict(manifest_schema=1, parent_hash=None,
            files={k: files._digest(e.raw) for k,e in envelopes.items()}))
        _require(expected[prefix+'manifest.json'][0] == manifest)
        generations[digest] = envelopes
    _destination_preflight(tree, expected, complete=True)
    return document, original, payloads, source_hashes, generations


def _owner(session, token, build_id):
    from startup_recovery.store import require_owner, _proof, _unsettled
    from startup_recovery.types import HandoffEvidence, Phase
    account, attempt = require_owner(session, token)
    _require(account.phase == Phase.STATE_DISCOVERED and attempt.build_id == build_id)
    _proof(HandoffEvidence(**(account.handoff_evidence or {})))
    _require(not _unsettled(session, token.scope), 'RECOVERY_OPERATIONS_UNRESOLVED')
    return account


def adopt_verified_cutover(*, session_factory, token, build_id, snapshot_reader,
                          manifest_path, expected_manifest_sha256, state_root):
    """Fresh discovery outside the transaction; DB recheck inside owner fencing.

    snapshot_reader is a server-owned capability, not a supplied snapshot. Its
    database(session, token) method must perform only SELECTs on that transaction.
    No network callback is invoked under the owner transaction. No admission or
    producer context is created by this operation.
    """
    from models import RecoveryCheckpointHead, RecoveryLegacyAdoption
    from startup_recovery.store import scope_key
    from startup_recovery.unit_of_work import operation_uow
    from startup_recovery.reconcile import DiscoverySnapshot, digest, _database_comparison
    from startup_recovery.legacy_semantics import validate_legacy_payloads
    from startup_recovery.adoption_evidence import adopted_envelope
    from startup_recovery.cutover_evidence import _VerificationTree
    from startup_recovery.cutover_db import read_heads
    from startup_recovery.cutover_manifest import CutoverError
    _require(files._valid_hash(expected_manifest_sha256), 'MANIFEST_PIN_REQUIRED')
    root = Path(state_root)
    _require(root.is_absolute() and Path(manifest_path) == root/'.cutover'/(expected_manifest_sha256+'.json'))
    _require(callable(snapshot_reader) and callable(getattr(snapshot_reader, 'database', None)))
    key = scope_key(token.scope)
    binding = None
    publication_started = False
    try:
        fresh = snapshot_reader(token)
        _require(isinstance(fresh, DiscoverySnapshot) and fresh.token == token)
        with _VerificationTree(root) as tree, tree.shared():
            document, originals, payloads, source_hashes, generations = _transport(tree, root, expected_manifest_sha256, manifest_path)
            validated = validate_legacy_payloads(payloads, fresh)
            if not generations:
                # Fresh all-scope reference proof, with no command capability.
                with session_factory() as s:
                    engine = s.get_bind()
                heads = read_heads(engine)
                _require(heads.raw == b'[]' and heads.digest == files._digest(b'[]'), 'REFERENCE_CHANGED')
                _, observed, _, _, extra = _transport(tree, root, expected_manifest_sha256, manifest_path)
                _require(observed == originals and extra == {}, 'LEGACY_ADOPTION_SOURCE_CHANGED')
                _require(read_heads(engine) == heads, 'REFERENCE_CHANGED')
            with operation_uow(session_factory, token) as uow:
                session = uow.session
                account = _owner(session, token, build_id)
                current_db = snapshot_reader.database(session, token)
                _require(digest(_database_comparison(current_db)) == digest(_database_comparison(fresh.database)),
                         'LEGACY_ADOPTION_DATABASE_DRIFT')
                validate_legacy_payloads(payloads, DiscoverySnapshot.create(token, current_db, fresh.broker))
                binding = AdoptionBinding.create(dict(version='legacy-adoption-v1',
                    cutover_manifest_hash=expected_manifest_sha256, scope_key=key,
                    account_scope=f'CTRADER:{token.scope.environment.upper()}:{token.scope.account_id}',
                    owner_id=current_db['runtime_owners'][0], attempt_id=token.attempt_id,
                    epoch=token.epoch, boot_id=token.boot_id, build_id=build_id,
                    handoff_evidence_hash=account.handoff_evidence['evidence_hash'],
                    source_hashes=source_hashes,
                    payload_hashes={k: files._digest(raw) for k,raw in validated.canonical_payloads},
                    dependency_hashes=validated.dependency_hashes))
                rows = session.execute(select(RecoveryLegacyAdoption).where(
                    (RecoveryLegacyAdoption.scope_key == key) |
                    RecoveryLegacyAdoption.generation_manifest_hash.in_(generations)).execution_options(populate_existing=True)).scalars().all()
                _require(len(rows) <= 1)
                receipt = _receipt(rows[0]) if rows else None
                heads = _heads(session, key, generations)
                if generations:
                    retry = classify_retry(receipt, heads, generations, binding)
                    _require(retry != 'PARTIAL_OR_CONFLICTING', 'LEGACY_ADOPTION_RETRY_CONFLICT')
                    manifest_hash, envelopes = next(iter(generations.items()))
                    if retry == 'COMMITTED_COMPLETE':
                        return AdoptionResult('ALREADY_COMMITTED', receipt['receipt_digest'], manifest_hash)
                else:
                    _require(receipt is None and heads == [], 'LEGACY_ADOPTION_EXISTING_AUTHORITY')
                    timestamp = datetime.now(timezone.utc).isoformat()
                    envelopes = {}
                    for kind, payload in validated.payloads.items():
                        identity = dict(account_scope=binding.value['account_scope'], owner_id=binding.value['owner_id'],
                            symbol=None, strategy_id=None, config_hash=None, position_id=None,
                            epoch=token.epoch, boot_id=token.boot_id, build_id=build_id,
                            dependencies_hash=validated.dependency_hashes[kind], generation=1)
                        envelopes[kind] = adopted_envelope(kind, payload, identity, binding, timestamp=timestamp)
                    # All five constructions and semantic checks precede any write.
                    generated = files.write_generation(root, envelopes, None)
                    manifest_hash = generated.manifest_hash
                _, after, _, _, durable = _transport(tree, root, expected_manifest_sha256, manifest_path)
                _require(after == originals, 'LEGACY_ADOPTION_SOURCE_CHANGED')
                _require(classify_retry(None, [], durable, binding) == 'UNCOMMITTED_ORPHAN')
                _require(digest(_database_comparison(snapshot_reader.database(session, token))) ==
                         digest(_database_comparison(current_db)), 'LEGACY_ADOPTION_DATABASE_DRIFT')
                _owner(session, token, build_id)
                receipt = make_receipt(binding, manifest_hash, {k: files._digest(e.raw) for k,e in envelopes.items()})
                publication_started = True
                session.add(RecoveryLegacyAdoption(**receipt, created_at=datetime.now(timezone.utc)))
                for kind, e in envelopes.items():
                    body = checked_adopted_envelope(e)
                    session.add(RecoveryCheckpointHead(scope_key=key, kind=kind, manifest_hash=manifest_hash,
                        file_hash=receipt['envelope_hashes'][kind], generation=1, identity=body['identity'],
                        admission_hash=body['admission_hash']))
                session.flush()
            return AdoptionResult('COMMITTED', receipt['receipt_digest'], manifest_hash)
    except CutoverError as exc:
        raise RecoveryError(exc.code) from None
    except RecoveryError:
        raise
    except Exception:
        if publication_started and binding is not None:
            classification = 'PARTIAL_OR_CONFLICTING'
            try:
                with _VerificationTree(root) as tree, tree.shared():
                    _, _, _, _, durable = _transport(tree, root, expected_manifest_sha256, manifest_path)
                    with session_factory() as session:
                        rows = session.execute(select(RecoveryLegacyAdoption).where(
                            (RecoveryLegacyAdoption.scope_key == key) |
                            RecoveryLegacyAdoption.generation_manifest_hash.in_(durable))).scalars().all()
                        if len(rows) <= 1:
                            classification = classify_retry(_receipt(rows[0]) if rows else None,
                                _heads(session, key, durable), durable, binding)
            except Exception:
                pass  # Inspection failure is ambiguity, never permission to retry.
            raise RecoveryError('LEGACY_ADOPTION_COMMIT_UNCERTAIN:' + classification) from None
        raise RecoveryError('LEGACY_ADOPTION_INVALID') from None
