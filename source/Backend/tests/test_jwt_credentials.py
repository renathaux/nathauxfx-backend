"""Legacy JWT authority is runtime-only and never replaces DB session auth."""
import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from jose import jwt
from jose.exceptions import JWTError


@pytest.fixture
def auth_module(monkeypatch):
    import auth
    monkeypatch.delenv('JWT_SECRET_KEY', raising=False)
    return auth


def test_no_embedded_jwt_signing_key(auth_module):
    tree = ast.parse(Path(auth_module.__file__).read_text())
    unsafe = any(isinstance(n, ast.Assign) and
        any(isinstance(t, ast.Name) and t.id == 'SECRET_KEY' for t in n.targets)
        and isinstance(n.value, ast.Constant) and bool(n.value.value)
        for n in ast.walk(tree))
    assert not unsafe, 'Embedded JWT signing key remains'


@pytest.mark.parametrize('delta,seconds', [(None, 86400), (timedelta(seconds=90), 90)])
def test_runtime_sign_verify_preserves_algorithm_claims_and_expiration(auth_module, monkeypatch, delta, seconds, capsys, caplog):
    class Frozen(datetime):
        @classmethod
        def utcnow(cls):
            return cls(2030, 1, 1)
    monkeypatch.setattr(auth_module, 'datetime', Frozen)
    key = 'synthetic-jwt-signing-key'
    monkeypatch.setenv('JWT_SECRET_KEY', key)
    claims = {'sub': 'synthetic-owner', 'role': 'admin', 'custom': {'x': 1}}
    token = auth_module.create_access_token(claims, expires_delta=delta)
    assert jwt.get_unverified_header(token)['alg'] == 'HS256'
    expected = dict(claims, exp=1893456000 + seconds)
    assert jwt.decode(token, key, algorithms=['HS256']) == expected
    assert auth_module.verify_access_token(token) == expected
    assert 'exp' not in claims
    output = capsys.readouterr()
    assert key not in output.out + output.err + caplog.text
    assert token not in output.out + output.err + caplog.text


def test_runtime_key_change_rejects_old_signature(auth_module, monkeypatch):
    monkeypatch.setenv('JWT_SECRET_KEY', 'synthetic-first-key')
    first = auth_module.create_access_token({'sub': 'synthetic-owner'})
    monkeypatch.setenv('JWT_SECRET_KEY', 'synthetic-second-key')
    with pytest.raises(JWTError, match='JWT_VERIFICATION_FAILED'):
        auth_module.verify_access_token(first)
    second = auth_module.create_access_token({'sub': 'synthetic-owner'})
    assert auth_module.verify_access_token(second)['sub'] == 'synthetic-owner'


@pytest.mark.parametrize('key', [None, '', '   '])
@pytest.mark.parametrize('operation', ['create_access_token', 'verify_access_token'])
def test_missing_or_empty_jwt_key_fails_before_crypto(auth_module, monkeypatch, key, operation):
    if key is not None:
        monkeypatch.setenv('JWT_SECRET_KEY', key)
    calls = []
    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError('Unconfigured crypto must not run')
    monkeypatch.setattr(auth_module.jwt, 'encode', forbidden)
    monkeypatch.setattr(auth_module.jwt, 'decode', forbidden)
    argument = {'sub': 'synthetic-owner'} if operation == 'create_access_token' else 'malformed'
    with pytest.raises(RuntimeError, match='JWT_SIGNING_KEY_UNAVAILABLE'):
        getattr(auth_module, operation)(argument)
    assert calls == []


@pytest.mark.parametrize('operation', ['create_access_token', 'verify_access_token'])
def test_crypto_error_cannot_disclose_key_or_token(auth_module, monkeypatch, capsys, caplog, operation):
    key = 'synthetic-sensitive-key'
    token = 'synthetic-sensitive-token'
    monkeypatch.setenv('JWT_SECRET_KEY', key)
    def broken(*args, **kwargs):
        raise JWTError(key + token)
    monkeypatch.setattr(auth_module.jwt, 'encode' if operation == 'create_access_token' else 'decode', broken)
    argument = {'sub': 'synthetic-owner'} if operation == 'create_access_token' else token
    with pytest.raises(JWTError) as captured:
        getattr(auth_module, operation)(argument)
    output = capsys.readouterr()
    evidence = str(captured.value) + output.out + output.err + caplog.text
    assert key not in evidence and token not in evidence
    assert captured.value.__suppress_context__


@pytest.mark.parametrize('kind', ['expired', 'wrong_algorithm', 'wrong_key', 'malformed'])
def test_verification_enforces_original_algorithm_signature_and_expiry(auth_module, monkeypatch, kind):
    key = 'synthetic-jwt-key'
    monkeypatch.setenv('JWT_SECRET_KEY', key)
    token = jwt.encode({'sub': 'synthetic-owner', 'exp': 1 if kind == 'expired' else 4102444800},
        'synthetic-wrong-key' if kind == 'wrong_key' else key,
        algorithm='HS384' if kind == 'wrong_algorithm' else 'HS256')
    if kind == 'malformed':
        token = 'malformed'
    with pytest.raises(JWTError, match='JWT_VERIFICATION_FAILED'):
        auth_module.verify_access_token(token)


def test_missing_jwt_key_does_not_authorize_protected_db_session_routes(auth_module, monkeypatch):
    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool
    from services import user_auth_service as sessions
    engine = create_engine('sqlite:///:memory:', poolclass=StaticPool, connect_args={'check_same_thread': False})
    sessions.sessions.metadata.create_all(engine)
    monkeypatch.setattr(sessions, '_engine', lambda engine=None: test_engine)
    test_engine = engine
    app = FastAPI()
    @app.get('/protected')
    def protected(user=Depends(sessions.require_admin)):
        return {'authorized': True}
    forged = jwt.encode({'sub': 'synthetic-owner', 'role': 'admin'}, 'synthetic-key', algorithm='HS256')
    for header in ('', 'Bearer '+forged, sessions.USER_AUTH_SCHEME+' '+forged):
        response = TestClient(app).get('/protected', headers={'Authorization': header})
        assert response.status_code == 401
        assert response.json() == {'detail': 'AUTHENTICATION_REQUIRED'}
        assert forged not in response.text
    engine.dispose()
