"""Checkpoint publication under the durable recovery owner lock.

Directory arguments come only from reviewed server-side path configuration.
This adapter neither reads legacy files nor supplies their missing identities.
Loading proves committed lineage, NOT compatibility with current broker/DB state;
the recovery reconciler must separately check dependencies before using a payload.
"""
from sqlalchemy import select
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from decimal import Decimal
import copy
import json
from pathlib import Path
from threading import Lock
from uuid import uuid4

from models import RecoveryCheckpointHead
from startup_recovery import checkpoints as files
from startup_recovery.store import require_owner, scope_key
from startup_recovery.types import Phase, RecoveryError

_producer = ContextVar('admitted_checkpoint_producer', default=None)
_worker_producers = {}
_worker_producers_lock = Lock()
# These consumers do not authorize broker execution. Failure denies their own
# state permanently for this producer; it must not stop unrelated LIVE recovery.
ISOLATED_KINDS = frozenset({'paper_backup', 'visits', 'feature_flags'})


def _head(session, scope, kind):
    return session.execute(select(RecoveryCheckpointHead).where(
        RecoveryCheckpointHead.scope_key == scope_key(scope),
        RecoveryCheckpointHead.kind == kind).execution_options(populate_existing=True)).scalar_one_or_none()


def publish(session_factory, token, directory, envelope, *, expected_parent):
    body = files.checked_envelope(envelope)
    with session_factory.begin() as session:
        account, attempt = require_owner(session, token)
        if account.phase not in {Phase.POSITION_MANAGEMENT_READY, Phase.NEW_ENTRIES_READY}:
            raise RecoveryError('CHECKPOINT_RECOVERY_NOT_ADMITTED')
        scope_name = f'CTRADER:{token.scope.environment.upper()}:{token.scope.account_id}'
        identity = body['identity']
        if (identity['account_scope'] != scope_name or identity['epoch'] != token.epoch
            or identity['boot_id'] != token.boot_id or identity['build_id'] != attempt.build_id
            or body['admission_hash'] != account.accepted_manifest_hash):
            raise RecoveryError('CHECKPOINT_IDENTITY_MISMATCH')
        head = _head(session, token.scope, envelope.kind)
        parent = head.manifest_hash if head else None
        sequence = head.generation + 1 if head else 1
        if parent != expected_parent or body['parent_hash'] != parent:
            raise RecoveryError('CHECKPOINT_PARENT_INVALID')
        if identity['generation'] != sequence:
            raise RecoveryError('CHECKPOINT_GENERATION_INVALID')
        files.provision_directory(directory)
        # A second publisher cannot pass the owner lock. Files are durable before
        # committing a pointer, and a failed DB commit leaves the old pointer intact.
        manifest = files.write_generation(directory, {envelope.kind: envelope}, parent)
        if head is None:
            head = RecoveryCheckpointHead(scope_key=scope_key(token.scope), kind=envelope.kind)
            session.add(head)
        head.manifest_hash = manifest.manifest_hash
        head.file_hash = manifest.file_hashes[envelope.kind]
        head.generation = sequence
        head.identity = identity
        head.admission_hash = body['admission_hash']
        session.flush()
    return manifest


def load(session_factory, scope, directory, kind):
    # Intentionally no "most recent file" fallback when DB access/pointer fails.
    with session_factory() as session:
        head = _head(session, scope, kind)
        if head is None:
            return files.Absent(str(directory))
        manifest_hash, file_hash, identity = head.manifest_hash, head.file_hash, head.identity
    candidate = files.read_committed(directory, manifest_hash).get(kind)
    if candidate is None:
        raise RecoveryError('CHECKPOINT_MANIFEST_INVALID')
    return files.validate_checkpoint(candidate, identity, accepted_hash=file_hash)


def _payload_bytes(payload):
    # Preserve the representation the old JSON writer would persist. No price,
    # risk arithmetic or rounding is performed by checkpoint serialization.
    # In particular, a Decimal returned by our reader must remain a JSON number:
    # default=str would manufacture a state change on an unchanged read/save.
    def canonical(value):
        if isinstance(value, dict):
            return {key: canonical(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [canonical(item) for item in value]
        if isinstance(value, float):
            return files._parse(json.dumps(value, allow_nan=False))
        if value is None or isinstance(value, (str, int, bool, Decimal)):
            return value
        return str(value)
    return files._encoded(canonical(payload))


class RuntimeWriter:
    """Explicit producer installed only after reconciliation, never by a loader.

    Bindings are reviewed paths, immutable execution identities and reconciled
    baseline payloads. They are NOT obtained by reading files here. An unchanged
    reconciled legacy payload cannot be laundered into a new envelope.
    """
    def __init__(self, session_factory, token, bindings, admission_hash, *, absent_kinds=(), dependency_provider=None):
        self._session_factory, self._token = session_factory, token
        self._admission_hash = admission_hash
        self._dependency_provider = dependency_provider
        self._bindings = copy.deepcopy(bindings)
        self._last = {kind: _payload_bytes(binding[2]) for kind, binding in bindings.items()}
        self._absent = set(absent_kinds)
        if not self._absent.issubset(self._bindings):
            raise RecoveryError('CHECKPOINT_KIND_INVALID')
        self._lock = Lock()
        self._revoked_kinds = set()

    def read(self, path, kind):
        """Return a fresh copy of admitted/current in-memory state, never legacy bytes."""
        try:
            return self._read(path, kind)
        except Exception as exc:
            self._revoke(kind, 'RECOVERY_CHECKPOINT_READ_FAILED')
            if isinstance(exc, RecoveryError):
                raise
            raise RecoveryError('RECOVERY_CHECKPOINT_READ_FAILED') from None

    def _revoke(self, kind, reason):
        if kind in ISOLATED_KINDS:
            with self._lock:
                self._revoked_kinds.add(kind)
        else:
            from startup_recovery.coordinator import block
            block(self._session_factory, self._token, reason)

    def _read(self, path, kind):
        with self._lock:
            if kind in self._revoked_kinds:
                raise RecoveryError('CHECKPOINT_KIND_REVOKED')
            binding = self._bindings.get(kind)
            if not binding or Path(path).absolute() != Path(binding[0]).absolute():
                raise RecoveryError('CHECKPOINT_PATH_INVALID')
            from startup_recovery.unit_of_work import admitted_read_session
            with admitted_read_session(self._session_factory) as session:
                account, _ = require_owner(session, self._token, lock=False)
                if (account.phase not in {Phase.POSITION_MANAGEMENT_READY, Phase.NEW_ENTRIES_READY}
                    or account.accepted_manifest_hash != self._admission_hash):
                    raise RecoveryError('CHECKPOINT_RECOVERY_NOT_ADMITTED')
            if kind in self._absent:
                return None
            payload = files._parse(self._last[kind])
            if self._dependency_provider is not None:
                dependencies = self._dependency_provider(kind, payload)
                if dependencies != binding[1]['dependencies_hash']:
                    raise RecoveryError('CHECKPOINT_DEPENDENCIES_CHANGED')
            return payload

    def write(self, path, kind, payload):
        try:
            return self._write(path, kind, payload)
        except Exception:
            self._revoke(kind, 'RECOVERY_CHECKPOINT_WRITE_FAILED')
            raise

    def _write(self, path, kind, payload):
        with self._lock:
            if kind in self._revoked_kinds:
                raise RecoveryError('CHECKPOINT_KIND_REVOKED')
            binding = self._bindings.get(kind)
            if not binding or Path(path).absolute() != Path(binding[0]).absolute():
                raise RecoveryError('CHECKPOINT_PATH_INVALID')
            raw = _payload_bytes(payload)
            if raw == self._last[kind]:
                return None
            with self._session_factory() as session:
                head = _head(session, self._token.scope, kind)
                parent = head.manifest_hash if head else None
                generation = head.generation + 1 if head else 1
            identity = {**binding[1], 'generation': generation}
            if self._dependency_provider is not None:
                dependencies = self._dependency_provider(kind, files._parse(raw))
                if not files._valid_hash(dependencies):
                    raise RecoveryError('CHECKPOINT_DEPENDENCIES_UNAVAILABLE')
                identity['dependencies_hash'] = dependencies
            envelope = files.produced_envelope(kind, files._parse(raw), identity,
                parent_hash=parent, admission_hash=self._admission_hash,
                produced_event_id=str(uuid4()), timestamp=datetime.now(timezone.utc).isoformat())
            manifest = publish(self._session_factory, self._token, Path(path).parent,
                               envelope, expected_parent=parent)
            self._last[kind] = raw  # Only update memory after the DB commit succeeded.
            self._bindings[kind] = (binding[0], identity, files._parse(raw))
            self._absent.discard(kind)
            return manifest


@contextmanager
def checkpoint_producer(writer):
    if not isinstance(writer, RuntimeWriter):
        raise RecoveryError('CHECKPOINT_PRODUCER_NOT_ADMITTED')
    token = _producer.set(writer)
    try:
        yield
    finally:
        _producer.reset(token)


def register_worker_producer(writer):
    """Bind once to an exact admitted token, never to 'current account/owner'.

    This registry supplies checkpoint I/O only, not authority: every read/write
    still performs the existing durable epoch/admission checks.
    """
    if not isinstance(writer, RuntimeWriter):
        raise RecoveryError('CHECKPOINT_PRODUCER_NOT_ADMITTED')
    with writer._session_factory() as session:
        account, _ = require_owner(session, writer._token, lock=False)
        if account.accepted_manifest_hash != writer._admission_hash:
            raise RecoveryError('CHECKPOINT_IDENTITY_MISMATCH')
    with _worker_producers_lock:
        existing = _worker_producers.get(writer._token)
        if existing is not None and existing is not writer:
            raise RecoveryError('CHECKPOINT_PRODUCER_ALREADY_BOUND')
        _worker_producers[writer._token] = writer


@contextmanager
def worker_producer(context):
    with _worker_producers_lock:
        writer = _worker_producers.get(context.token)
    if writer is None:
        yield  # A checkpoint operation itself will deny missing admission.
    else:
        if writer._admission_hash != context.snapshot_hash:
            raise RecoveryError('CHECKPOINT_IDENTITY_MISMATCH')
        with checkpoint_producer(writer):
            yield


def write_runtime(path, kind, payload):
    writer = _producer.get()
    if writer is None:
        raise RecoveryError('CHECKPOINT_PRODUCER_NOT_ADMITTED')
    return writer.write(path, kind, payload)


def read_runtime(path, kind):
    writer = _producer.get()
    if writer is None:
        raise RecoveryError('CHECKPOINT_PRODUCER_NOT_ADMITTED')
    return writer.read(path, kind)
