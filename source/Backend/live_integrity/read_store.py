"""PostgreSQL-enforced diagnostic read capability; never imports application DB."""
from contextlib import contextmanager
from sqlalchemy import text
from sqlalchemy.orm import Session

LOCK_NAMESPACE = 1179863382  # 'FSIV', separate two-int diagnostic namespace
LOCK_KEY = 1


class Busy(ValueError):
    pass


@contextmanager
def read_only(engine):
    if engine.dialect.name != 'postgresql':
        raise ValueError('DIAGNOSTIC_POSTGRESQL_REQUIRED')
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(text('SET TRANSACTION READ ONLY'))
            connection.execute(text("SET LOCAL statement_timeout = '1500ms'"))
            connection.execute(text("SET LOCAL lock_timeout = '100ms'"))
            connection.execute(text("SET LOCAL idle_in_transaction_session_timeout = '12s'"))
            if connection.execute(text('SHOW transaction_read_only')).scalar_one() != 'on':
                raise ValueError('DIAGNOSTIC_READ_ONLY_UNAVAILABLE')
            yield connection
        finally:
            if transaction.is_active:
                transaction.rollback()


@contextmanager
def read_only_session(engine):
    with read_only(engine) as connection:
        with Session(bind=connection,autoflush=False,expire_on_commit=False) as session:
            yield session


@contextmanager
def single_flight(connection):
    """Transaction-scoped try-lock: no queue, retries, or persistent DB writes.

    The owning read_only transaction MUST outlive all verifier work. Process
    death closes its connection and releases the same PostgreSQL lock.
    """
    acquired = connection.execute(text('SELECT pg_try_advisory_xact_lock(:namespace,:key)'),
        dict(namespace=LOCK_NAMESPACE,key=LOCK_KEY)).scalar_one()
    if acquired is not True:
        raise Busy('DIAGNOSTIC_BUSY')
    yield
