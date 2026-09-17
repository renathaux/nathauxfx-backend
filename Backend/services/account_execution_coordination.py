"""Nonblocking account coordination; durable fences have no timeout or TTL."""
from contextlib import contextmanager
from contextvars import ContextVar
import fcntl
import hashlib
import os
import tempfile

from sqlalchemy import select, text
from models import BrokerIntegrationTestSubmission
from services.broker_integration_test_errors import BlockerCode

_execution_account = ContextVar('normal_execution_account', default=None)


class ExecutionFenced(RuntimeError):
    code = BlockerCode.EXECUTION_FENCED


@contextmanager
def account_lock(session_factory, account_id):
    account_id = str(account_id)
    if not account_id.isdecimal():
        yield
        return
    with session_factory() as session:
        engine = session.get_bind()
    if engine.dialect.name == 'postgresql':
        # Session-level advisory locks are unsafe behind transaction poolers.
        # A dedicated connection holds a transaction lock across all broker work.
        with engine.connect() as connection, connection.begin():
            key = int.from_bytes(hashlib.sha256(('broker-test:' + account_id).encode()).digest()[:8], 'big', signed=True)
            acquired = connection.execute(text('SELECT pg_try_advisory_xact_lock(:key)'),
                {'key': key}).scalar()
            if not acquired:
                raise ExecutionFenced('Broker test account busy')
            yield
    elif engine.dialect.name == 'sqlite':
        identity = hashlib.sha256(f'{engine.url}:{account_id}'.encode()).hexdigest()
        path = os.path.join(tempfile.gettempdir(), f'flowsignal-broker-test-{identity}.lock')
        with open(path, 'a') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ExecutionFenced('Broker test account busy') from exc
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
    else:
        raise ExecutionFenced('Unsupported coordination database')


def run_normal_submission(session_factory, account_id, callback):
    token = _execution_account.set(str(account_id))
    try:
        return _run_normal_submission(session_factory, account_id, callback)
    finally:
        _execution_account.reset(token)


def assert_execution_account(account_id):
    expected = _execution_account.get()
    if expected is not None and expected != str(account_id):
        raise ExecutionFenced('Selected broker account changed during execution')


def _run_normal_submission(session_factory, account_id, callback):
    if not str(account_id).isdecimal():
        return callback()
    with account_lock(session_factory, account_id):
        with session_factory() as session:
            pending = session.scalar(select(BrokerIntegrationTestSubmission.test_id).where(
                BrokerIntegrationTestSubmission.unresolved_account == str(account_id)))
            if pending:
                raise ExecutionFenced('Unresolved broker integration test')
        return callback()


def exclude_test_positions(session_factory, account_id, positions, *, closed_history=False):
    """Exclude only durable exact identity matches, including terminal tests."""
    if not str(account_id).isdecimal():
        return positions
    with session_factory() as session:
        rows = session.scalars(select(BrokerIntegrationTestSubmission).where(
            BrokerIntegrationTestSubmission.account_id == str(account_id))).all()
        if closed_history and any(row.unresolved_account and row.request_started_at
                                  and not row.broker_position_id for row in rows):
            error = ExecutionFenced('Closed history identity unresolved')
            error.code = BlockerCode.HISTORY_IDENTITY_UNRESOLVED
            raise error
        refs = {row.reference for row in rows}
        ids = {row.broker_position_id for row in rows if row.broker_position_id}
    def tracked(position):
        raw = position
        # Connector applies two normalizers, each preserving the original in raw.
        for _ in range(4):
            if not isinstance(raw, dict):
                break
            trade = raw.get('tradeData') or {}
            position_id = str(raw.get('position_id') or raw.get('positionId') or '')
            if (position_id in ids or trade.get('label') in refs
                    or raw.get('label') in refs or raw.get('clientOrderId') in refs):
                return True
            raw = raw.get('raw')
        return False
    return [position for position in positions if not tracked(position)]


def exclude_test_closed_deals(session_factory, account_id, deals):
    """Deals may have no reference: do not expose history before identity resolution."""
    return exclude_test_positions(session_factory, account_id, deals, closed_history=True)
