"""Trusted administrative termination adapter: relinquish only, never takeover.

The caller must independently approve/pin retained platform termination evidence.
This function validates its binding, not the truth of a caller-invented document.
It is not an HTTP endpoint or an automatic recovery callback. Run it in a fresh
administrative process after the predecessor is proven terminated; never reset
the predecessor process's revoked-token set.

store.relinquish remains the lower-level fenced mutation primitive. It checks
owner/proof and its existing DB guards, but does not obtain this adapter's fresh
broker/database observations. Canonical submission safety is enforced here,
including on the locked database reread, before calling that unchanged primitive.
"""
from dataclasses import asdict, dataclass
from startup_recovery import store
from startup_recovery.checkpoints import _parse, _encoded, _digest, _valid_hash
from startup_recovery.reconcile import digest, _database_comparison, _broker_comparison, _submission_pending
from startup_recovery.types import RecoveryError, ManagerToken, HandoffEvidence, Phase
from startup_recovery.unit_of_work import reconciliation_uow


@dataclass(frozen=True)
class RelinquishmentResult:
    status: str
    evidence_sha256: str
    predecessor_epoch: int


def require(value):
    if not value:
        raise RecoveryError('RECOVERY_TERMINATION_EVIDENCE_INVALID')


def safety(value, scope):
    d,b=value['database'],value['broker']
    require(d['selection']['account_id']==scope.account_id
            and d['selection']['environment']==scope.environment)
    require(b['account_id']==scope.account_id and b['environment']==scope.environment)
    require(b['complete'] is True and b['authenticated'] is True)
    require(b['positions']==[] and b['orders']==[])
    settings=[r for r in d['settings'] if r.get('setting_name')=='live_auto_trade_enabled']
    require(len(settings)==1 and settings[0]['setting_value']=='false')
    for row in d['submissions']:
        require(not _submission_pending(row))
    for row in d['lifecycles']:
        require(row.get('status') not in {'SUBMITTING','RECONCILIATION_REQUIRED'})
        require(not row.get('broker_position_id'))
        require(not (row.get('tp1_requested_at') and not row.get('tp1_completed_at')))
        require((row.get('management_state') or {}).get('protection_state') not in {'PENDING','FAILED'})
    return digest(_database_comparison(d)),digest(_broker_comparison(b))


def relinquish_terminated_owner(*,session_factory,scope,expected_attempt_id,expected_epoch,
        predecessor_boot_id,predecessor_build_id,predecessor_deployment_id,
        successor_release_identity,evidence_document,expected_evidence_sha256,safety_reader):
    mutation_started=False
    try:
        require(isinstance(evidence_document,bytes) and _valid_hash(expected_evidence_sha256))
        require(_digest(evidence_document)==expected_evidence_sha256)
        doc=_parse(evidence_document)
        require(_encoded(doc)==evidence_document)
        require(set(doc)=={'schema','kind','scope','attempt_id','epoch','boot_id','build_id',
            'deployment_id','successor_release_identity','predecessor_status',
            'operations_reconciled','termination_record_sha256'})
        require(type(doc['schema']) is int and doc['schema']==1)
        require(doc['kind']=='operator-termination' and doc['predecessor_status']=='TERMINATED'
            and doc['operations_reconciled'] is True and _valid_hash(doc['termination_record_sha256']))
        require(type(expected_epoch) is int and expected_epoch>0 and type(doc['epoch']) is int)
        require(_valid_hash(successor_release_identity))
        for value in (expected_attempt_id,predecessor_boot_id,predecessor_build_id,predecessor_deployment_id):
            require(isinstance(value,str) and bool(value))
        require(doc['scope']==asdict(scope) and doc['attempt_id']==expected_attempt_id
            and doc['epoch']==expected_epoch and doc['boot_id']==predecessor_boot_id
            and doc['build_id']==predecessor_build_id and doc['deployment_id']==predecessor_deployment_id
            and doc['successor_release_identity']==successor_release_identity)
        require(callable(safety_reader) and callable(getattr(safety_reader,'database',None)))
        first=safety_reader(scope)
        expected=safety(first,scope)
        require(safety(safety_reader(scope),scope)==expected)
        token=ManagerToken(scope,expected_attempt_id,expected_epoch,predecessor_boot_id)
        with reconciliation_uow(session_factory,scope) as uow:
            account,attempt=store.require_owner(uow.session,token)
            require(account.phase==Phase.BLOCKED and attempt.phase==Phase.BLOCKED
                and attempt.build_id==predecessor_build_id)
            require((account.handoff_evidence or {}).get('evidence_hash')!=expected_evidence_sha256)
            current=safety_reader.database(uow.session,scope)
            require(safety(dict(database=current,broker=first['broker']),scope)[0]==expected[0])
            require(not store._unsettled(uow.session,scope))
            proof=HandoffEvidence('operator-termination',expected_evidence_sha256,
                predecessor_boot_id,True,True)
            mutation_started=True
            store.relinquish(uow.session,token,proof)
        return RelinquishmentResult('RELINQUISHED',expected_evidence_sha256,expected_epoch)
    except RecoveryError:
        raise
    except Exception:
        code='RECOVERY_HANDOFF_COMMIT_INSPECT_REQUIRED' if mutation_started else 'RECOVERY_TERMINATION_EVIDENCE_INVALID'
        raise RecoveryError(code) from None


def _ready_authority(tree, session, token, build_id, accepted_manifest_hash, database):
    """Read-only, exact five-head/one-receipt authority, never an orphan loader.

    Caller holds the existing shared filesystem lock before the account UOW.
    Flat runtime files are deliberately not compared to transported bytes: after
    admission they are mutable mirrors, not this checkpoint authority identity.
    Any successor generation or unexplained namespace requires another design.
    """
    from sqlalchemy import select, or_
    from models import RecoveryLegacyAdoption
    from startup_recovery.adoption_evidence import (
        KINDS, AdoptedEnvelope, validate_adoption_receipt, checked_adopted_envelope,
    )
    from startup_recovery.legacy_adoption import _receipt, _heads, classify_retry
    from startup_recovery.cutover_manifest import decode_manifest
    from startup_recovery.cutover_evidence import _verify_coverage

    key = store.scope_key(token.scope)
    generations = tree.entries('.recovery-generations')
    require(len(generations) == 1 and _valid_hash(generations[0]))
    generation = generations[0]
    rows = session.scalars(select(RecoveryLegacyAdoption).where(or_(
        RecoveryLegacyAdoption.scope_key == key,
        RecoveryLegacyAdoption.generation_manifest_hash == generation
    )).execution_options(populate_existing=True)).all()
    require(len(rows) == 1)
    receipt = _receipt(rows[0])
    require(receipt['generation_manifest_hash'] == generation)
    heads = _heads(session, key, (generation,))
    require(len(heads) == 5 and {h['kind'] for h in heads} == KINDS)
    prefix = '.recovery-generations/' + generation + '/'
    require(set(tree.entries(prefix[:-1])) == {'manifest.json', *(k+'.json' for k in KINDS)})
    manifest = tree.read(prefix+'manifest.json')
    require(manifest.stat.mode == 0o600 and _digest(manifest.raw) == generation)
    envelopes = {}
    for kind in sorted(KINDS):
        image = tree.read(prefix+kind+'.json')
        require(image.stat.mode == 0o600)
        envelopes[kind] = AdoptedEnvelope(kind, image.raw)
    hashes = {k: _digest(e.raw) for k, e in envelopes.items()}
    require(manifest.raw == _encoded(dict(manifest_schema=1, parent_hash=None, files=hashes)))
    binding = validate_adoption_receipt(receipt, envelopes, key)
    require(classify_retry(receipt, heads, {generation: envelopes}, binding) == 'COMMITTED_COMPLETE')
    bound = binding.value
    require(bound['attempt_id'] == token.attempt_id and bound['epoch'] == token.epoch
        and bound['boot_id'] == token.boot_id and bound['build_id'] == build_id
        and bound['account_scope'] == f'CTRADER:{token.scope.environment.upper()}:{token.scope.account_id}'
        and token.scope.broker == 'ctrader')
    require(database['runtime_owners'] == [bound['owner_id']])

    # Verify retained transport metadata, not post-admission flat-file bytes.
    pin = receipt['cutover_manifest_hash']
    require(set(tree.entries('.cutover')) == {'lock', pin+'.json'})
    lock = tree.read('.cutover/lock')
    require(lock.raw == b'' and lock.stat.mode == 0o600)
    image = tree.read('.cutover/'+pin+'.json')
    require(image.stat.mode == 0o400)
    document = decode_manifest(image.raw, pin)
    body = _parse(document.raw)
    _verify_coverage(body)
    require(body['state_root'] == str(tree.root) and body['db_references'] == []
        and body['db_reference_sha256'] == _digest(b'[]'))
    records = [r for r in body['records'] if r['presence'] == 'PRESENT']
    require(len(records) == 7 and {r['kind'] for r in records} == KINDS | {'paper_backup', 'visits'})
    for record in records:
        require(record['classification'] == 'COPY_CANDIDATE'
            and record['target_path'] == record['kind']+'.json')
        if record['kind'] in KINDS:
            require(record['sha256'] == receipt['source_hashes'][record['kind']])
    require(set(tree.entries('.')) == {'.cutover', '.recovery-generations',
        *(r['target_path'] for r in records)})
    projections = []
    for kind, envelope in sorted(envelopes.items()):
        checked = checked_adopted_envelope(envelope)
        projections.append(dict(kind=kind, sha256=hashes[kind], identity=checked['identity'],
                                adoption=checked['adoption']))
    authority = dict(schema=1, scope=asdict(token.scope), accepted_manifest_hash=accepted_manifest_hash,
        receipt=receipt, heads=sorted(heads, key=lambda h: h['kind']),
        generation_manifest_hash=generation, envelopes=projections)
    return _digest(b'flowsignal-ready-owner-authority-v1\0' + _encoded(authority))


def relinquish_terminated_ready_owner(*, session_factory, scope, expected_attempt_id, expected_epoch,
        predecessor_boot_id, predecessor_build_id, predecessor_deployment_id,
        successor_release_identity, evidence_document, expected_evidence_sha256, safety_reader,
        expected_accepted_manifest_hash, expected_checkpoint_authority_sha256, state_root):
    """Administrative graceful drain of an independently proven terminated owner.

    Only NEW_ENTRIES_READY with the original five adopted heads is supported.
    Expected evidence/authority digests must be independently approved, not
    generated by this mutation call. This never resumes, acquires or allocates.
    The server-owned reader performs two fresh observations outside ownership
    locks; its database method must perform only SELECTs on the supplied session.
    """
    from pathlib import Path
    from startup_recovery.cutover_evidence import _VerificationTree

    mutation_started = False
    try:
        require(isinstance(evidence_document, bytes) and _valid_hash(expected_evidence_sha256))
        require(_digest(evidence_document) == expected_evidence_sha256)
        require(_valid_hash(expected_accepted_manifest_hash) and _valid_hash(expected_checkpoint_authority_sha256))
        require(type(expected_epoch) is int and expected_epoch > 0 and _valid_hash(successor_release_identity))
        for value in (expected_attempt_id, predecessor_boot_id, predecessor_build_id, predecessor_deployment_id):
            require(isinstance(value, str) and bool(value))
        doc = _parse(evidence_document)
        require(type(doc) is dict and _encoded(doc) == evidence_document)
        expected = dict(schema=1, kind='graceful-drain', scope=asdict(scope),
            attempt_id=expected_attempt_id, epoch=expected_epoch, boot_id=predecessor_boot_id,
            build_id=predecessor_build_id, deployment_id=predecessor_deployment_id,
            successor_release_identity=successor_release_identity, predecessor_status='TERMINATED',
            predecessor_phase='NEW_ENTRIES_READY', operations_reconciled=True,
            termination_record_sha256=doc.get('termination_record_sha256'),
            accepted_manifest_hash=expected_accepted_manifest_hash,
            checkpoint_authority_sha256=expected_checkpoint_authority_sha256)
        require(_valid_hash(expected['termination_record_sha256']) and _encoded(doc) == _encoded(expected))
        require(callable(safety_reader) and callable(getattr(safety_reader, 'database', None)))
        first = safety_reader(scope)
        observations = safety(first, scope)
        require(safety(safety_reader(scope), scope) == observations)
        token = ManagerToken(scope, expected_attempt_id, expected_epoch, predecessor_boot_id)
        root = Path(state_root)
        require(root.is_absolute())
        # Match adoption's existing filesystem -> account -> owner lock order.
        with _VerificationTree(root) as tree, tree.shared():
            with reconciliation_uow(session_factory, scope) as uow:
                account, attempt = store.require_owner(uow.session, token)
                require(account.phase == Phase.NEW_ENTRIES_READY and attempt.phase == Phase.NEW_ENTRIES_READY
                    and attempt.build_id == predecessor_build_id
                    and account.accepted_manifest_hash == expected_accepted_manifest_hash)
                require((account.handoff_evidence or {}).get('evidence_hash') != expected_evidence_sha256)
                current = safety_reader.database(uow.session, scope)
                require(safety(dict(database=current, broker=first['broker']), scope)[0] == observations[0])
                require(not store._unsettled(uow.session, scope))
                require(_ready_authority(tree, uow.session, token, predecessor_build_id,
                    expected_accepted_manifest_hash, current) == expected_checkpoint_authority_sha256)
                proof = HandoffEvidence('graceful-drain', expected_evidence_sha256,
                    predecessor_boot_id, True, True)
                mutation_started = True
                store.relinquish(uow.session, token, proof)
        return RelinquishmentResult('RELINQUISHED', expected_evidence_sha256, expected_epoch)
    except Exception as exc:
        if mutation_started:
            raise RecoveryError('RECOVERY_HANDOFF_COMMIT_INSPECT_REQUIRED') from None
        if isinstance(exc, RecoveryError):
            raise
        raise RecoveryError('RECOVERY_TERMINATION_EVIDENCE_INVALID') from None


def _runtime_safety(observation, scope):
    # DB row order is not checkpoint authority; never alter the reader's copy.
    database = dict(observation['database'])
    database['checkpoint_heads'] = sorted(database['checkpoint_heads'], key=lambda h: h['kind'])
    return safety(dict(database=database, broker=observation['broker']), scope)


def _runtime_fresh(broker, started):
    """Same 20s budget as certified broker observations; never extend under lock."""
    from time import time, monotonic
    from math import isfinite
    observed = broker.get('observed_at')
    require(type(observed) in (int, float) and isfinite(observed))
    require(0 <= time() - observed <= 20 and 0 <= monotonic() - started <= 20)


def _runtime_generation(tree, session, key, generation):
    """Only an explicit head/ancestor reference is readable; no orphan discovery."""
    from sqlalchemy import select
    from models import RecoveryLegacyAdoption
    from startup_recovery.checkpoints import _KINDS, ProducedEnvelope, checked_envelope, CheckpointCandidate
    from startup_recovery.adoption_evidence import (
        AdoptedEnvelope, checked_adopted_envelope, receipt_validated_candidates,
    )
    from startup_recovery.legacy_adoption import _receipt

    require(_valid_hash(generation))
    prefix = '.recovery-generations/' + generation + '/'
    image = tree.read(prefix + 'manifest.json')
    require(image.stat.mode == 0o600 and _digest(image.raw) == generation)
    manifest = _parse(image.raw)
    require(type(manifest) is dict and set(manifest) == {'manifest_schema', 'parent_hash', 'files'}
        and type(manifest['manifest_schema']) is int and manifest['manifest_schema'] == 1
        and _encoded(manifest) == image.raw)
    require(manifest['parent_hash'] is None or _valid_hash(manifest['parent_hash']))
    hashes = manifest['files']
    require(type(hashes) is dict and bool(hashes) and set(hashes) <= _KINDS)
    require(set(tree.entries(prefix[:-1])) == {'manifest.json', *(k+'.json' for k in hashes)})
    bodies, envelopes, candidates = {}, {}, {}
    adopted = set()
    for kind in sorted(hashes):
        require(_valid_hash(hashes[kind]))
        image = tree.read(prefix + kind + '.json')
        require(image.stat.mode == 0o600 and _digest(image.raw) == hashes[kind])
        body = _parse(image.raw)
        require(type(body) is dict and _encoded(body) == image.raw)
        if 'adoption' in body:
            envelope = AdoptedEnvelope(kind, image.raw)
            body = checked_adopted_envelope(envelope)
            adopted.add(kind)
        else:
            envelope = ProducedEnvelope(kind, image.raw)
            body = checked_envelope(envelope)
        require(body['parent_hash'] == manifest['parent_hash'])
        bodies[kind], envelopes[kind] = body, envelope
        candidates[kind] = CheckpointCandidate(kind, image.raw, False)
    # Same generation-wide identity contract as write_generation; even an
    # unselected sibling must not make the selected manifest incoherent.
    require(len({_encoded({field: bodies[kind]['identity'][field]
        for field in ('account_scope', 'epoch', 'boot_id', 'generation')}) for kind in bodies}) == 1)
    receipt = None
    if adopted:
        require(adopted == set(hashes))
        rows = session.scalars(select(RecoveryLegacyAdoption).where(
            RecoveryLegacyAdoption.scope_key == key,
            RecoveryLegacyAdoption.generation_manifest_hash == generation
        ).execution_options(populate_existing=True)).all()
        require(len(rows) == 1)
        receipt = _receipt(rows[0])
        require(receipt['generation_manifest_hash'] == generation)
        # This constructor validates the complete receipt against these exact
        # local envelope bytes before issuing candidate proofs. Do not validate
        # twice or reuse validation across separate generation reads.
        candidates = receipt_validated_candidates(receipt, envelopes, key)
    return manifest, bodies, candidates, receipt


def _runtime_provenance(session, token, build_id, historical_hash, body, receipt):
    from sqlalchemy import select
    from models import RecoveryAttempt

    identity = body['identity']
    require(token.scope.broker == 'ctrader'
        and identity['account_scope'] == f'CTRADER:{token.scope.environment.upper()}:{token.scope.account_id}'
        and type(identity['epoch']) is int and 0 < identity['epoch'] <= token.epoch)
    if receipt is not None:
        return dict(classification='ADOPTED', receipt=receipt)
    if identity['epoch'] == token.epoch:
        require(identity['boot_id'] == token.boot_id and identity['build_id'] == build_id
            and body['admission_hash'] == historical_hash)
        return dict(classification='PRODUCED_CURRENT')
    rows = session.scalars(select(RecoveryAttempt).where(
        RecoveryAttempt.scope_key == store.scope_key(token.scope),
        RecoveryAttempt.epoch == identity['epoch']
    ).execution_options(populate_existing=True)).all()
    require(len(rows) == 1)
    producer = rows[0]
    require(producer.boot_id == identity['boot_id'] and producer.build_id == identity['build_id']
        and producer.phase == Phase.RELINQUISHED and producer.phase_evidence_hash == body['admission_hash'])
    return dict(classification='PRODUCED_INHERITED', claim='CURRENTLY_ACCEPTED_INHERITED_AUTHORITY',
        producer=dict(attempt_id=producer.attempt_id, epoch=producer.epoch, boot_id=producer.boot_id,
            build_id=producer.build_id, phase=producer.phase, phase_evidence_hash=producer.phase_evidence_hash))


def _runtime_lineage(tree, session, token, build_id, historical_hash, head):
    generation, seen, lineage = head['manifest_hash'], set(), []
    sequence, child_epoch, selected = head['generation'], token.epoch, None
    require(type(sequence) is int and sequence > 0)
    while generation is not None:
        require(generation not in seen)
        seen.add(generation)
        manifest, bodies, candidates, receipt = _runtime_generation(
            tree, session, store.scope_key(token.scope), generation)
        kind = head['kind']
        require(kind in bodies)
        body = bodies[kind]
        identity = body['identity']
        require(identity['generation'] == sequence and identity['epoch'] <= child_epoch)
        provenance = _runtime_provenance(session, token, build_id, historical_hash, body, receipt)
        if selected is None:
            require(head['identity'] == identity and _encoded(head['identity']) == _encoded(identity)
                and head['file_hash'] == manifest['files'][kind]
                and head['admission_hash'] == body['admission_hash'])
            selected = candidates[kind], provenance
        lineage.append(dict(manifest_hash=generation, file_hash=manifest['files'][kind], **provenance))
        sequence -= 1
        child_epoch = identity['epoch']
        generation = manifest['parent_hash']
        require((generation is None) == (sequence == 0))
    require(selected is not None)
    return selected[0], dict(**head, **selected[1], envelope_sha256=_digest(selected[0].raw), lineage=lineage)


def _runtime_authority(tree, session, token, build_id, historical_hash, observation):
    """Read-only two-proof projection; callable by independent pin collection.

    A historical recovery result is not a historical checkpoint snapshot. Only
    today's complete DB-selected set enters today's unchanged reconciliation.
    Unreferenced generations are UNREFERENCED_EVIDENCE, never loaded or repaired.
    """
    from sqlalchemy import select
    from models import RecoveryCheckpointHead
    from startup_recovery.checkpoints import _KINDS
    from startup_recovery.reconcile import DiscoverySnapshot, reconcile

    require(_valid_hash(historical_hash))
    account, attempt = store.require_owner(session, token, lock=False)
    require(account.broker == token.scope.broker and account.environment == token.scope.environment
        and account.account_id == token.scope.account_id)
    require(account.phase == Phase.NEW_ENTRIES_READY and attempt.phase == Phase.NEW_ENTRIES_READY
        and attempt.build_id == build_id and attempt.conflict_reason is None
        and account.accepted_manifest_hash == historical_hash and attempt.phase_evidence_hash == historical_hash)
    key = store.scope_key(token.scope)
    rows = session.scalars(select(RecoveryCheckpointHead).where(
        RecoveryCheckpointHead.scope_key == key).execution_options(populate_existing=True)).all()
    heads = [dict(scope_key=row.scope_key, kind=row.kind, manifest_hash=row.manifest_hash,
        file_hash=row.file_hash, generation=row.generation, identity=row.identity,
        admission_hash=row.admission_hash) for row in rows]
    kinds = [h['kind'] for h in heads]
    require(bool(heads) and len(kinds) == len(set(kinds)) and set(kinds) <= _KINDS)
    # The injected server reader cannot omit a DB-selected kind or substitute a pointer.
    database = observation['database']
    require(sorted(database['checkpoint_heads'], key=lambda h: h['kind']) == sorted(heads, key=lambda h: h['kind']))
    comparisons = _runtime_safety(observation, token.scope)
    candidates, projections = {}, []
    for head in sorted(heads, key=lambda h: h['kind']):
        require(head['identity']['owner_id'] in database['runtime_owners'])
        candidate, projection = _runtime_lineage(tree, session, token, build_id, historical_hash, head)
        candidates[head['kind']] = candidate
        projections.append(projection)
    result = reconcile(DiscoverySnapshot.create(token, database, observation['broker']), candidates)
    require(result.ok and result.conflicts == () and set(result.checkpoints) == set(kinds)
        and not set(kinds).intersection(result.quarantined))
    return dict(schema=1, scope=asdict(token.scope), current=dict(attempt_id=token.attempt_id,
        epoch=token.epoch, boot_id=token.boot_id, build_id=build_id),
        historical=dict(owner_attempt_id=account.owner_attempt_id, owner_epoch=account.owner_epoch,
            account_phase=account.phase, attempt_phase=attempt.phase, conflict_reason=attempt.conflict_reason,
            historical_admission_hash=historical_hash, phase_evidence_hash=attempt.phase_evidence_hash),
        authority=dict(current_reconciliation_hash=result.manifest_hash,
            current_dependencies_hash=result.dependencies_hash, conflicts=list(result.conflicts),
            quarantined=list(result.quarantined), heads=projections),
        database_safety_sha256=comparisons[0], broker_safety_sha256=comparisons[1])


def relinquish_terminated_runtime_owner(*, session_factory, scope, expected_attempt_id, expected_epoch,
        predecessor_boot_id, predecessor_build_id, predecessor_deployment_id,
        successor_release_identity, evidence_document, expected_evidence_sha256, safety_reader,
        expected_historical_admission_hash, expected_current_runtime_authority_sha256, state_root):
    """Administrative-only successor drain. Independent pins precede this call.

    Two fresh network observations precede filesystem -> account -> owner locks.
    The retained immutable broker observation expires after at most 20 seconds,
    including lock wait, lineage reads and reconciliation. A failure is not retry
    authority; after mutation starts every failure requires commit inspection.
    """
    from pathlib import Path
    from time import monotonic
    from startup_recovery.cutover_evidence import _VerificationTree
    from startup_recovery.reconcile import freeze, thaw

    mutation_started = False
    try:
        require(isinstance(evidence_document, bytes) and _valid_hash(expected_evidence_sha256)
            and _digest(evidence_document) == expected_evidence_sha256)
        require(_valid_hash(expected_historical_admission_hash)
            and _valid_hash(expected_current_runtime_authority_sha256) and _valid_hash(successor_release_identity))
        require(type(expected_epoch) is int and expected_epoch > 0)
        for value in (expected_attempt_id, predecessor_boot_id, predecessor_build_id, predecessor_deployment_id):
            require(isinstance(value, str) and bool(value))
        doc = _parse(evidence_document)
        require(type(doc) is dict and _encoded(doc) == evidence_document)
        expected = dict(schema=1, kind='graceful-drain', scope=asdict(scope),
            attempt_id=expected_attempt_id, epoch=expected_epoch, boot_id=predecessor_boot_id,
            build_id=predecessor_build_id, deployment_id=predecessor_deployment_id,
            successor_release_identity=successor_release_identity, predecessor_status='TERMINATED',
            predecessor_phase='NEW_ENTRIES_READY', operations_reconciled=True,
            termination_record_sha256=doc.get('termination_record_sha256'),
            historical_admission_hash=expected_historical_admission_hash,
            current_runtime_authority_sha256=expected_current_runtime_authority_sha256)
        require(_valid_hash(expected['termination_record_sha256']) and _encoded(doc) == _encoded(expected))
        require(callable(safety_reader) and callable(getattr(safety_reader, 'database', None)))
        started = monotonic()
        first = thaw(freeze(safety_reader(scope)))
        _runtime_fresh(first['broker'], started)
        comparisons = _runtime_safety(first, scope)
        second = thaw(freeze(safety_reader(scope)))
        _runtime_fresh(second['broker'], started)
        require(second['broker']['observed_at'] >= first['broker']['observed_at'])
        require(_runtime_safety(second, scope) == comparisons)
        token = ManagerToken(scope, expected_attempt_id, expected_epoch, predecessor_boot_id)
        root = Path(state_root)
        require(root.is_absolute())
        with _VerificationTree(root) as tree, tree.shared():
            with reconciliation_uow(session_factory, scope) as uow:
                store.require_owner(uow.session, token)
                current = safety_reader.database(uow.session, scope)
                observation = dict(database=current, broker=second['broker'])
                require(not store._unsettled(uow.session, scope))
                authority = _runtime_authority(tree, uow.session, token, predecessor_build_id,
                    expected_historical_admission_hash, observation)
                # Authority validation fully checks this fresh locked observation.
                # Reuse only its returned canonical identities, within this call;
                # independent authority collection retains the same full checks.
                require((authority['database_safety_sha256'], authority['broker_safety_sha256'])
                    == comparisons)
                require(_digest(b'flowsignal-runtime-owner-authority-v1\0' + _encoded(authority))
                    == expected_current_runtime_authority_sha256)
                _runtime_fresh(second['broker'], started)
                proof = HandoffEvidence('graceful-drain', expected_evidence_sha256, predecessor_boot_id, True, True)
                mutation_started = True
                store.relinquish(uow.session, token, proof)
        return RelinquishmentResult('RELINQUISHED', expected_evidence_sha256, expected_epoch)
    except Exception as exc:
        if mutation_started:
            raise RecoveryError('RECOVERY_HANDOFF_COMMIT_INSPECT_REQUIRED') from None
        if isinstance(exc, RecoveryError):
            raise
        raise RecoveryError('RECOVERY_TERMINATION_EVIDENCE_INVALID') from None
