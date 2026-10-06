"""Untrusted candidates and immutable, crash-safe checkpoint generations.

This module never chooses a newest file or commits a DB pointer. Its caller must
hold durable ownership and commit the returned manifest hash only AFTER all
fsyncs succeed. Reading legacy data never calls produced_envelope/write_generation.
"""
from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
from types import MappingProxyType
from uuid import uuid4

from live_integrity.binding import _emit
from startup_recovery.types import RecoveryError

_KINDS = {'live_backup', 'paper_backup', 'final_signal_hold', 'fifteen_m_swing_watch',
          'news_trading_state', 'app_settings', 'feature_flags', 'market_data_source',
          'ctrader_accounts', 'visits', 'live_monthly_history'}
_IDENTITY = {'account_scope', 'owner_id', 'symbol', 'strategy_id', 'config_hash',
             'position_id', 'epoch', 'boot_id', 'build_id', 'dependencies_hash', 'generation'}


def _reject(*_args):
    raise ValueError('Noncanonical checkpoint')


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _reject()
        result[key] = value
    return result


def _parse(raw):
    return json.loads(raw, parse_float=Decimal, parse_constant=_reject, object_pairs_hook=_pairs)


def _encoded(value):
    return _emit(value).encode('utf-8')


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _valid_hash(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def _kind(kind):
    if kind not in _KINDS:
        raise RecoveryError('CHECKPOINT_KIND_INVALID')


def _directory(path, *, create=False):
    """Walk with dirfds/O_NOFOLLOW, including parents, rather than check-then-open."""
    path = Path(path).absolute()
    if '..' in path.parts:
        raise RecoveryError('CHECKPOINT_UNSAFE_PATH')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(path.anchor, flags)
    try:
        for component in path.parts[1:]:
            if create:
                try:
                    os.mkdir(component, 0o700, dir_fd=fd)
                    os.fsync(fd)
                except FileExistsError:
                    pass
            following = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = following
        return fd
    except BaseException:
        os.close(fd)
        raise


def provision_directory(path):
    """Server-reviewed directories only; no state files and no symlink traversal."""
    try:
        os.close(_directory(path, create=True))
    except OSError:
        raise RecoveryError('CHECKPOINT_UNSAFE_PATH') from None


def _read(path):
    path = Path(path)
    parent = _directory(path.parent)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
        with os.fdopen(fd, 'rb') as handle:
            return handle.read()
    finally:
        os.close(parent)


@dataclass(frozen=True)
class Absent:
    path: str


@dataclass(frozen=True)
class CheckpointCandidate:
    kind: str
    raw: bytes
    legacy: bool
    error: str | None = None


@dataclass(frozen=True)
class RejectedCheckpoint:
    reason: str


@dataclass(frozen=True)
class AcceptedCheckpoint:
    payload_bytes: bytes
    checkpoint_hash: str

    @property
    def payload(self):
        return _parse(self.payload_bytes)


@dataclass(frozen=True)
class ProducedEnvelope:
    kind: str
    raw: bytes


@dataclass(frozen=True)
class Manifest:
    manifest_hash: str
    file_hashes: object
    parent_hash: str | None


def read_candidate(path, kind):
    _kind(kind)
    try:
        raw = _read(path)
    except FileNotFoundError:
        return Absent(str(path))
    except OSError:
        return CheckpointCandidate(kind, b'', False, 'CHECKPOINT_UNSAFE_PATH')
    try:
        body = _parse(raw)
        if not isinstance(body, dict) and not (kind == 'visits' and isinstance(body, list)):
            _reject()
        return CheckpointCandidate(kind, raw, 'checkpoint_schema' not in body)
    except (ValueError, TypeError, UnicodeError):
        return CheckpointCandidate(kind, raw, False, 'CHECKPOINT_CORRUPT')


def validate_checkpoint(candidate, expected_identity, *, accepted_hash):
    if isinstance(candidate, Absent):
        return candidate
    if candidate.error:
        return RejectedCheckpoint(candidate.error)
    if candidate.legacy:
        return RejectedCheckpoint('LEGACY_RECONCILIATION_REQUIRED')
    if not _valid_hash(accepted_hash) or _digest(candidate.raw) != accepted_hash:
        return RejectedCheckpoint('CHECKPOINT_HASH_MISMATCH')
    body = _parse(candidate.raw)
    if body.get('checkpoint_schema') != 1 or body.get('kind') != candidate.kind:
        return RejectedCheckpoint('CHECKPOINT_SCHEMA_MISMATCH')
    if body.get('identity') != expected_identity or set(expected_identity) != _IDENTITY:
        return RejectedCheckpoint('CHECKPOINT_IDENTITY_MISMATCH')
    payload = _encoded(body.get('payload'))
    if _digest(payload) != body.get('payload_hash'):
        return RejectedCheckpoint('CHECKPOINT_PAYLOAD_HASH_MISMATCH')
    return AcceptedCheckpoint(payload, accepted_hash)


def produced_envelope(kind, payload, identity, *, parent_hash, admission_hash,
                      produced_event_id, timestamp):
    """Only call for NEW state produced after successful recovery admission.

    The producer must supply its immutable execution identity, never obtain a
    current saved strategy to fill a missing legacy identity. Reconciliation of
    legacy candidates is intentionally not a constructor for this type.
    """
    _kind(kind)
    if ((not isinstance(payload, list) if kind == 'visits' else not isinstance(payload, dict))
        or isinstance(payload, CheckpointCandidate)
        or not produced_event_id or not _valid_hash(admission_hash)):
        raise RecoveryError('CHECKPOINT_NEW_STATE_REQUIRED')
    if kind == 'visits':
        from startup_recovery.publication import validate_produced_checkpoint
        validate_produced_checkpoint(kind, payload, {})
    if (set(identity) != _IDENTITY or any(identity[k] in (None, '') for k in
        ('account_scope', 'owner_id', 'epoch', 'boot_id', 'build_id', 'dependencies_hash', 'generation'))
        or type(identity['epoch']) is not int or identity['epoch'] < 1
        or type(identity['generation']) is not int or identity['generation'] < 1
        or not _valid_hash(identity['dependencies_hash'])):
        raise RecoveryError('CHECKPOINT_IDENTITY_MISSING')
    if parent_hash is not None and not _valid_hash(parent_hash):
        raise RecoveryError('CHECKPOINT_PARENT_INVALID')
    # UTC timestamp is audit evidence, never the admission/compatibility rule.
    from datetime import datetime
    try:
        when = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        if when.utcoffset() is None or when.utcoffset().total_seconds() != 0:
            _reject()
    except (ValueError, TypeError, AttributeError):
        raise RecoveryError('CHECKPOINT_TIMESTAMP_INVALID') from None
    body = dict(checkpoint_schema=1, kind=kind, identity=identity, parent_hash=parent_hash,
                admission_hash=admission_hash, produced_event_id=produced_event_id,
                persisted_at=timestamp, payload=payload, payload_hash=_digest(_encoded(payload)))
    return ProducedEnvelope(kind, _encoded(body))


def _write_at(directory_fd, name, raw):
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
    with os.fdopen(fd, 'wb') as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())


def checked_envelope(envelope):
    """Validate even internal constructed values before any filesystem mutation."""
    if not isinstance(envelope, ProducedEnvelope):
        raise RecoveryError('CHECKPOINT_NEW_STATE_REQUIRED')
    try:
        body = _parse(envelope.raw)
        checked = produced_envelope(envelope.kind, body['payload'], body['identity'],
            parent_hash=body['parent_hash'], admission_hash=body['admission_hash'],
            produced_event_id=body['produced_event_id'], timestamp=body['persisted_at'])
        if checked.raw != _encoded(body):
            raise RecoveryError('CHECKPOINT_ENVELOPE_INVALID')
        return body
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise RecoveryError('CHECKPOINT_ENVELOPE_INVALID') from None


def write_generation(directory, envelopes, expected_parent):
    if not envelopes or any(not isinstance(e, ProducedEnvelope) for e in envelopes.values()):
        raise RecoveryError('CHECKPOINT_NEW_STATE_REQUIRED')
    scopes = set()
    for kind, envelope in envelopes.items():
        _kind(kind)
        body = checked_envelope(envelope)
        if envelope.kind != kind or body['parent_hash'] != expected_parent:
            raise RecoveryError('CHECKPOINT_PARENT_INVALID')
        scopes.add(_encoded({k: body['identity'][k] for k in ('account_scope', 'epoch', 'boot_id', 'generation')}))
    if len(scopes) != 1:
        raise RecoveryError('CHECKPOINT_IDENTITY_MISMATCH')
    hashes = {kind: _digest(envelopes[kind].raw) for kind in sorted(envelopes)}
    manifest_bytes = _encoded(dict(manifest_schema=1, parent_hash=expected_parent, files=hashes))
    manifest_hash = _digest(manifest_bytes)
    root = _directory(directory)
    generation_root = temporary = None
    try:
        try:
            os.mkdir('.recovery-generations', 0o700, dir_fd=root)
        except FileExistsError:
            pass
        generation_root = os.open('.recovery-generations', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
        name = '.tmp-' + uuid4().hex
        os.mkdir(name, 0o700, dir_fd=generation_root)
        temporary = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=generation_root)
        for kind in sorted(envelopes):
            _write_at(temporary, kind + '.json', envelopes[kind].raw)
        _write_at(temporary, 'manifest.json', manifest_bytes)
        os.fsync(temporary)
        os.replace(name, manifest_hash, src_dir_fd=generation_root, dst_dir_fd=generation_root)
        os.fsync(generation_root)
        os.fsync(root)
        return Manifest(manifest_hash, MappingProxyType(hashes), expected_parent)
    finally:
        # Interrupted generations remain unaccepted evidence, never auto-recovered
        # by mtime. Do not delete/overwrite prior generations or legacy originals.
        for fd in (temporary, generation_root, root):
            if fd is not None:
                os.close(fd)


def read_committed(directory, accepted_manifest_hash):
    if accepted_manifest_hash is None:
        return Absent(str(directory))
    if not _valid_hash(accepted_manifest_hash):
        raise RecoveryError('CHECKPOINT_MANIFEST_INVALID')
    generation = Path(directory) / '.recovery-generations' / accepted_manifest_hash
    try:
        raw = _read(generation / 'manifest.json')
        if _digest(raw) != accepted_manifest_hash:
            raise RecoveryError('CHECKPOINT_MANIFEST_INVALID')
        manifest = _parse(raw)
        if manifest.get('manifest_schema') != 1 or not isinstance(manifest.get('files'), dict):
            raise RecoveryError('CHECKPOINT_MANIFEST_INVALID')
        result = {}
        for kind, checksum in manifest['files'].items():
            _kind(kind)
            candidate = read_candidate(generation / (kind + '.json'), kind)
            if not isinstance(candidate, CheckpointCandidate) or candidate.error or _digest(candidate.raw) != checksum:
                raise RecoveryError('CHECKPOINT_HASH_MISMATCH')
            result[kind] = candidate
        return result
    except (OSError, ValueError, TypeError):
        raise RecoveryError('CHECKPOINT_MANIFEST_INVALID') from None
