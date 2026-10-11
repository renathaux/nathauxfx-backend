"""Task 3 required PostgreSQL and pure-validation gates; synthetic state only."""
import ast
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import FrozenInstanceError
from decimal import Decimal
import importlib
import os
from pathlib import Path
import subprocess
import sys

import pytest
from sqlalchemy import event, text, create_engine
from sqlalchemy.exc import DBAPIError

from tests.cutover_fixture import pg, generation, snapshot_db
from tests.test_cutover_files import snapshot
from startup_recovery.checkpoints import _encoded, _parse, _digest
from startup_recovery.cutover_manifest import HeadSnapshot, CutoverError


def api():
    return (importlib.import_module('startup_recovery.cutover_db'),
            importlib.import_module('startup_recovery.cutover_validation'))


def heads(refs):
    raw = _encoded(sorted(refs, key=lambda r: (r['scope_key'], r['kind'])))
    return HeadSnapshot(raw, _digest(raw))


@contextmanager
def select_only(engine):
    statements = []
    def check(conn, cursor, statement, parameters, context, executemany):
        sql = statement.lower().strip()
        assert sql.startswith(('select ', 'set transaction read only', 'set local ', 'show transaction_read_only')), sql
        assert not any(x in sql for x in ('for update','nextval','setval','advisory',' lock ','last_seen_at')), sql
        if sql.startswith('select '):
            cursor.execute('SHOW transaction_read_only')
            assert cursor.fetchone() == ('on',)
        statements.append(sql)
    event.listen(engine, 'before_cursor_execute', check)
    try:
        yield statements
    finally:
        event.remove(engine, 'before_cursor_execute', check)


def test_exact_all_scope_capture_read_only_deterministic(pg):
    db, validator = api()
    engine, schema, bundles = pg
    before = snapshot_db(engine, schema)
    with select_only(engine) as statements:
        result = db.read_heads(engine)
        assert db.read_heads(engine) == result
    expected = heads([r for b in bundles for r in b[2]])
    assert result == expected
    assert len(_parse(result.raw)) == 3
    assert len({r['scope_key'] for r in _parse(result.raw)}) == 2
    validated = []
    for raw, payloads, _ in bundles:
        validated.extend(validator.validate_generation(raw, payloads, _digest(raw), result))
    assert sorted(validated) == sorted(r['scope_key']+':'+r['kind'] for r in _parse(result.raw))
    assert any(x == 'show transaction_read_only' for x in statements)
    with pytest.raises(FrozenInstanceError):
        result.raw = b'[]'
    assert snapshot_db(engine, schema) == before


@pytest.mark.parametrize('sql', [
    'INSERT INTO {s}.auth_canary(last_seen_at) VALUES (now())',
    'UPDATE {s}.auth_canary SET last_seen_at=now()',
    'DELETE FROM {s}.auth_canary',
    'CREATE TABLE {s}.forbidden(id int)',
    'ALTER TABLE {s}.auth_canary ADD COLUMN forbidden int',
    'DROP TABLE {s}.auth_canary',
    "SELECT nextval('{s}.auth_canary_id_seq')",
    "SELECT setval('{s}.auth_canary_id_seq', 999)",
])
def test_postgres_rejects_all_mutations(pg, monkeypatch, sql):
    db, _ = api()
    engine, schema, _ = pg
    before = snapshot_db(engine, schema)
    original = db.read_only
    @contextmanager
    def attempted_write(e):
        with original(e) as c:
            assert c.execute(text('SHOW transaction_read_only')).scalar_one() == 'on'
            c.execute(text(sql.format(s=schema)))
            yield c
    monkeypatch.setattr(db, 'read_only', attempted_write)
    with pytest.raises(CutoverError, match='CUTOVER_DATABASE_READ_FAILED'):
        db.read_heads(engine)
    assert snapshot_db(engine, schema) == before
    # Independently assert the actual PG error, not only our sanitized wrapper.
    with pytest.raises(DBAPIError) as err:
        with original(engine) as c:
            c.execute(text(sql.format(s=schema)))
    assert err.value.orig.pgcode == '25006'
    assert snapshot_db(engine, schema) == before


def test_explicit_database_only_no_fallback(monkeypatch):
    db, _ = api()
    monkeypatch.delenv('CUTOVER_DATABASE_URL', raising=False)
    monkeypatch.setenv('DATABASE_URL','postgresql://unreachable.invalid/production')
    with pytest.raises(CutoverError, match='CUTOVER_DATABASE_URL_REQUIRED'):
        db.configured_engine()
    monkeypatch.setenv('CUTOVER_DATABASE_URL','sqlite:///:memory:')
    with pytest.raises(CutoverError, match='CUTOVER_POSTGRESQL_REQUIRED'):
        db.configured_engine()
    with pytest.raises(CutoverError, match='CUTOVER_POSTGRESQL_REQUIRED'):
        db.read_heads(create_engine('sqlite:///:memory:'))


def test_database_identity_mismatch_never_connects(pg, monkeypatch):
    db, _ = api()
    engine, schema, _ = pg
    before = snapshot_db(engine, schema)
    monkeypatch.setenv('CUTOVER_DATABASE_URL','postgresql://unreachable.invalid/not-approved')
    with pytest.raises(CutoverError, match='CUTOVER_DATABASE_IDENTITY_MISMATCH'):
        db.read_heads(engine)
    assert snapshot_db(engine,schema) == before


def test_database_json_cast_preserves_exact_decimal_representation(pg):
    db, _ = api()
    engine,schema,bundles = pg
    identity = dict(bundles[0][2][0]['identity'],position_id=Decimal('9007199254740993.0000000000000001'))
    with engine.begin() as c:
        c.execute(text(f'UPDATE {schema}.recovery_checkpoint_heads SET identity=CAST(:identity AS JSON) WHERE kind=:kind'),
            {'identity':_encoded(identity).decode(), 'kind':'live_backup'})
    before = snapshot_db(engine,schema)
    with select_only(engine): result = db.read_heads(engine)
    ref = next(r for r in _parse(result.raw) if r['kind']=='live_backup')
    assert ref['identity']['position_id'] == Decimal('9007199254740993.0000000000000001')
    assert b'9007199254740993.0000000000000001' in result.raw
    assert snapshot_db(engine,schema) == before


def test_orphan_head_fails_instead_of_disappearing_from_join(pg):
    db, _ = api()
    engine,schema,_ = pg
    with engine.begin() as c:
        constraint = c.execute(text('SELECT constraint_name FROM information_schema.table_constraints WHERE table_schema=:s AND table_name=\'recovery_checkpoint_heads\' AND constraint_type=\'FOREIGN KEY\''),{'s':schema}).scalar_one()
        c.execute(text(f'ALTER TABLE {schema}.recovery_checkpoint_heads DROP CONSTRAINT "{constraint}"'))
        c.execute(text(f"UPDATE {schema}.recovery_checkpoint_heads SET scope_key='orphan' WHERE kind='app_settings'"))
    before = snapshot_db(engine,schema)
    with select_only(engine), pytest.raises(CutoverError): db.read_heads(engine)
    assert snapshot_db(engine,schema) == before


@pytest.mark.parametrize('field,value', [('kind','unknown'),('identity',{}),('admission_hash',''),('file_hash','x'),('generation',0)])
def test_invalid_database_reference_blocks_without_mutation(pg, field, value):
    db, _ = api()
    from models import RecoveryCheckpointHead
    engine, schema, _ = pg
    with engine.begin() as c:
        c.execute(RecoveryCheckpointHead.__table__.update().where(RecoveryCheckpointHead.kind=='app_settings').values(**{field:value}))
    before = snapshot_db(engine, schema)
    with select_only(engine), pytest.raises(CutoverError):
        db.read_heads(engine)
    assert snapshot_db(engine, schema) == before


def test_missing_schema_blocks_not_empty_success(pg):
    db, _ = api()
    engine, schema, _ = pg
    with engine.begin() as c:
        c.execute(text(f'DROP TABLE {schema}.recovery_checkpoint_heads'))
    before = snapshot_db(engine, schema)
    with pytest.raises(CutoverError, match='CUTOVER_DATABASE_READ_FAILED'):
        db.read_heads(engine)
    assert snapshot_db(engine, schema) == before


CASES = ['valid', 'missing_payload', 'manifest_hash', 'file_hash', 'payload_hash',
    'kind', 'identity', 'generation', 'admission', 'duplicate_reference',
    'conflicting_reference', 'rejected', 'incomplete_reference', 'unmapped_kind',
    'parent', 'bad_json', 'legacy', 'snapshot_hash', 'unlisted_payload',
    'manifest_schema', 'empty_manifest', 'scope_mismatch', 'nonfinite',
    'ref_file_hash','ref_generation','ref_admission','bool_epoch','duplicate_json_key',
    'missing_manifest_payload','missing_identity_field']


@pytest.mark.parametrize('case', CASES)
def test_pure_generation_and_every_failure_preserve_pg_and_files(pg, tmp_path, monkeypatch, case):
    db, validator = api()
    engine, schema, bundles = pg
    raw, payloads, refs = deepcopy(bundles[0])
    # Snapshot both source and destination including all timestamps; O_NOATIME observer.
    for directory in ('source','destination'):
        root = tmp_path / directory
        root.mkdir(mode=0o700)
        (root/'manifest.json').write_bytes(raw)
        for kind, value in payloads.items():
            (root/(kind+'.json')).write_bytes(value)
    files_before = snapshot(tmp_path)
    db_before = snapshot_db(engine, schema)
    state = db.read_heads(engine)
    body = _parse(payloads['live_backup'])
    if case == 'missing_payload': payloads.pop('live_backup')
    elif case == 'manifest_hash': raw += b' '
    elif case == 'file_hash': payloads['live_backup'] += b' '
    elif case == 'payload_hash': body['payload_hash'] = '0'*64
    elif case == 'kind': body['kind'] = 'paper_backup'
    elif case == 'identity': body['identity']['owner_id'] = 'foreign'
    elif case == 'generation': body['identity']['generation'] = 2
    elif case == 'admission': body['admission_hash'] = '0'*64
    elif case == 'parent': body['parent_hash'] = '0'*64
    elif case == 'legacy': body.pop('checkpoint_schema')
    elif case == 'unmapped_kind': refs[0]['kind'] = 'unknown'
    elif case == 'scope_mismatch': refs[0]['account_id'] = 'foreign'
    elif case == 'ref_file_hash': refs[0]['file_hash'] = '0'*64
    elif case == 'ref_generation': refs[0]['generation'] = 2
    elif case == 'ref_admission': refs[0]['admission_hash'] = '0'*64
    elif case == 'bool_epoch': body['identity']['epoch'] = True
    elif case == 'missing_identity_field': body['identity'].pop('build_id')
    elif case == 'missing_manifest_payload':
        manifest = _parse(raw)
        manifest['files']['visits'] = '0'*64
        raw = _encoded(manifest)
        for r in refs: r['manifest_hash'] = _digest(raw)
    elif case == 'incomplete_reference': refs[0].pop('admission_hash')
    elif case in ('duplicate_reference','conflicting_reference'):
        refs.append(deepcopy(refs[0]))
        if case == 'conflicting_reference': refs[-1]['file_hash'] = '0'*64
    elif case == 'unlisted_payload': payloads['visits'] = b'[]'
    elif case == 'rejected':
        from startup_recovery.checkpoints import RejectedCheckpoint
        monkeypatch.setattr(validator,'validate_checkpoint',lambda *a, **k: RejectedCheckpoint('synthetic-rejection'))
    semantic = {'payload_hash','kind','identity','generation','admission','parent','legacy','bad_json','nonfinite','bool_epoch','missing_identity_field','duplicate_json_key'}
    if case in semantic:
        payloads['live_backup'] = (b'{' if case == 'bad_json' else b'{"n":NaN}' if case == 'nonfinite' else _encoded(body))
        if case == 'duplicate_json_key': payloads['live_backup'] = b'{"kind":"live_backup","kind":"live_backup"}'
        manifest = _parse(raw)
        manifest['files']['live_backup'] = _digest(payloads['live_backup'])
        raw = _encoded(manifest)
        for r in refs:
            r['manifest_hash'] = _digest(raw)
            r['file_hash'] = manifest['files'][r['kind']]
    if case in ('manifest_schema','empty_manifest'):
        manifest = _parse(raw)
        if case == 'manifest_schema': manifest['manifest_schema'] = 2
        else: manifest['files'] = {}
        raw = _encoded(manifest)
        for r in refs: r['manifest_hash'] = _digest(raw)
    state = heads(refs + bundles[1][2])
    if case == 'snapshot_hash': state = HeadSnapshot(state.raw, '0'*64)
    expected_hash = refs[0]['manifest_hash']
    # Pure boundary cannot open files, DB connections or call any checkpoint writer.
    import builtins
    from startup_recovery import checkpoints
    def forbidden(*a, **k): raise AssertionError('Forbidden IO/writer in pure validation')
    with monkeypatch.context() as m:
        m.setattr(builtins, 'open', forbidden)
        m.setattr(os, 'open', forbidden)
        m.setattr(type(engine), 'connect', forbidden)
        for name in ('write_generation','produced_envelope','read_candidate','read_committed','checked_envelope'):
            m.setattr(checkpoints, name, forbidden)
        if case == 'valid':
            keys = validator.validate_generation(raw, payloads, expected_hash, state)
            assert keys == tuple(sorted(r['scope_key']+':'+r['kind'] for r in refs))
            assert len(keys) == 2  # shared generation, both references retained
        else:
            with pytest.raises(CutoverError):
                validator.validate_generation(raw, payloads, expected_hash, state)
    assert snapshot_db(engine, schema) == db_before
    assert snapshot(tmp_path) == files_before


def test_shared_bytes_do_not_drop_different_scope_reference(pg):
    db, validator = api()
    from models import RecoveryCheckpointHead
    engine, schema, bundles = pg
    raw, payloads, refs = bundles[0]
    foreign = dict(refs[0], scope_key=bundles[1][2][0]['scope_key'])
    with engine.begin() as c:
        c.execute(RecoveryCheckpointHead.__table__.insert().values(**{k:v for k,v in foreign.items() if k not in ('broker','environment','account_id')}))
    before = snapshot_db(engine,schema)
    # Conflicting account identity must block, not silently discard that scope.
    with pytest.raises(CutoverError): db.read_heads(engine)
    assert snapshot_db(engine,schema) == before


def test_no_writable_dependency_imports_or_calls():
    db, validator = api()
    allowed = {'os','datetime','sqlalchemy','sqlalchemy.engine','sqlalchemy.exc',
        'live_integrity.read_store','startup_recovery.cutover_manifest',
        'startup_recovery.cutover_validation','startup_recovery.checkpoints'}
    forbidden = {'write_generation','publish','produced_envelope','repair','acquire_owner',
        'reconcile','read_runtime','RuntimeWriter','SessionLocal','Session','single_flight','checked_envelope'}
    for module in (db,validator):
        tree = ast.parse(Path(module.__file__).read_text())
        for node in ast.walk(tree):
            if isinstance(node,ast.Import): assert all(n.name in allowed for n in node.names)
            if isinstance(node,ast.ImportFrom):
                assert node.module in allowed
                assert not any(n.name in forbidden for n in node.names)
            if isinstance(node,ast.Call):
                assert getattr(node.func,'id',getattr(node.func,'attr','')) not in forbidden
    code = '''
import sys, importlib.abc
class Guard(importlib.abc.MetaPathFinder):
 def find_spec(self, fullname, path=None, target=None):
  if fullname in {'db','models','api','ctrader_connector','startup_recovery.store','startup_recovery.checkpoint_store','startup_recovery.coordinator','startup_recovery.publication'}:
   raise AssertionError(fullname)
sys.meta_path.insert(0,Guard())
import startup_recovery.cutover_db, startup_recovery.cutover_validation
'''
    subprocess.run([sys.executable,'-B','-c',code],check=True)
