"""Header-only durable owner auth; synthetic sessions, never hosted credentials."""
from datetime import datetime, timedelta, timezone
import hashlib
import pytest
from test_live_integrity_read_store import pg
from fastapi import FastAPI
from fastapi.testclient import TestClient


def request(monkeypatch, headers=None, cookies=None, extra=None, query=''):
    from routes import strategy_live_integrity as route
    monkeypatch.setenv('STRATEGY_LIVE_INTEGRITY_VERIFY_ENABLED', '1')
    calls = []
    def child(payload):
        calls.append(payload)
        return {'status': 200, 'body': {'decision': 'WOULD_BLOCK',
            'REAL_ORDER_DISPATCH_AVAILABLE': False, 'block_reasons': ['WAIT_STUDIO_EVALUATOR']}}
    monkeypatch.setattr(route, 'run', child)
    app = FastAPI(); app.include_router(route.router)
    with TestClient(app, cookies=cookies or {}) as client:
        result = client.post('/admin/strategy-live-integrity/verify'+query,
            headers=headers or {}, json=dict(strategy_id='saved', symbol='XAUUSD', **(extra or {})))
    return result, calls


def test_legacy_header_enters_same_worker_without_csrf_and_does_not_allow(monkeypatch):
    result, calls = request(monkeypatch, {'Authorization': 'Bearer synthetic-owner'})
    assert result.status_code == 200
    assert result.json()['decision'] == 'WOULD_BLOCK'
    assert result.json()['REAL_ORDER_DISPATCH_AVAILABLE'] is False
    assert calls[0]['auth_scheme'] == 'legacy-owner'
    assert calls[0]['token'] == 'synthetic-owner'
    assert calls[0]['csrf'] == ''
    assert set(calls[0]) == {'auth_scheme','token','csrf','strategy_id','symbol','runtime','deadline'}
    assert 'synthetic-owner' not in result.text


@pytest.mark.parametrize('header', ['Bearer', 'Bearer ', 'Bearer a b', 'bearer token',
    'Basic token', 'FlowSignalUser', 'FlowSignalUser ', 'Bearer token\ttwo'])
def test_malformed_header_cannot_fall_back_to_modern_cookie(monkeypatch, header):
    result, calls = request(monkeypatch, {'Authorization': header, 'X-FlowSignal-CSRF': 'csrf'},
        {'flowsignal_session': 'valid-modern'})
    assert result.status_code in (401,403)
    assert calls == []


@pytest.mark.parametrize('transport', ['missing','cookie','query','body','custom'])
def test_legacy_token_only_other_transport_never_selects_legacy_auth(monkeypatch, transport):
    result, calls = request(monkeypatch,
        {'X-Session-Token': 'owner'} if transport == 'custom' else None,
        {'owner_session': 'owner'} if transport == 'cookie' else None,
        {'token': 'owner'} if transport == 'body' else None,
        '?token=owner' if transport == 'query' else '')
    assert result.status_code in (401,422)
    assert not calls


@pytest.mark.parametrize('cookie', [False, True])
def test_modern_auth_stays_modern_and_requires_csrf(monkeypatch, cookie):
    headers = {} if cookie else {'Authorization': 'FlowSignalUser modern'}
    cookies = {'flowsignal_session':'modern'} if cookie else None
    result, calls = request(monkeypatch, headers, cookies)
    assert result.status_code == 403 and not calls
    result, calls = request(monkeypatch, dict(headers, **{'X-FlowSignal-CSRF':'csrf'}), cookies)
    assert result.status_code == 200
    assert calls[0]['auth_scheme'] == 'modern'
    assert calls[0]['csrf'] == 'csrf'


class Rows:
    def __init__(self, rows): self.rows = rows
    def mappings(self): return self
    def all(self): return self.rows


class ReadConnection:
    def __init__(self, rows): self.rows = rows; self.queries = []
    def execute(self, sql, params):
        self.queries.append((str(sql), params))
        assert str(sql).lstrip().upper().startswith('SELECT ')
        assert params == {'hash': hashlib.sha256(b'synthetic-owner').hexdigest()}
        return Rows(self.rows)


def legacy_reader():
    from live_integrity import auth
    assert hasattr(auth, 'read_legacy_owner'), 'durable legacy verifier predicate missing'
    return auth.read_legacy_owner


def test_durable_legacy_owner_resolves_existing_namespace_read_only():
    connection = ReadConnection([{'expires_at': datetime.now(timezone.utc)+timedelta(hours=1)}])
    assert legacy_reader()(connection, 'synthetic-owner') == 'owner:flowsignal.contact@gmail.com'
    assert len(connection.queries) == 1
    assert 'flowsignal_owner_sessions' in connection.queries[0][0]


@pytest.mark.parametrize('expiry', [None, 'tomorrow', 123, True,
    datetime.now(), datetime(2000,1,1,tzinfo=timezone.utc)])
def test_missing_invalid_or_expired_durable_expiry_denied(expiry):
    from live_integrity.auth import AccessDenied
    reader = legacy_reader()
    with pytest.raises(AccessDenied): reader(ReadConnection([{'expires_at':expiry}]), 'synthetic-owner')


@pytest.mark.parametrize('rows', [[], [{'expires_at':datetime(2100,1,1,tzinfo=timezone.utc)}]*2])
def test_unknown_or_ambiguous_durable_session_denied(rows):
    from live_integrity.auth import AccessDenied
    reader = legacy_reader()
    with pytest.raises(AccessDenied): reader(ReadConnection(rows), 'synthetic-owner')


@pytest.mark.parametrize('actor', [{}, {'role':'user','email':'flowsignal.contact@gmail.com'},
    {'role':'admin','email':'different@example.invalid'}, {'role':'admin','email':None}])
def test_noncanonical_owner_mapping_blocks(monkeypatch, actor):
    import owner_identity
    from live_integrity.auth import AccessDenied
    monkeypatch.setattr(owner_identity, 'legacy_owner_actor', lambda: actor)
    with pytest.raises(AccessDenied):
        legacy_reader()(ReadConnection([{'expires_at':datetime(2100,1,1,tzinfo=timezone.utc)}]), 'synthetic-owner')


def test_duplicate_authorization_is_rejected(monkeypatch):
    result, calls = request(monkeypatch, [('Authorization','Bearer one'),('Authorization','FlowSignalUser two')])
    assert result.status_code == 401 and not calls


def test_expiry_equal_to_now_blocks(monkeypatch):
    from live_integrity import auth
    now = datetime(2026,1,1,tzinfo=timezone.utc)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None): return now
    monkeypatch.setattr(auth, 'datetime', Clock)
    expiry = Clock(2026,1,1,tzinfo=timezone.utc)
    with pytest.raises(auth.AccessDenied):
        auth.read_legacy_owner(ReadConnection([{'expires_at':expiry}]), 'synthetic-owner')


def test_real_durable_auth_and_isolated_worker_have_no_session_side_effects(pg, monkeypatch):
    import json, os, subprocess, sys, time
    from pathlib import Path
    from sqlalchemy import text
    from live_integrity.auth import read_admin, read_legacy_owner, AccessDenied
    from live_integrity.read_store import read_only
    from services import user_auth_service as modern
    modern.metadata.create_all(pg)
    token = 'synthetic-owner'
    digest = hashlib.sha256(token.encode()).hexdigest()
    with pg.begin() as c:
        c.execute(text('CREATE TABLE IF NOT EXISTS flowsignal_owner_sessions (token_hash text primary key, created_at timestamptz, last_seen_at timestamptz, expires_at timestamptz)'))
        c.execute(text('INSERT INTO flowsignal_owner_sessions VALUES (:h, now(), now(), now()+interval \'1 hour\') ON CONFLICT (token_hash) DO UPDATE SET expires_at=excluded.expires_at'), {'h':digest})
        for role in ('admin','user'):
            uid = 'legacy-bridge-'+role
            h = hashlib.sha256(('bridge-'+role).encode()).hexdigest()
            c.execute(text('DELETE FROM flowsignal_sessions WHERE token_hash=:h'), {'h':h})
            c.execute(text('DELETE FROM flowsignal_users WHERE id=:id'), {'id':uid})
            c.execute(modern.users.insert().values(id=uid,email=role+'@example.invalid',full_name='Synthetic',password_hash='not-a-password',role=role,is_active=True,email_verified=True,approval_status='APPROVED',created_at=1,updated_at=1))
            c.execute(modern.sessions.insert().values(token_hash=h,user_id=uid,csrf_token='synthetic-csrf',created_at=1,expires_at=time.time()+3600,last_seen_at=123,revoked_at=None))
    def snapshot():
        with read_only(pg) as c:
            return {name: c.execute(text('SELECT * FROM '+name+' ORDER BY 1')).all()
                for name in ('flowsignal_owner_sessions','flowsignal_sessions','flowsignal_users')}
    before = snapshot()
    # A cache-only entry cannot confer durable authority; reader never accesses api.
    import types
    cache = {'cache-only': {'role':'admin'}}
    monkeypatch.setitem(sys.modules, 'api', types.SimpleNamespace(SESSIONS=cache))
    with read_only(pg) as c:
        assert c.execute(text('SHOW transaction_read_only')).scalar_one() == 'on'
        assert read_legacy_owner(c,token) == 'owner:flowsignal.contact@gmail.com'
        for wrong in ('unknown', 'cache-only', 'bridge-user', 'bridge-admin', digest):
            with pytest.raises(AccessDenied): read_legacy_owner(c,wrong)
        with pytest.raises(AccessDenied): read_admin(c,token,'any-csrf')
        assert read_admin(c,'bridge-admin','synthetic-csrf') == 'owner:admin@example.invalid'
        for bad in ('wrong',''):
            with pytest.raises(AccessDenied): read_admin(c,'bridge-admin',bad)
    payload = dict(auth_scheme='legacy-owner',token=token,csrf='',strategy_id='absent',symbol='XAUUSD',runtime={},deadline=time.monotonic()+8)
    env = {'PATH':os.environ['PATH'], 'DATABASE_URL':os.environ['VERIFIER_TEST_PG_DSN'],
        'STRATEGY_LIVE_INTEGRITY_VERIFY_ENABLED':'1', 'PYTHONDONTWRITEBYTECODE':'1'}
    run = subprocess.run([sys.executable,'-I','-B',str(Path('live_integrity/worker.py').resolve())],
        input=json.dumps(payload)+'\n',capture_output=True,text=True,env=env,timeout=10)
    assert run.returncode == 0
    response = json.loads(run.stdout)
    assert response['body']['REAL_ORDER_DISPATCH_AVAILABLE'] is False
    assert response['body']['decision'] == 'WOULD_BLOCK'
    assert response['body']['block_reasons'] == ['BUILD_IDENTITY_UNVERIFIED']
    assert token not in run.stdout + run.stderr
    assert snapshot() == before
    assert cache == {'cache-only': {'role':'admin'}}
