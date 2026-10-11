"""Explicit PostgreSQL read capability for cutover; no application DB or ORM."""
import os

from sqlalchemy import Column, MetaData, Table, Text, cast, create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from live_integrity.read_store import read_only
from startup_recovery.checkpoints import _digest, _encoded, _parse
from startup_recovery.cutover_manifest import CutoverError, HeadSnapshot, require
from startup_recovery.cutover_validation import reference_rows


def _url():
    value = os.environ.get('CUTOVER_DATABASE_URL')
    require(bool(value), 'CUTOVER_DATABASE_URL_REQUIRED')
    try:
        url = make_url(value)
    except (ValueError, SQLAlchemyError):
        raise CutoverError('CUTOVER_DATABASE_URL_INVALID') from None
    require(url.get_backend_name() == 'postgresql', 'CUTOVER_POSTGRESQL_REQUIRED')
    return url


def configured_engine():
    """Only this explicit operator variable is accepted; never DATABASE_URL."""
    try:
        return create_engine(_url())
    except SQLAlchemyError:
        raise CutoverError('CUTOVER_DATABASE_CONFIGURATION_FAILED') from None


def read_heads(engine):
    require(engine.dialect.name == 'postgresql', 'CUTOVER_POSTGRESQL_REQUIRED')
    require(engine.url == _url(), 'CUTOVER_DATABASE_IDENTITY_MISMATCH')
    # Core declarations describe existing columns, never reflect/create/migrate.
    metadata = MetaData()
    head = Table('recovery_checkpoint_heads', metadata, *[Column(k) for k in
        ('scope_key','kind','manifest_hash','file_hash','generation','identity','admission_hash')])
    account = Table('recovery_accounts', metadata, *[Column(k) for k in
        ('scope_key','broker','environment','account_id')])
    query = select(*(head.c[k] for k in ('scope_key','kind','manifest_hash','file_hash','generation','admission_hash')),
        cast(head.c.identity, Text).label('identity'), account.c.broker,
        account.c.environment, account.c.account_id).select_from(
            head.outerjoin(account, head.c.scope_key == account.c.scope_key))
    # One statement snapshot captures ALL heads. Outer join exposes orphaned scope
    # references rather than silently losing them. JSON text preserves decimals.
    try:
        with read_only(engine) as connection:
            rows = []
            for row in connection.execute(query).mappings():
                item = dict(row)
                item['identity'] = _parse(item['identity'])
                rows.append(item)
            raw = _encoded(reference_rows(rows))
            return HeadSnapshot(raw, _digest(raw))
    except SQLAlchemyError:
        raise CutoverError('CUTOVER_DATABASE_READ_FAILED') from None
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise CutoverError('CUTOVER_REFERENCE_INVALID') from None
