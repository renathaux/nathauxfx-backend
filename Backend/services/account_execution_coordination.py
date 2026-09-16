"""Nonblocking account coordination; durable fences have no timeout or TTL."""
from contextlib import contextmanager
from contextvars import ContextVar
import fcntl
import hashlib
import os
import tempfile

from sqlalchemy import select, text
from models import BrokerIntegrationTestSubmission

TEST_ACCOUNT = '47784297'
_execution_account = ContextVar('normal_execution_account', default=None)


class ExecutionFenced(RuntimeError):
    pass


@contextmanager
def account_lock(session_factory, account_id):
    if str(account_id) != TEST_ACCOUNT:
        yield
        return
    with session_factory() as session:
        engine = session.get_bind()
    if engine.dialect.name == 'postgresql':
        # Session-level advisory locks are unsafe behind transaction poolers.
        # A dedicated connection holds a transaction lock across all broker work.
        with engine.connect() as connection, connection.begin():
            acquired = connection.execute(text('SELECT pg_try_advisory_xact_lock(:key)'),
                {'key': 477842970022}).scalar()
            if not acquired:
                raise ExecutionFenced('DEMO broker test account busy')
            yield
    elif engine.dialect.name == 'sqlite':
        identity = hashlib.sha256(str(engine.url).encode()).hexdigest()
        path = os.path.join(tempfile.gettempdir(), f'flowsignal-demo-{identity}.lock')
        with open(path, 'a') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ExecutionFenced('DEMO broker test account busy') from exc
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
    if str(account_id) != TEST_ACCOUNT:
        return callback()
    with account_lock(session_factory, account_id):
        with session_factory() as session:
            pending = session.scalar(select(BrokerIntegrationTestSubmission.test_id).where(
                BrokerIntegrationTestSubmission.unresolved_account == TEST_ACCOUNT))
            if pending:
                raise ExecutionFenced('Unresolved DEMO broker integration test')
        return callback()


def exclude_test_positions(session_factory, account_id, positions):
    """Exclude only durable exact identity matches, including terminal tests."""
    if str(account_id) != TEST_ACCOUNT:
        return positions
    with session_factory() as session:
        rows = session.scalars(select(BrokerIntegrationTestSubmission).where(
            BrokerIntegrationTestSubmission.account_id == TEST_ACCOUNT)).all()
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
