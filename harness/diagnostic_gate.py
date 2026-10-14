"""Collection only. Results are NEVER approved as a reference automatically."""
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
from diagnostic_contract import canonical, create_empty_sqlite, digest, read_cases, sqlite_snapshot
from verify_source import verify, SOURCE

DOC_SHA = '0d66543aad759a8d38d3470bfb422fda3d6e8494e880862fec3d0e6713762d12'

def run(root, evidence, *, logging_only=False):
    assert platform.system() == 'Linux' and platform.machine() == 'x86_64'
    assert platform.python_version() == '3.14.3'
    assert (os.getuid(), os.getgid()) == (501, 1000)
    harness = Path(__file__).resolve().parent
    before = verify(root)
    def record(name, value):
        (evidence/name).write_bytes(canonical(value))
    try:
        with tempfile.TemporaryDirectory(prefix='diagnostic-broad-', dir=root.parent) as temporary:
            test_root = Path(temporary)/'source'
            shutil.copytree(root, test_root)
            overlay = json.loads((harness/'broad-inputs.json').read_bytes())
            # Test-only historical bytes keep their independently pinned provenance.
            assert overlay['source'] == '4c1c483086f08a8c2596cb400725071d03a1249e'
            for row in overlay['files']:
                p = Path(row['path'])
                assert not p.is_absolute() and '..' not in p.parts
                assert str(p).startswith(('Backend/tests/', 'Backend/scripts/', 'Backend/benchmarks/'))
                raw = (harness/'broad-inputs'/p).read_bytes()
                assert len(raw) == row['size'] and hashlib.sha256(raw).hexdigest() == row['sha256']
                dst = test_root/p
                assert not dst.exists()
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(raw)
            raw = (harness/'diagnostic-inputs/INDICATOR_EVENT_STREAM.md').read_bytes()
            assert len(raw) == 5095 and hashlib.sha256(raw).hexdigest() == DOC_SHA
            (test_root/'Backend/INDICATOR_EVENT_STREAM.md').write_bytes(raw)
            assert not (test_root/'Backend/fundamentals/bls_timestamp_manifest_12m.json').exists()
            db = test_root/'Backend/database/flowsignal.db'
            db.parent.mkdir(exist_ok=True)
            initial_sqlite = create_empty_sqlite(db)
            record('sqlite-before.json', initial_sqlite)
            subprocess.run(['/usr/bin/git', 'init', '-q', str(test_root)], check=True)
            subprocess.run(['/usr/bin/git', '-C', str(test_root), 'add', '--', '*.py'], check=True)
            env = {k: os.environ[k] for k in ('HOME', 'PATH', 'PYTHONDONTWRITEBYTECODE',
                   'PYTHON_DOTENV_DISABLED', 'PYTHONPATH', 'VERIFIER_TEST_PG_DSN')}
            assert env['VERIFIER_TEST_PG_DSN'].startswith('postgresql+psycopg2://cert_user@/verifier_test?host=/tmp/fs-verifier-pg.')
            env.update(PYTHONHASHSEED='0', TZ='UTC', LC_ALL='C.UTF-8', DIAGNOSTIC_EVIDENCE=str(evidence))
            record('diagnostic-environment.json', dict(python=sys.version, executable=sys.executable,
                platform=platform.platform(), os_release=Path('/etc/os-release').read_text(),
                uid=os.getuid(), gid=os.getgid(), environment=env, cwd=str(test_root/'Backend'),
                filesystem=subprocess.check_output(['stat', '-f', '-c', '%T', str(test_root)], text=True).strip(),
                git=subprocess.check_output(['/usr/bin/git', '--version'], text=True).strip(),
                postgres=subprocess.check_output(['pg_config', '--version'], text=True).strip(),
                packages=sorted([d.metadata['Name'], d.version] for d in importlib.metadata.distributions()),
                sqlite_setup='PRAGMA user_version=0; fresh synthetic empty DB; no model DDL',
                normalization='NONE', reference_approved=False))
            command = [sys.executable, '-B', '-m', 'pytest', '-q', '-ra', '-p', 'no:cacheprovider',
                       '-p', 'diagnostic_observer', '--continue-on-collection-errors', '--tb=short',
                       '--junitxml='+str(evidence/'broad.xml')]
            if logging_only:
                nodes = [
                    'tests/test_fundamental_insight_cache.py::FundamentalInsightCacheTests::test_cache_invalidation_failure_does_not_fail_committed_ingestion',
                    'tests/test_fundamental_insight_reliability.py::FundamentalInsightReliabilityTests::test_timing_record_uses_production_visible_logger',
                    'tests/test_indicator_stream_account_scope.py::test_account_scoped_v3b_5m_correction_rolls_back_and_replays',
                    'tests/test_indicator_stream_account_scope.py::test_irreversible_live_lifecycle_blocks_repair_without_history_mutation[SUBMITTING]',
                    'tests/test_indicator_stream_account_scope.py::test_irreversible_live_lifecycle_blocks_repair_without_history_mutation[CONSUMED]',
                ]
                results = []
                for i, node in enumerate(nodes):
                    probe_evidence = evidence/f'logging-{i}'
                    probe_evidence.mkdir()
                    snapshot = sqlite_snapshot(db)
                    assert snapshot == initial_sqlite, 'LOGGING_SQLITE_INITIAL_DRIFT'
                    assert snapshot['schema'] == [] and snapshot['integrity'] == 'ok', 'LOGGING_SQLITE_DRIFT'
                    record(f'logging-{i}/sqlite-before.json', snapshot)
                    probe_env = dict(env, DIAGNOSTIC_EVIDENCE=str(probe_evidence))
                    xml = evidence/f'logging-{i}.xml'
                    with (evidence/f'logging-{i}.log').open('w') as log:
                        result = subprocess.run(command[:-1]+['--junitxml='+str(xml), node],
                            cwd=test_root/'Backend', env=probe_env, stdout=log, stderr=subprocess.STDOUT)
                    rows = read_cases(xml)
                    assert sqlite_snapshot(db) == snapshot, 'LOGGING_SQLITE_MUTATED'
                    results.append(dict(node=node, process_returncode=result.returncode, cases=rows))
                record('logging-results.json', results)
                assert all(v['process_returncode']==0 and len(v['cases'])==1 and v['cases'][0]['status']=='passed' for v in results), 'ISOLATED_LOGGING_PROOF_BLOCKED'
            else:
                with (evidence/'broad.log').open('w') as log:
                    result = subprocess.run(command, cwd=test_root/'Backend', env=env,
                                            stdout=log, stderr=subprocess.STDOUT)
                rows = read_cases(evidence/'broad.xml')
                record('diagnostic-results.json', dict(cases=rows, fingerprint_sha256=digest(rows),
                    process_returncode=result.returncode, reference_approved=False,
                    classification='COLLECTED_DIAGNOSTIC_NOT_CERTIFICATION'))
                assert result.returncode in (0, 1), 'DIAGNOSTIC_EXECUTION_ERROR'
            record('sqlite-after.json', sqlite_snapshot(db))
        record('diagnostic-cleanup.json', {'disposable_copy_removed': not Path(temporary).exists()})
    finally:
        after = verify(root)
        assert before == after
        record('source-after.json', after)

if __name__ == '__main__':
    run(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve())
