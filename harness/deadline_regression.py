"""Native red/green evidence using pinned old code outside release source."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

PIN = 'dcd2c0677c80c32a0cd7639ca09fea9eaf2d51f6e382c2cc2940e7317040e296'
BASE = '63b42795ca26bdecb1be6d7758c8ae757eee90d4'

def prove(root, evidence):
    here = Path(__file__).resolve()
    baseline = here.parent/'test-only-inputs/recovery_readers_baseline.py'
    assert hashlib.sha256(baseline.read_bytes()).hexdigest() == PIN
    results = {}
    for mode in ('old', 'fixed'):
        xml = evidence/('deadline-'+mode+'.xml')
        proc = subprocess.run([sys.executable, '-B', str(here), mode, str(xml)],
            cwd=root/'Backend', capture_output=True, text=True)
        (evidence/('deadline-'+mode+'.log')).write_text(proc.stdout+proc.stderr)
        totals = {k: sum(int(s.get(k, 0)) for s in ET.parse(xml).iter('testsuite'))
                  for k in ('tests', 'failures', 'errors', 'skipped')}
        expected = dict(tests=1 if mode=='old' else 5,
                        failures=1 if mode=='old' else 0, errors=0, skipped=0)
        assert totals == expected and proc.returncode == (1 if mode=='old' else 0)
        if mode == 'old':
            assert 'RECOVERY_BROKER_READ_FAILED' in proc.stdout
        results[mode] = totals
    results.update(baseline_source=BASE, baseline_reader_sha256=PIN,
        old_shared_budget_fails=True, fresh_per_operation_budget_passes=True,
        single_operation_timeout_passes=True, timeout_seconds=20)
    (evidence/'deadline-regression.json').write_text(json.dumps(results, sort_keys=True))

if __name__ == '__main__':
    mode, xml = sys.argv[1:]
    sys.path.insert(0, str(Path.cwd()))
    if mode == 'old':
        path = Path(__file__).parent/'test-only-inputs/recovery_readers_baseline.py'
        assert hashlib.sha256(path.read_bytes()).hexdigest() == PIN
        spec = importlib.util.spec_from_file_location('startup_recovery.readers', path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    import pytest
    args = ['-q','-p','no:cacheprovider','--junitxml='+xml]
    if mode == 'old':
        args += ['tests/test_recovery_readers.py::test_independent_discovery_snapshots_get_fresh_deadlines']
    else:
        args += ['tests/test_recovery_readers.py','-k',
                 'independent_discovery or submission_reads_and_idle or cannot_renew']
    raise SystemExit(pytest.main(args))
