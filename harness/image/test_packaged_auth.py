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
    assert build['backend_git_sha'] == '4c1c483086f08a8c2596cb400725071d03a1249e'
    assert build['source_manifest_sha256'] == 'dbfa367c95c106c787b8338b282c2e83f4985cecb845ac3cb5bd8e233c32a0cc'
    assert build['build_identity'] == 'b6507d18c7c1c2ee542d2ccfee3ee385ba0ac812683672b9690bd66c383ad35a'
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
        payload = dict(auth_scheme='legacy-owner', token=token, csrf='',
                       strategy_id='absent', symbol='XAUUSD', runtime={}, deadline=time.monotonic()+8)
        env = dict(PATH=os.environ['PATH'], DATABASE_URL=dsn,
                   STRATEGY_LIVE_INTEGRITY_VERIFY_ENABLED='1', PYTHONDONTWRITEBYTECODE='1')
        result = subprocess.run([sys.executable, '-I', '-B', str(Path('live_integrity/worker.py').resolve())],
                                input=json.dumps(payload)+'\n', capture_output=True, text=True,
                                env=env, timeout=10)
        assert result.returncode == 0
        response = json.loads(result.stdout)
        assert response['status'] == 200
        assert response['body']['build'] == build
        assert response['body']['decision'] == 'WOULD_BLOCK'
        assert response['body']['REAL_ORDER_DISPATCH_AVAILABLE'] is False
        assert response['body']['block_reasons'] == ['STRATEGY_SAVED_ROW_MISSING']
        assert token not in result.stdout + result.stderr
        assert snapshot() == before
    finally:
        engine.dispose()
