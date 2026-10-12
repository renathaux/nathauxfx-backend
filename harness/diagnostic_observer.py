"""Observes synthetic fixture values only; no product callbacks or mutation."""
import os
from pathlib import Path
from types import ModuleType
import pytest
from diagnostic_contract import canonical, digest

def emit(name, value):
    (Path(os.environ['DIAGNOSTIC_EVIDENCE'])/name).write_bytes(canonical(value))

def pytest_collection_finish(session):
    emit('collection-order.json', [item.nodeid for item in session.items])

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.when != 'call':
        return
    target = 'test_actual_position_sync_a_b_a_preserves_frozen_management'
    cleanup = 'test_cleanup_never_removes_current_process_and_only_evicts_needed_result'
    if not (item.name.startswith(target) or item.name == cleanup):
        return
    evidence = {'node': item.nodeid, 'outcome': report.outcome, 'observation': 'UNAVAILABLE'}
    if call.excinfo is not None:
        for entry in call.excinfo.traceback:
            if entry.frame.code.name != (target if item.name.startswith(target) else cleanup):
                continue
            values = entry.frame.f_locals
            if item.name.startswith(target):
                # Only existing module dictionary and primitive synthetic test payload.
                module = values.get('api')
                if type(module) is not ModuleType or 'trade' not in values:
                    evidence['reason'] = 'MISSING_SYNTHETIC_LOCALS'
                    continue
                mirror = vars(module).get('LIVE_ACTIVE_ORDERS')
                if type(mirror) is not dict or 'EURUSD' not in mirror:
                    evidence['reason'] = 'MISSING_MIRROR_KEY'
                    continue
                current = mirror['EURUSD']
                supplied = values['trade']
                evidence.update(observation='CAPTURED', current=current, supplied=supplied,
                                equal_at_failure=canonical(current)==canonical(supplied),
                                limitation='Local trade may alias mirror; not pre-sync immutability proof')
            else:
                rows = {}
                root = values.get('tmp_path')
                if isinstance(root, Path):
                    for key in ('a', 'b', 'c'):
                        row = values.get(key)
                        if type(row) is dict and type(row.get('job_id')) is str:
                            p = root/row['job_id']/'state.json'
                            rows[key] = {'path': str(p), 'job_id': row['job_id'],
                                         'exists': p.exists(), 'mtime_ns': p.stat().st_mtime_ns if p.exists() else None}
                    evidence.update(observation='CAPTURED', surviving_files=rows)
    emit('semantic-'+digest(item.nodeid)+'.json', evidence)
