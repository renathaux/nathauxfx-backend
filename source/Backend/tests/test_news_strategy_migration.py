"""Disposable PostgreSQL receipt schema: no authority/data backfill."""
from alembic import command
from alembic.script import ScriptDirectory
import sqlalchemy as sa
import pytest

from test_legacy_adoption_migration import database, snapshot


def test_news_receipt_migration_preserves_all_existing_authority(database):
    import models
    assert hasattr(models,'RecoveryNewsStrategyCompatibility'), 'Durable compatibility model missing'
    engine,cfg=database
    command.upgrade(cfg,'20261010_0031')
    with engine.connect() as conn:
        before=snapshot(conn)
    command.upgrade(cfg,'20261016_0032')
    with engine.connect() as conn:
        after=snapshot(conn)
        assert after.pop('recovery_news_strategy_compatibility')==[]
        assert after==before
        assert conn.scalar(sa.text('SELECT version_num FROM alembic_version'))=='20261016_0032'
        inspector=sa.inspect(conn)
        columns={c['name'] for c in inspector.get_columns('recovery_news_strategy_compatibility')}
        assert columns==set(models.RecoveryNewsStrategyCompatibility.__table__.columns.keys())
        assert inspector.get_pk_constraint('recovery_news_strategy_compatibility')['constrained_columns']==['scope_key']
        assert {f['referred_table'] for f in inspector.get_foreign_keys('recovery_news_strategy_compatibility')}=={
            'recovery_accounts','recovery_attempts','saved_strategies'}
        source_type = next(c['type'] for c in inspector.get_columns('saved_strategies')
                           if c['name'] == 'strategy_id')
        for column in inspector.get_columns('recovery_news_strategy_compatibility'):
            if column['name'] in {'predecessor_strategy_id', 'successor_strategy_id'}:
                assert column['type'].length == source_type.length
    assert ScriptDirectory.from_config(cfg).get_heads()==['20261016_0032']
    command.downgrade(cfg,'20261010_0031')
    with engine.connect() as conn:
        assert snapshot(conn)==before


@pytest.mark.parametrize('change', [
    {'scope_key': 'missing'}, {'predecessor_attempt_id': 'missing'},
    {'predecessor_epoch': 0}, {'predecessor_epoch': -1},
    {'predecessor_strategy_id': 'missing'}, {'successor_strategy_id': 'missing'},
    {'successor_strategy_id': 'old'}, {'document': None}, {'receipt_digest': None},
])
def test_receipt_constraints_reject_invalid_insert_without_partial_row(database, change):
    engine, cfg = database
    command.upgrade(cfg, '20261016_0032')
    metadata = sa.MetaData()
    strategies = sa.Table('saved_strategies', metadata, autoload_with=engine)
    receipts = sa.Table('recovery_news_strategy_compatibility', metadata, autoload_with=engine)
    with engine.begin() as conn:
        for strategy_id in ('old', 'new'):
            conn.execute(strategies.insert().values(strategy_id=strategy_id, owner_id='customer',
                name=strategy_id, schema_version=1, definition_json={},
                created_at=sa.func.now(), updated_at=sa.func.now()))
    valid = dict(scope_key='scope', predecessor_attempt_id='attempt', predecessor_epoch=1,
        original_checkpoint_hash='a'*64, predecessor_strategy_id='old', successor_strategy_id='new',
        receipt_digest='b'*64, document={}, created_at=sa.func.now())
    with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
        conn.execute(receipts.insert().values(**{**valid, **change}))
    with engine.connect() as conn:
        assert conn.scalar(sa.select(sa.func.count()).select_from(receipts)) == 0
