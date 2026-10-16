"""Shared read-only committed lineage proof; no ownership or publication API."""
from startup_recovery.checkpoints import _digest, _encoded, _parse, _valid_hash
from startup_recovery.types import Phase, RecoveryError, scope_key


def require(value):
    if not value:
        raise RecoveryError("RECOVERY_TERMINATION_EVIDENCE_INVALID")


def read_news_checkpoint(session, snapshot, directory, build_id):
    """Startup reads a selected head and full ancestry, never a newest file."""
    from pathlib import Path
    from models import RecoveryAccount
    from startup_recovery.cutover_evidence import _VerificationTree
    from startup_recovery.news_strategy_compatibility import validate_lineage
    heads = [h for h in snapshot.database['checkpoint_heads'] if h['kind'] == 'news_trading_state']
    require(len(heads) == 1)
    account = session.get(RecoveryAccount, scope_key(snapshot.token.scope), populate_existing=True)
    require(account is not None)
    with _VerificationTree(Path(directory)) as tree, tree.shared():
        candidate, projection = read_lineage(tree, session, snapshot.token, build_id,
            account.accepted_manifest_hash, heads[0])
        validate_lineage(snapshot.database, projection, _parse(candidate.raw)['payload'],
                         scope_key(snapshot.token.scope))
    return candidate


def read_generation(tree, session, key, generation):
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


def read_provenance(session, token, build_id, historical_hash, body, receipt):
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
        RecoveryAttempt.scope_key == scope_key(token.scope),
        RecoveryAttempt.epoch == identity['epoch']
    ).execution_options(populate_existing=True)).all()
    require(len(rows) == 1)
    producer = rows[0]
    require(producer.boot_id == identity['boot_id'] and producer.build_id == identity['build_id']
        and producer.phase == Phase.RELINQUISHED and producer.phase_evidence_hash == body['admission_hash'])
    return dict(classification='PRODUCED_INHERITED', claim='CURRENTLY_ACCEPTED_INHERITED_AUTHORITY',
        producer=dict(attempt_id=producer.attempt_id, epoch=producer.epoch, boot_id=producer.boot_id,
            build_id=producer.build_id, phase=producer.phase, phase_evidence_hash=producer.phase_evidence_hash))


def read_lineage(tree, session, token, build_id, historical_hash, head):
    generation, seen, lineage = head['manifest_hash'], set(), []
    sequence, child_epoch, selected = head['generation'], token.epoch, None
    require(type(sequence) is int and sequence > 0)
    while generation is not None:
        require(generation not in seen)
        seen.add(generation)
        manifest, bodies, candidates, receipt = read_generation(
            tree, session, scope_key(token.scope), generation)
        kind = head['kind']
        require(kind in bodies)
        body = bodies[kind]
        identity = body['identity']
        require(identity['generation'] == sequence and identity['epoch'] <= child_epoch)
        provenance = read_provenance(session, token, build_id, historical_hash, body, receipt)
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
