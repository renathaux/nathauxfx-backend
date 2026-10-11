"""Immutable worker identity. No connection, credential or mutation capability."""
from contextvars import Context, ContextVar
from contextlib import contextmanager
from functools import wraps
from dataclasses import dataclass, replace
import re

from startup_recovery.types import ManagerToken, RecoveryError

ROLES = frozenset({'management', 'entry', 'panel', 'price', 'ingestion', 'scheduler', 'request'})
_worker = ContextVar('recovery_fixed_worker', default=None)


@dataclass(frozen=True)
class RecoveryOperationContext:
    token: ManagerToken
    role: str
    snapshot_hash: str
    selection_revision: str | None = None
    operation_id: str | None = None
    position_id: str | None = None

    def __post_init__(self):
        if (not isinstance(self.token, ManagerToken) or self.role not in ROLES
            or not isinstance(self.snapshot_hash, str)
            or not re.fullmatch('[0-9a-f]{64}', self.snapshot_hash)
            or any(value is not None and not isinstance(value, str)
                   for value in (self.operation_id, self.position_id, self.selection_revision))):
            raise RecoveryError('RECOVERY_WORKER_IDENTITY_INVALID')


def current():
    return _worker.get()


def check_current():
    context = current()
    if context is not None:
        from startup_recovery.runtime import require_worker_admission
        require_worker_admission(context, context.role)


@contextmanager
def bound(context):
    """Request facade from explicit admitted identity, with no owner lookup."""
    from startup_recovery.runtime import manager_context
    from ctrader_account_context import AccountIdentity, pinned_account
    if not isinstance(context, RecoveryOperationContext):
        raise RecoveryError('RECOVERY_WORKER_IDENTITY_INVALID')
    marker = _worker.set(context)
    scope = context.token.scope
    try:
        with manager_context(context.token), pinned_account(
            AccountIdentity(scope.account_id, scope.environment, context.selection_revision)):
            yield
    finally:
        _worker.reset(marker)


def entry_ready():
    from startup_recovery.runtime import require_worker_admission
    try:
        require_worker_admission(current(), 'entry')
        return True
    except RecoveryError as exc:
        if exc.code == 'RECOVERY_NOT_COMPLETE':
            return False
        raise


@contextmanager
def publication():
    """Short local publication fence; never wrap broker/provider I/O in this."""
    context = current()
    if context is None:
        yield  # Non-worker callers retain their existing independent guards.
        return
    from db import SessionLocal
    from startup_recovery.unit_of_work import operation_uow
    from startup_recovery.store import require_owner
    from startup_recovery.types import Phase
    with operation_uow(SessionLocal, context) as uow:
        account, _ = require_owner(uow.session, context.token)
        if (account.phase not in {Phase.POSITION_MANAGEMENT_READY, Phase.NEW_ENTRIES_READY}
            or account.accepted_manifest_hash != context.snapshot_hash):
            raise RecoveryError('RECOVERY_PUBLICATION_NOT_ADMITTED')
        yield


def publishes(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with publication():
            return function(*args, **kwargs)
    return wrapped


def capture(role, *, context=None):
    """Only a boundary may capture an already-bound caller; never select an owner."""
    from startup_recovery.runtime import caller_token, require_worker_admission
    bound = context or current()
    if bound is not None:
        result = replace(bound, role=role)
    else:
        from db import SessionLocal
        from startup_recovery.store import require_owner
        token = caller_token()
        from ctrader_account_context import current_identity
        identity = current_identity()
        if identity is not None and (identity.account_id, identity.environment) != (
            token.scope.account_id, token.scope.environment):
            raise RecoveryError('RECOVERY_ACCOUNT_CONFLICT')
        with SessionLocal() as session:
            account, _ = require_owner(session, token, lock=False)
            result = RecoveryOperationContext(token, role, account.accepted_manifest_hash,
                identity.selection_revision if identity is not None else None)
    require_worker_admission(result, role)
    return result


def run(worker_context, function, *args, **kwargs):
    """Start with an empty Context: never copy a DB UOW or broker permit."""
    def invoke():
        context = worker_context
        from startup_recovery.runtime import manager_context, require_worker_admission
        from ctrader_account_context import AccountIdentity, pinned_account
        from startup_recovery.checkpoint_store import worker_producer
        require_worker_admission(context, context.role)
        scope = context.token.scope
        reset = _worker.set(context)
        try:
            with manager_context(context.token), worker_producer(context), pinned_account(
                AccountIdentity(scope.account_id, scope.environment, context.selection_revision)
            ):
                return function(*args, **kwargs)
        finally:
            _worker.reset(reset)
    return Context().run(invoke)
