"""Real read-only auth on disposable PostgreSQL; diagnostics never dispatch."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib
import os
import time
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, event, text


def route():
    try:
        return importlib.import_module('routes.runtime_identity')
    except ModuleNotFoundError:
        pytest.fail('Authenticated runtime identity route missing')


@pytest.fixture
def auth_db():
    from services import user_auth_service as auth
    dsn = os.environ.get('VERIFIER_TEST_PG_DSN', '')
    assert 'host=/tmp/fs-verifier-pg.' in dsn and '/verifier_test?' in dsn
    root = create_engine(dsn)
    schema = 'identity_' + uuid4().hex
    with root.begin() as c:
        c.execute(text('CREATE SCHEMA ' + schema))
    engine = create_engine(dsn, connect_args={'options': '-csearch_path=' + schema})
    auth.metadata.create_all(engine)
    with engine.begin() as c:
        c.execute(text('CREATE TABLE flowsignal_owner_sessions (token_hash text primary key, expires_at timestamptz, last_seen_at timestamptz)'))
        for role in ('admin', 'user'):
            c.execute(auth.users.insert().values(id=role, email=role+'@example.invalid',
                full_name='Synthetic Person', password_hash='synthetic', role=role,
                is_active=True, email_verified=True, approval_status='APPROVED', created_at=1, updated_at=1))
            c.execute(auth.sessions.insert().values(token_hash=hashlib.sha256(role.encode()).hexdigest(),
                user_id=role, csrf_token='csrf', expires_at=time.time()+3600,
                created_at=1, last_seen_at=1, revoked_at=None))
        for token, hours in [('owner', 1), ('expired', -1)]:
            c.execute(text('INSERT INTO flowsignal_owner_sessions VALUES (:hash,:expiry,:seen)'),
                dict(hash=hashlib.sha256(token.encode()).hexdigest(),
                     expiry=datetime.now(timezone.utc)+timedelta(hours=hours),
                     seen=datetime(2020, 1, 1, tzinfo=timezone.utc)))
    yield engine
    engine.dispose()
    with root.begin() as c:
        c.execute(text('DROP SCHEMA ' + schema + ' CASCADE'))
    root.dispose()


def application(engine):
    from startup_recovery.observability import StartupRecorder
    api = SimpleNamespace(app=FastAPI(), _runtime_identity=StartupRecorder(),
        _recovery_runtime=None, LIVE_AUTO_TRADE_ENABLED={'enabled': False})
    route().install(api, engine=engine)
    return api


@pytest.mark.parametrize('headers,status', [
    ({}, 401), ({'Authorization': 'Bearer owner'}, 200),
    ({'Authorization': 'Bearer expired'}, 401), ({'Authorization': 'Bearer unknown'}, 401),
    ({'Authorization': 'FlowSignalUser admin'}, 403),
    ({'Authorization': 'FlowSignalUser admin', 'X-FlowSignal-CSRF': 'wrong'}, 403),
    ({'Authorization': 'FlowSignalUser admin', 'X-FlowSignal-CSRF': 'csrf'}, 200),
    ({'Authorization': 'FlowSignalUser user', 'X-FlowSignal-CSRF': 'csrf'}, 403),
    ({'Authorization': 'Basic owner'}, 401), ({'Authorization': 'Bearer two words'}, 401),
    ([('Authorization', 'Bearer owner'), ('Authorization', 'Bearer owner')], 401),
])
def test_authentication_csrf_and_no_store(auth_db, headers, status, monkeypatch):
    monkeypatch.delenv('STRATEGY_LIVE_INTEGRITY_VERIFY_ENABLED', raising=False)
    api = application(auth_db)
    response = TestClient(api.app).get('/admin/runtime-identity', headers=headers)
    assert response.status_code == status
    assert response.headers['cache-control'] == 'no-store'
    assert 'csrf' not in response.text and 'example.invalid' not in response.text
    if status == 200:
        assert response.json()['process']['pid'] == os.getpid()
        assert response.json()['authority_observation']['broker_mutation_authority'] == 'UNKNOWN'
        assert response.json()['authority_observation']['REAL_ORDER_DISPATCH_AVAILABLE'] is False


def test_modern_cookie_preserves_csrf(auth_db):
    client = TestClient(application(auth_db).app)
    client.cookies.set('flowsignal_session', 'admin')
    assert client.get('/admin/runtime-identity').status_code == 403
    assert client.get('/admin/runtime-identity', headers={'X-FlowSignal-CSRF': 'csrf'}).status_code == 200


@pytest.mark.parametrize('live,expected', [(False, False), (True, True), (None, 'UNKNOWN'), ('false', 'UNKNOWN'), (0, 'UNKNOWN')])
def test_live_observation_is_strict_not_coerced(auth_db, live, expected):
    api = application(auth_db)
    api.LIVE_AUTO_TRADE_ENABLED['enabled'] = live
    response = TestClient(api.app).get('/admin/runtime-identity', headers={'Authorization': 'Bearer owner'})
    observed = response.json()['runtime_observation']
    assert observed['live_auto_enabled'] == expected
    assert observed['runtime_token_present'] is False
    assert observed['checkpoint_producer_admitted'] is False
    assert observed['execution_active'] == 'UNKNOWN'
    assert api.LIVE_AUTO_TRADE_ENABLED['enabled'] is live


def test_repeated_reads_have_no_database_or_local_mutation(auth_db, monkeypatch):
    from startup_recovery import bootstrap, store
    from startup_recovery.readers import ReadAdapters
    from services import customer_forex_guard
    api = application(auth_db)
    def forbidden(*args, **kwargs):
        pytest.fail('diagnostic entered forbidden recovery/broker/session mutation')
    for module, name in [(bootstrap, 'start'), (store, 'begin_attempt'), (store, 'acquire_owner'),
                         (customer_forex_guard, 'validate_owner_session'),
                         (ReadAdapters, 'authenticate'), (ReadAdapters, 'broker')]:
        monkeypatch.setattr(module, name, forbidden)
    tables = ('flowsignal_users', 'flowsignal_sessions', 'flowsignal_owner_sessions')
    def data():
        with auth_db.connect() as c:
            return [c.execute(text('SELECT * FROM '+name+' ORDER BY 1')).all() for name in tables]
    before = data()
    recorded = api._runtime_identity.snapshot()
    queries = []
    def inspect_query(conn, cursor, statement, parameters, context, executemany):
        queries.append(statement)
        assert statement.startswith(('SELECT ', 'SET TRANSACTION READ ONLY', 'SET LOCAL ', 'SHOW '))
    event.listen(auth_db, 'before_cursor_execute', inspect_query)
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(pool.map(lambda _: TestClient(api.app).get('/admin/runtime-identity',
                headers={'Authorization': 'Bearer owner'}), range(12)))
    finally:
        event.remove(auth_db, 'before_cursor_execute', inspect_query)
    assert all(r.status_code == 200 for r in responses)
    assert all(r.json()['startup'] == responses[0].json()['startup'] for r in responses)
    assert queries.count('SET TRANSACTION READ ONLY') == 12
    assert before == data()
    assert api._runtime_identity.snapshot() == recorded
    assert api.LIVE_AUTO_TRADE_ENABLED == {'enabled': False}


def test_successful_history_never_conveys_current_broker_authority(auth_db):
    from startup_recovery.observability import observing, note
    api = application(auth_db)
    with observing(api._runtime_identity):
        note('acquisition_started')
        note('acquisition_committed')
        note('token_installed')
        note('producer_admitted')
        note('finished', ready=True, reason=None, phases=['NEW_ENTRIES_READY'])
    result = TestClient(api.app).get('/admin/runtime-identity', headers={'Authorization': 'Bearer owner'}).json()
    assert result['startup']['ready'] is True
    assert result['startup']['checkpoint_producer_admitted'] is True
    assert result['runtime_observation']['runtime_token_present'] is False
    assert result['authority_observation']['broker_mutation_authority'] == 'UNKNOWN'


def test_only_exact_diagnostic_get_bypasses_runtime_context(auth_db):
    from startup_recovery.asgi import RecoveryContextMiddleware
    from startup_recovery.types import RecoveryError
    api = application(auth_db)
    calls = []
    class Runtime:
        token = None
        writer = None
        def runtime_context(self):
            calls.append('context')
            from contextlib import contextmanager
            @contextmanager
            def context():
                raise RecoveryError('RECOVERY_TOKEN_INVALID')
                yield
            return context()
    api._recovery_runtime = Runtime()
    api.app.add_middleware(RecoveryContextMiddleware, runtime_api=api)
    client = TestClient(api.app, follow_redirects=False)
    result = client.get('/admin/runtime-identity', headers={'Authorization': 'Bearer owner'})
    assert result.status_code == 200
    assert calls == []
    assert client.get('/admin/runtime-identity').status_code == 401
    assert calls == []
    client.post('/admin/runtime-identity')
    client.get('/admin/runtime-identity/other')
    assert calls == ['context', 'context']


def test_database_failure_is_sanitized_and_no_store(auth_db, monkeypatch):
    api = application(auth_db)
    def fail():
        raise RuntimeError('postgresql://private-user:private-secret@host/db')
    monkeypatch.setattr(auth_db, 'connect', fail)
    response = TestClient(api.app).get('/admin/runtime-identity', headers={'Authorization': 'Bearer owner'})
    assert response.status_code == 503
    assert response.headers['cache-control'] == 'no-store'
    assert 'private' not in response.text


def test_actual_assembled_server_uses_diagnostic_without_recovery(auth_db, monkeypatch):
    # A fresh interpreter avoids altering the broad suite's assembled app/global
    # engine and proves this is the actual server assembly, not a test router.
    import subprocess
    import sys
    from urllib.parse import urlencode
    with auth_db.connect() as connection:
        schema = connection.execute(text('SELECT current_schema()')).scalar_one()
    env = dict(os.environ, DATABASE_URL=os.environ['VERIFIER_TEST_PG_DSN']+'&'+
               urlencode({'options': '-csearch_path='+schema}))
    code = '''
import os
from fastapi.testclient import TestClient
import closed_market_bootstrap
app = closed_market_bootstrap.create_app()
import api
class NoRuntimeWork:
    token = None
    writer = None
    def runtime_context(self):
        raise AssertionError('unexpected runtime authority evaluation')
api._recovery_runtime = NoRuntimeWork()
response = TestClient(app).get('/admin/runtime-identity', headers={'Authorization': 'Bearer owner'})
assert response.status_code == 200
assert response.json()['process']['pid'] == os.getpid()
'''
    result = subprocess.run([sys.executable, '-B', '-c', code], env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
