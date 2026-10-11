"""Short ordered transactions; never transferable network capabilities.

Account advisory namespace intentionally matches existing broker-test exclusion.
The context rejects nested sessions before they can wait on their caller's locks.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import hashlib
from sqlalchemy import text
from startup_recovery.types import RecoveryError

_active = ContextVar('recovery_transaction', default=None)


@dataclass
class UnitOfWork:
    session: object
    token: object
    last: tuple = (0, '')
    held: set = field(default_factory=set)

    def acquire(self, rank, key):
        if _active.get() is not self:
            raise RecoveryError('RECOVERY_TRANSACTION_INACTIVE')
        identity = (rank, str(key))
        if identity in self.held:
            return False  # already held: caller must not acquire again
        if rank not in range(1,9) or identity < self.last:
            raise RecoveryError('RECOVERY_LOCK_ORDER')
        self.last = identity
        self.held.add(identity)
        return True


def ordered(session, rank, key):
    current = _active.get()
    if current is None:
        return True  # legacy caller integration retains its existing behavior
    if current.session is not session:
        raise RecoveryError('RECOVERY_SESSION_CONFLICT')
    return current.acquire(rank,key)


def current_session():
    current = _active.get()
    return current.session if current else None


@contextmanager
def operation_uow(factory, context):
    token = getattr(context,'token',context)
    from startup_recovery.runtime import assert_token_usable
    assert_token_usable(token)
    with _account_transaction(factory,token.scope.account_id,token) as uow:
        yield uow


@contextmanager
def reconciliation_uow(factory,scope):
    """Ledger observation only: no manager token or broker-send capability.

    A restarted process may record exact broker evidence before ownership is
    transferred. It cannot acquire/replace the prior owner's authority here.
    """
    with _account_transaction(factory,scope.account_id,None) as uow:
        from startup_recovery.store import account_state
        account_state(uow.session,scope,lock=True)
        yield uow


@contextmanager
def _account_transaction(factory,account_id,token):
    if _active.get() is not None:
        raise RecoveryError('RECOVERY_NESTED_TRANSACTION')
    with factory() as session:
        with session.begin():
            uow = UnitOfWork(session,token)
            marker = _active.set(uow)
            try:
                uow.acquire(1, account_id)
                if session.get_bind().dialect.name == 'postgresql':
                    key = int.from_bytes(hashlib.sha256(('broker-test:'+account_id).encode()).digest()[:8], 'big', signed=True)
                    if not session.scalar(text('SELECT pg_try_advisory_xact_lock(:key)'), {'key':key}):
                        raise RecoveryError('RECOVERY_ACCOUNT_BUSY')
                elif session.get_bind().dialect.name == 'sqlite':
                    session.execute(text('BEGIN IMMEDIATE'))
                else:
                    raise RecoveryError('RECOVERY_POSTGRES_REQUIRED')
                yield uow
            finally:
                _active.reset(marker)


@contextmanager
def admitted_read_session(factory):
    session = current_session()
    if session is not None:
        with session.no_autoflush:
            yield session
    else:
        with factory() as session:
            with session.no_autoflush:
                yield session


def require_network_boundary():
    if current_session() is not None:
        raise RecoveryError('RECOVERY_NETWORK_IN_TRANSACTION')
