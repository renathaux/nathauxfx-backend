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

def run(root, evidence):
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
            assert overlay['source'] == SOURCE
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
            record('sqlite-before.json', create_empty_sqlite(db))
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
            with (evidence/'broad.log').open('w') as log:
                result = subprocess.run(command, cwd=test_root/'Backend', env=env,
                                        stdout=log, stderr=subprocess.STDOUT)
            rows = read_cases(evidence/'broad.xml')
            record('diagnostic-results.json', dict(cases=rows, fingerprint_sha256=digest(rows),
                process_returncode=result.returncode, reference_approved=False,
                classification='COLLECTED_DIAGNOSTIC_NOT_CERTIFICATION'))
            record('sqlite-after.json', sqlite_snapshot(db))
            assert result.returncode in (0, 1), 'DIAGNOSTIC_EXECUTION_ERROR'
        record('diagnostic-cleanup.json', {'disposable_copy_removed': not Path(temporary).exists()})
    finally:
        after = verify(root)
        assert before == after
        record('source-after.json', after)

if __name__ == '__main__':
    run(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve())
