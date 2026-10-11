from unittest.mock import Mock
import pytest
from test_recovery_store import store_api, db
from startup_recovery.types import RecoveryError


def test_release_migration_has_separate_nonqueued_cross_session_lock(db):
    from startup_recovery.migrate import serialized_migration
    engine = db.kw['bind']
    second = Mock()
    def first():
        with pytest.raises(RecoveryError, match='RECOVERY_MIGRATION_BUSY'):
            serialized_migration(engine, second)
        second.assert_not_called()
    serialized_migration(engine, first)
    serialized_migration(engine, second)
    second.assert_called_once()


def test_release_migration_lock_released_on_failure(db):
    from startup_recovery.migrate import serialized_migration
    def fail(): raise RuntimeError('migration failed')
    with pytest.raises(RuntimeError):
        serialized_migration(db.kw['bind'], fail)
    next_migration = Mock()
    serialized_migration(db.kw['bind'], next_migration)
    next_migration.assert_called_once()
