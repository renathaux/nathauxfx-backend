"""Receipt migration gates against an explicitly disposable PostgreSQL database."""
import importlib.util
import os
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

REVISION = '20261010_0031'
TABLE = 'recovery_legacy_adoptions'
MIGRATION = Path('migrations/versions/20261010_0031_legacy_checkpoint_adoption.py')


@pytest.fixture
def database(monkeypatch):
    dsn = os.environ.get('VERIFIER_TEST_PG_DSN', '')
    assert 'host=/tmp/fs-verifier-pg.' in dsn and '/verifier_test?' in dsn
    engine = sa.create_engine(dsn)
    schema = 'adoption_migration_' + uuid4().hex
    with engine.begin() as conn:
        conn.execute(sa.text('CREATE SCHEMA ' + schema))
    url = sa.engine.make_url(dsn).update_query_dict({'options': '-csearch_path=' + schema})
    scoped = sa.create_engine(url)
    monkeypatch.setenv('MIGRATION_DATABASE_URL', url.render_as_string(hide_password=False))
    cfg = Config('alembic.ini')
    try:
        command.upgrade(cfg, '20261001_0030')
        with scoped.begin() as conn:
            conn.execute(sa.text("INSERT INTO recovery_accounts (scope_key,broker,environment,account_id,allocated_epoch,phase) VALUES ('scope','ctrader','demo','123',1,'STATE_DISCOVERED')"))
            conn.execute(sa.text("INSERT INTO recovery_attempts (attempt_id,scope_key,epoch,boot_id,build_id,phase,created_at) VALUES ('attempt','scope',1,'boot','build','STATE_DISCOVERED',CURRENT_TIMESTAMP)"))
        yield scoped, cfg
    finally:
        scoped.dispose()
        with engine.begin() as conn:
            conn.execute(sa.text('DROP SCHEMA ' + schema + ' CASCADE'))
        engine.dispose()


def require_migration():
    assert MIGRATION.is_file(), '0031 receipt migration is missing'


def snapshot(conn):
    return {table: conn.execute(sa.text('SELECT * FROM ' + table)).all()
            for table in sa.inspect(conn).get_table_names() if table != 'alembic_version'}


def test_upgrade_only_adds_empty_receipt_table_and_preserves_existing_rows(database):
    engine, cfg = database
    require_migration()
    with engine.connect() as conn:
        before = snapshot(conn)
    command.upgrade(cfg, REVISION)
    with engine.connect() as conn:
        after = snapshot(conn)
        assert set(after) - set(before) == {TABLE}
        assert after.pop(TABLE) == []
        assert after == before
        assert conn.scalar(sa.text('SELECT version_num FROM alembic_version')) == REVISION
    scripts = ScriptDirectory.from_config(cfg)
    assert scripts.get_heads() == ['20261016_0032']
    assert scripts.get_revision('20261016_0032').down_revision == REVISION
    import models
    assert hasattr(models, 'RecoveryLegacyAdoption'), 'Receipt ORM model is missing'
    model = models.RecoveryLegacyAdoption.__table__
    assert {c['name'] for c in sa.inspect(engine).get_columns(TABLE)} == set(model.columns.keys())
    command.downgrade(cfg, '20261001_0030')
    with engine.connect() as conn:
        assert snapshot(conn) == before


def receipt(**overrides):
    result = dict(scope_key='scope', cutover_manifest_hash='a'*64,
                  attempt_id='attempt', epoch=1, boot_id='boot', build_id='build',
                  handoff_evidence_hash='b'*64, adoption_policy_version='legacy-adoption-v1',
                  source_hashes={}, dependency_hashes={}, generation_manifest_hash='c'*64,
                  envelope_hashes={}, receipt_digest='d'*64)
    result.update(overrides)
    return result


@pytest.mark.parametrize('invalid', [
    {}, {'scope_key': 'unknown'}, {'attempt_id': 'unknown'}, {'epoch': 0},
    {'epoch': -1}, {'receipt_digest': None}, {'source_hashes': None},
    {'dependency_hashes': None}, {'envelope_hashes': None},
])
def test_receipt_identity_and_evidence_constraints(database, invalid):
    engine, cfg = database
    require_migration()
    command.upgrade(cfg, REVISION)
    table = sa.Table(TABLE, sa.MetaData(), autoload_with=engine)
    with engine.begin() as conn:
        conn.execute(table.insert().values(**receipt(), created_at=sa.func.now()))
    if invalid:
        invalid = {'cutover_manifest_hash': 'e'*64, **invalid}
    with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
        conn.execute(table.insert().values(**receipt(**invalid), created_at=sa.func.now()))


def test_receipt_ddl_rolls_back_on_transaction_failure(database):
    engine, _ = database
    require_migration()
    spec = importlib.util.spec_from_file_location('receipt_migration', MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.down_revision == '20261001_0030'
    with pytest.raises(RuntimeError, match='injected'), engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            module.upgrade()
            assert sa.inspect(conn).has_table(TABLE)
            raise RuntimeError('injected transaction failure')
    assert not sa.inspect(engine).has_table(TABLE)
