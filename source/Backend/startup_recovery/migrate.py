"""Explicit release migration command, never an application-import side effect."""
from pathlib import Path
from sqlalchemy import text
from startup_recovery.types import RecoveryError

# Separate two-integer namespace: FSRC/release-schema. Not an account,
# position-manager, trading-operation or diagnostic single-flight lock.
MIGRATION_LOCK = (1179865667, 1)


def serialized_migration(engine, migrate):
    if engine.dialect.name != 'postgresql':
        raise RecoveryError('RECOVERY_MIGRATION_DATABASE_UNSUPPORTED')
    with engine.connect() as connection:
        acquired = connection.execute(text('SELECT pg_try_advisory_lock(:namespace,:key)'),
            dict(namespace=MIGRATION_LOCK[0], key=MIGRATION_LOCK[1])).scalar_one()
        if not acquired:
            raise RecoveryError('RECOVERY_MIGRATION_BUSY')
        try:
            migrate()
        finally:
            connection.execute(text('SELECT pg_advisory_unlock(:namespace,:key)'),
                dict(namespace=MIGRATION_LOCK[0], key=MIGRATION_LOCK[1]))


def main():
    import os
    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool
    from alembic.config import Config
    from alembic import command
    from db import normalize_database_url
    raw = os.getenv('MIGRATION_DATABASE_URL') or os.getenv('DATABASE_URL')
    if not raw:
        raise RecoveryError('RECOVERY_MIGRATION_DATABASE_UNAVAILABLE')
    engine = create_engine(normalize_database_url(raw), poolclass=NullPool)
    config = Config(str(Path(__file__).resolve().parents[1] / 'alembic.ini'))
    try:
        serialized_migration(engine, lambda: command.upgrade(config, 'head'))
    finally:
        engine.dispose()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # A DB driver exception can contain connection details. No traceback or
        # URL is emitted by this wrapper. Release tooling receives failure only.
        raise SystemExit('RECOVERY_RELEASE_MIGRATION_FAILED') from None
