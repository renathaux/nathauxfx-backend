"""Pure validation of original generation bytes; never admission or publication."""
from datetime import datetime

from startup_recovery.checkpoints import (
    AcceptedCheckpoint, CheckpointCandidate, _IDENTITY, _digest, _encoded,
    _parse, _valid_hash, validate_checkpoint,
)
from startup_recovery.cutover_manifest import (
    CutoverError, HeadSnapshot, KINDS, checked_references, require,
)


def _identity(identity):
    require(type(identity) is dict and set(identity) == _IDENTITY, 'CUTOVER_IDENTITY_INVALID')
    for key in ('account_scope','owner_id','boot_id','build_id'):
        require(isinstance(identity[key], str) and bool(identity[key]), 'CUTOVER_IDENTITY_INVALID')
    for key in ('epoch','generation'):
        require(type(identity[key]) is int and identity[key] > 0, 'CUTOVER_IDENTITY_INVALID')
    require(_valid_hash(identity['dependencies_hash']), 'CUTOVER_IDENTITY_INVALID')


def reference_rows(rows):
    """Canonicalize only representation, never fill missing reference identities."""
    rows = checked_references(rows)
    for ref in rows:
        _identity(ref['identity'])
        require(ref['broker'] == 'ctrader' and ref['environment'] in ('demo','live'), 'CUTOVER_SCOPE_INVALID')
        require(ref['identity']['account_scope'] == f"CTRADER:{ref['environment'].upper()}:{ref['account_id']}", 'CUTOVER_SCOPE_INVALID')
        require(ref['generation'] == ref['identity']['generation'], 'CUTOVER_GENERATION_INVALID')
    return rows


def validate_generation(manifest_raw, payloads, expected_manifest_hash, heads):
    """Return referenced scope:kind keys. Success proves bytes, NOT readiness.

    Payload keys are exact checkpoint kinds; all bytes named by this generation
    must be supplied, including files not currently selected by a head. Shared
    generation bytes are validated once; every referring scope remains checked.
    """
    try:
        require(type(manifest_raw) is bytes and _valid_hash(expected_manifest_hash), 'CUTOVER_MANIFEST_INVALID')
        require(_digest(manifest_raw) == expected_manifest_hash, 'CUTOVER_MANIFEST_HASH_MISMATCH')
        require(type(heads) is HeadSnapshot and type(heads.raw) is bytes, 'CUTOVER_REFERENCE_INVALID')
        require(_digest(heads.raw) == heads.digest, 'CUTOVER_REFERENCE_HASH_MISMATCH')
        refs = reference_rows(_parse(heads.raw))
        require(_encoded(refs) == heads.raw, 'CUTOVER_REFERENCE_NONCANONICAL')
        selected = [r for r in refs if r['manifest_hash'] == expected_manifest_hash]
        require(bool(selected), 'CUTOVER_GENERATION_UNREFERENCED')
        manifest = _parse(manifest_raw)
        require(type(manifest) is dict and set(manifest) == {'manifest_schema','files','parent_hash'}, 'CUTOVER_MANIFEST_INVALID')
        require(type(manifest['manifest_schema']) is int and manifest['manifest_schema'] == 1, 'CUTOVER_MANIFEST_INVALID')
        require(manifest['parent_hash'] is None or _valid_hash(manifest['parent_hash']), 'CUTOVER_PARENT_INVALID')
        files = manifest['files']
        require(type(files) is dict and bool(files) and set(files) <= KINDS, 'CUTOVER_MANIFEST_INVALID')
        require(type(payloads) is dict and set(payloads) == set(files), 'CUTOVER_PAYLOADS_INCOMPLETE')
        bodies = {}
        generation_identities = set()
        for kind, file_hash in files.items():
            raw = payloads[kind]
            require(type(raw) is bytes and _valid_hash(file_hash) and _digest(raw) == file_hash, 'CUTOVER_FILE_HASH_MISMATCH')
            body = _parse(raw)
            require(type(body) is dict and set(body) == {'checkpoint_schema','kind','identity','parent_hash',
                'admission_hash','produced_event_id','persisted_at','payload','payload_hash'}, 'CUTOVER_ENVELOPE_INVALID')
            require(type(body['checkpoint_schema']) is int and body['checkpoint_schema'] == 1, 'CUTOVER_ENVELOPE_INVALID')
            _identity(body['identity'])
            require(body['parent_hash'] == manifest['parent_hash'], 'CUTOVER_PARENT_INVALID')
            require(_valid_hash(body['admission_hash']) and isinstance(body['produced_event_id'],str) and bool(body['produced_event_id']), 'CUTOVER_ENVELOPE_INVALID')
            when = datetime.fromisoformat(body['persisted_at'].replace('Z','+00:00'))
            require(when.utcoffset() is not None and when.utcoffset().total_seconds() == 0, 'CUTOVER_ENVELOPE_INVALID')
            require(isinstance(body['payload'], list if kind == 'visits' else dict), 'CUTOVER_ENVELOPE_INVALID')
            result = validate_checkpoint(CheckpointCandidate(kind, raw, False), body['identity'], accepted_hash=file_hash)
            require(type(result) is AcceptedCheckpoint, 'CUTOVER_CHECKPOINT_REJECTED')
            ident = body['identity']
            generation_identities.add((ident['account_scope'],ident['epoch'],ident['boot_id'],ident['generation']))
            bodies[kind] = body
        require(len(generation_identities) == 1, 'CUTOVER_GENERATION_CONFLICT')
        for ref in selected:
            require(ref['kind'] in files and ref['file_hash'] == files[ref['kind']], 'CUTOVER_REFERENCE_CONFLICT')
            body = bodies[ref['kind']]
            require(_encoded(ref['identity']) == _encoded(body['identity']), 'CUTOVER_IDENTITY_MISMATCH')
            require(ref['admission_hash'] == body['admission_hash'], 'CUTOVER_ADMISSION_MISMATCH')
        return tuple(r['scope_key']+':'+r['kind'] for r in selected)
    except (ValueError, TypeError, KeyError, UnicodeError, AttributeError):
        raise CutoverError('CUTOVER_GENERATION_INVALID') from None
