"""Actual-image counterpart to the checkout-only build-identity rejection test.

External harness only. Synthetic Unix-socket PostgreSQL; no hosted credentials.
The unchanged checkout test remains mandatory in the backend certification gate.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest
from sqlalchemy import create_engine, text


def test_packaged_durable_auth_worker_is_read_only():
    from live_integrity.auth import read_legacy_owner, AccessDenied
    from live_integrity.build_identity import capture
    from live_integrity.read_store import read_only

    build = capture()
    assert build['backend_git_sha'] == 'e40b95132c06a6dac13919e6e03aaa6a31d5e955'
    assert build['source_manifest_sha256'] == 'dfca22d3652a11dee9a3ea9b4783e6908deb72b6137590f54f691e7c469c19c0'
    assert build['build_identity'] == '68fc8f9b5d69440bc0f8d404f059db8ebaf8683c1745b1e6fabe4b26a88bbe77'
    assert build['clean_source'] is True
    dsn = os.environ['VERIFIER_TEST_PG_DSN']
    assert 'host=/tmp/fs-verifier-pg.image.' in dsn and '/verifier_test?' in dsn
    engine = create_engine(dsn)
    token = 'synthetic-packaged-owner'
    digest = hashlib.sha256(token.encode()).hexdigest()
    try:
        with engine.begin() as connection:
            connection.execute(text('CREATE TABLE IF NOT EXISTS flowsignal_owner_sessions (token_hash text primary key, created_at timestamptz, last_seen_at timestamptz, expires_at timestamptz)'))
            connection.execute(text("INSERT INTO flowsignal_owner_sessions VALUES (:h, now(), now(), now()+interval '1 hour')"), {'h': digest})
            # Minimal independent fixed schema for the deliberately absent strategy.
            connection.execute(text('CREATE TABLE IF NOT EXISTS saved_strategies (strategy_id text, owner_id text, name text, schema_version text, updated_at timestamptz, definition_json text)'))

        def snapshot():
            with read_only(engine) as connection:
                assert connection.execute(text('SHOW transaction_read_only')).scalar_one() == 'on'
                return {table: connection.execute(text('SELECT * FROM '+table+' ORDER BY 1')).all()
                        for table in ('flowsignal_owner_sessions', 'saved_strategies')}

        before = snapshot()
        with read_only(engine) as connection:
            assert read_legacy_owner(connection, token) == 'owner:flowsignal.contact@gmail.com'
            with pytest.raises(AccessDenied):
                read_legacy_owner(connection, 'synthetic-unknown')
        env = dict(PATH=os.environ['PATH'], DATABASE_URL=dsn,
                   STRATEGY_LIVE_INTEGRITY_VERIFY_ENABLED='1', PYTHONDONTWRITEBYTECODE='1')
        import selectors
        with subprocess.Popen([sys.executable, '-I', '-B', str(Path('live_integrity/worker.py').resolve()), '--preload'],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=env) as child:
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(child.stdout,selectors.EVENT_READ)
                    assert selector.select(60), 'PRELOAD_DID_NOT_FINISH'
                assert child.stdout.readline() == b'READY\n'
                assert snapshot() == before
                payload = dict(auth_scheme='legacy-owner', token=token, csrf='',
                               strategy_id='absent', symbol='XAUUSD', runtime={}, deadline=time.monotonic()+8)
                stdout,stderr=child.communicate((json.dumps(payload)+'\n').encode(),timeout=10)
                assert child.returncode == 0
                response = json.loads(stdout)
            finally:
                if child.poll() is None:child.kill()
                child.communicate()
        assert response['status'] == 200
        assert response['body']['build'] == build
        assert response['body']['decision'] == 'WOULD_BLOCK'
        assert response['body']['REAL_ORDER_DISPATCH_AVAILABLE'] is False
        assert response['body']['block_reasons'] == ['STRATEGY_SAVED_ROW_MISSING']
        assert token.encode() not in stdout + stderr
        assert snapshot() == before
    finally:
        engine.dispose()
