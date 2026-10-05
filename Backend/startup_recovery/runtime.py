"""Operation-local capability propagation; never a current-owner lookup."""
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Lock

from startup_recovery.types import ManagerToken, RecoveryError

_token = ContextVar('recovery_caller_token', default=None)
_permit = ContextVar('recovery_operation_permit', default=None)
_revoked = set()
_revoked_lock = Lock()


def invalidate_token(token):
    """Sticky process-local DENIAL only; never grants or transfers authority.

    A failed DB revocation commit must not let this boot resume when connectivity
    returns. Other boots still cannot take over without durable handoff evidence.
    """
    if isinstance(token, ManagerToken):
        with _revoked_lock:
            _revoked.add(token)


def assert_token_usable(token):
    if not isinstance(token, ManagerToken):
        raise RecoveryError('RECOVERY_TOKEN_MISSING')
    with _revoked_lock:
        if token in _revoked:
            raise RecoveryError('RECOVERY_TOKEN_INVALID')


def caller_token():
    token = _token.get()
    assert_token_usable(token)
    return token


def require_worker_admission(context=None, role=None):
    """Revalidate the caller's fixed epoch; never discover an owner for it."""
    from startup_recovery.operation_context import RecoveryOperationContext, current, ROLES
    context = context or current()
    if context is not None and not isinstance(context, RecoveryOperationContext):
        raise RecoveryError('RECOVERY_WORKER_IDENTITY_INVALID')
    token = context.token if context is not None else caller_token()
    assert_token_usable(token)
    role = role or (context.role if context is not None else 'entry')
    if role not in ROLES:
        raise RecoveryError('RECOVERY_WORKER_ROLE_INVALID')
    from db import SessionLocal
    from startup_recovery.store import require_owner
    from startup_recovery.unit_of_work import admitted_read_session
    from startup_recovery.types import Phase
    try:
        with admitted_read_session(SessionLocal) as session:
            account, _ = require_owner(session, token, lock=False)
            allowed = {Phase.NEW_ENTRIES_READY.value}
            if role != 'entry':
                allowed.add(Phase.POSITION_MANAGEMENT_READY.value)
            if account.phase not in allowed:
                raise RecoveryError('RECOVERY_NOT_COMPLETE')
            if not account.accepted_manifest_hash or (context is not None
                and account.accepted_manifest_hash != context.snapshot_hash):
                raise RecoveryError('RECOVERY_SNAPSHOT_CHANGED')
    except RecoveryError as exc:
        if exc.code in {'RECOVERY_DB_UNAVAILABLE', 'RECOVERY_DATABASE_UNAVAILABLE',
                        'RECOVERY_SNAPSHOT_CHANGED', 'RECOVERY_TOKEN_STALE',
                        'RECOVERY_TOKEN_INVALID', 'RECOVERY_ACCOUNT_CONFLICT'}:
            invalidate_token(token)
        raise
    except Exception:
        invalidate_token(token)
        raise RecoveryError('RECOVERY_DATABASE_UNAVAILABLE') from None
    return token


@contextmanager
def manager_context(token):
    if not isinstance(token, ManagerToken):
        raise RecoveryError('RECOVERY_TOKEN_MISSING')
    reset = _token.set(token)
    try:
        yield
    finally:
        _token.reset(reset)


@contextmanager
def operation_context(permit):
    if _permit.get() is not None:
        raise RecoveryError('RECOVERY_NESTED_OPERATION')
    reset = _permit.set(permit)
    try:
        yield
    finally:
        _permit.reset(reset)


def operation_permit():
    permit = _permit.get()
    if permit is None:
        raise RecoveryError('RECOVERY_MUTATION_UNFENCED')
    return permit
