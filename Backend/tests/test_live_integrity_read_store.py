"""Hard-gate tests: disposable PostgreSQL only, never hosted credentials."""
import importlib
import os
import time

import pytest
from sqlalchemy import create_engine, text, Column, Integer
from sqlalchemy.orm import declarative_base
from sqlalchemy.exc import DBAPIError


@pytest.fixture
def pg():
    dsn = os.getenv('VERIFIER_TEST_PG_DSN')
    if not dsn:
        pytest.skip('Hard gate must also run with explicit disposable Unix-socket PostgreSQL')
    assert 'host=/tmp/fs-verifier-pg.' in dsn and '/verifier_test?' in dsn, 'Disposable local database only'
    engine = create_engine(dsn)
    with engine.begin() as c:
        c.execute(text('CREATE TABLE IF NOT EXISTS verifier_proof (id integer primary key)'))
        c.execute(text('INSERT INTO verifier_proof VALUES (1) ON CONFLICT DO NOTHING'))
    yield engine
    engine.dispose()


def store():
    try:
        return importlib.import_module('live_integrity.read_store')
    except ModuleNotFoundError:
        pytest.fail('Read-only DB capability missing')


def test_postgresql_read_only_rejects_all_dml_and_autoflush(pg):
    s = store()
    for statement in ('INSERT INTO verifier_proof VALUES (2)', 'UPDATE verifier_proof SET id=2', 'DELETE FROM verifier_proof'):
        with s.read_only(pg) as c:
            assert c.execute(text('SHOW transaction_read_only')).scalar_one() == 'on'
            assert c.execute(text('SELECT id FROM verifier_proof')).scalar_one() == 1
            with pytest.raises(DBAPIError) as caught:
                c.execute(text(statement))
            assert caught.value.orig.pgcode == '25006'
    Base = declarative_base()
    class Proof(Base):
        __tablename__ = 'verifier_proof'
        id = Column(Integer, primary_key=True)
    with s.read_only_session(pg) as session:
        session.add(Proof(id=3))
        assert session.query(Proof).count() == 1
        with pytest.raises(DBAPIError) as caught:
            session.flush()
        assert caught.value.orig.pgcode == '25006'


@pytest.mark.parametrize('outcome',['success','exception','timeout'])
def test_two_independent_sessions_singleflight_and_release(pg,outcome):
    s = store()
    class Injected(Exception): pass
    try:
        with s.read_only(pg) as first:
            with s.single_flight(first):
                with s.read_only(pg) as second:
                    start = time.monotonic()
                    with pytest.raises(s.Busy):
                        with s.single_flight(second):
                            pytest.fail('Second verifier entered')
                    assert time.monotonic()-start < .5
                    # Unrelated execution namespace remains freely acquirable.
                    assert second.execute(text('SELECT pg_try_advisory_xact_lock(17, 23)')).scalar_one()
                if outcome != 'success':
                    raise Injected(outcome)
    except Injected: pass
    with s.read_only(pg) as third:
        with s.single_flight(third):
            assert third.execute(text('SELECT 1')).scalar_one() == 1


def test_db_admin_read_does_not_touch_last_seen_or_create_schema(pg):
    from services import user_auth_service as normal
    from live_integrity.auth import read_admin,AccessDenied
    import hashlib
    normal.metadata.create_all(pg)  # fixture setup ONLY, never inside verifier
    token_hash=hashlib.sha256(b'verifier-fixture-token').hexdigest()
    with pg.begin() as c:
        c.execute(text("DELETE FROM flowsignal_sessions WHERE token_hash=:h"),{'h':token_hash})
        c.execute(text("DELETE FROM flowsignal_users WHERE id='verifier-admin'"))
        c.execute(normal.users.insert().values(id='verifier-admin',email='fixture@example.invalid',full_name='Fixture',password_hash='not-a-password',role='admin',is_active=True,email_verified=True,approval_status='APPROVED',created_at=1,updated_at=1))
        c.execute(normal.sessions.insert().values(token_hash=token_hash,user_id='verifier-admin',csrf_token='fixture-csrf',created_at=1,expires_at=time.time()+3600,last_seen_at=123,revoked_at=None))
    with store().read_only(pg) as c:
        assert read_admin(c,'verifier-fixture-token','fixture-csrf')=='owner:fixture@example.invalid'
        assert c.execute(text('SELECT last_seen_at FROM flowsignal_sessions WHERE token_hash=:h'),{'h':token_hash}).scalar_one()==123
        with pytest.raises(AccessDenied):
            read_admin(c,'verifier-fixture-token','wrong')
    with pg.begin() as c:
        c.execute(text("UPDATE flowsignal_users SET role='user' WHERE id='verifier-admin'"))
    with store().read_only(pg) as c:
        with pytest.raises(AccessDenied) as error:
            read_admin(c,'verifier-fixture-token','fixture-csrf')
        assert error.value.status==403


def test_real_worker_death_releases_global_lock(pg,tmp_path):
    import subprocess,sys
    script=tmp_path/'lock_worker.py'
    script.write_text('''import sys,time
from sqlalchemy import create_engine
sys.path.insert(0,sys.argv[2])
from live_integrity.read_store import read_only,single_flight
with read_only(create_engine(sys.argv[1])) as c:
    with single_flight(c):
        print('ACQUIRED',flush=True)
        time.sleep(60)
''')
    from pathlib import Path
    process=subprocess.Popen([sys.executable,str(script),os.environ['VERIFIER_TEST_PG_DSN'],str(Path.cwd())],stdout=subprocess.PIPE,text=True)
    try:
        assert process.stdout.readline().strip()=='ACQUIRED'
        with store().read_only(pg) as second:
            with pytest.raises(store().Busy):
                with store().single_flight(second): pass
        process.kill(); process.wait(timeout=2)
        deadline=time.monotonic()+2
        while True:
            try:
                with store().read_only(pg) as third:
                    with store().single_flight(third): break
            except store().Busy:
                assert time.monotonic()<deadline
                time.sleep(.01)
    finally:
        if process.poll() is None:
            process.kill();process.wait(timeout=2)
