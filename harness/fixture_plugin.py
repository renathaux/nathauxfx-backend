"""Scope the synthetic legacy SQLite file to the two legacy-schema tests only."""
import json
import os
from pathlib import Path
import pytest
from fixture import temporary_database, fingerprint
from verify_source import verify

ALLOWED = {
    'test_models_match_existing_sqlite_tables',
    'test_local_sqlite_source_is_readable_for_one_time_copy',
}
PIN = '03489da97292902be00cb0c9cb2e6d97d43fd6df9b1b1668daa82d5054ab2a4e'

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    if item.path.name != 'test_database_configuration.py' or item.name not in ALLOWED:
        yield
        return
    root = Path(item.path).resolve().parents[2]
    before = verify(root)
    assert fingerprint() == PIN
    directory = root/'Backend/database'
    directory.mkdir(exist_ok=True)
    path = directory/'flowsignal.db'
    try:
        with temporary_database(path):
            yield
    finally:
        assert not path.exists()
        after = verify(root)
        assert before == after
        evidence = Path(os.environ['FIXTURE_EVIDENCE'])
        with evidence.open('a') as out:
            out.write(json.dumps({'test': item.name, 'classification': 'SYNTHETIC_TEST_ONLY',
                'historical_database_used': False, 'ddl_sha256': PIN,
                'fixture_deleted': True, 'source_before': before, 'source_after': after}, sort_keys=True)+'\n')
