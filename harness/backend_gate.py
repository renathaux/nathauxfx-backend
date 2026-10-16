"""Disposable, network-denied backend gate; no production entrypoints or data."""
import glob
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import xml.etree.ElementTree as ET
from contextlib import nullcontext
from git_shim import temporary_git_shim, prove as prove_git_shim
from verify_source import verify
from deadline_regression import prove as prove_deadline_regression
from auth_overlay import auth_overlay, required_expand, INPUTS as AUTH_INPUTS, SOURCE as AUTH_SOURCE

ROOT = Path(sys.argv[1]).resolve()
EVIDENCE = Path(sys.argv[2]).resolve()
assert platform.system() == 'Linux' and platform.machine() == 'x86_64'
assert platform.python_version() == '3.14.3'
assert os.getuid() == 501 and os.getgid() == 1000
assert os.environ['VERIFIER_TEST_PG_DSN'].startswith('postgresql+psycopg2://cert_user@/verifier_test?host=/tmp/fs-verifier-pg.')
verify(ROOT)
os.chdir(ROOT/'Backend')
sys.path.insert(0, str(ROOT/'Backend'))
from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic import command
from sqlalchemy import create_engine, text

cfg = Config('alembic.ini')
assert ScriptDirectory.from_config(cfg).get_heads() == ['20261010_0031']
engine = create_engine(os.environ['DATABASE_URL'])
command.upgrade(cfg, '20261001_0030')
tables = ['recovery_accounts','recovery_attempts','recovery_mutations','recovery_checkpoint_heads']
def snapshot():
    with engine.connect() as c:
        return {'revision': c.execute(text('SELECT version_num FROM alembic_version')).scalar_one(),
                'rows': {t: c.execute(text('SELECT count(*) FROM '+t)).scalar_one() for t in tables}}
before = snapshot()
assert before['revision'] == '20261001_0030' and not any(before['rows'].values())
subprocess.run([sys.executable, '-B', '-m', 'startup_recovery.migrate'], check=True)
after = snapshot()
assert after['revision'] == '20261010_0031' and before['rows'] == after['rows']
with engine.connect() as c:
    assert c.execute(text('SELECT count(*) FROM recovery_legacy_adoptions')).scalar_one() == 0
subprocess.run([sys.executable, '-B', '-m', 'startup_recovery.migrate'], check=True)
assert snapshot() == after
engine.dispose()
(EVIDENCE/'migration.json').write_text(json.dumps({'before':before,'after':after,'noop':True},sort_keys=True))

def expand(*patterns):
    return required_expand(ROOT/'Backend',*patterns)
cutover = expand('tests/test_cutover_*.py')
recovery = expand('tests/test_recovery_*.py','tests/test_strategy_live_version_binding.py')
bounded = sorted(set(recovery + cutover + ['tests/test_runtime_paths.py','tests/test_runtime_paths_postgres.py','tests/test_database_configuration.py']))
suites = [
    ('runtime-identity',['tests/test_runtime_identity_observations.py','tests/test_runtime_identity_endpoint.py'],49),
    ('architecture',['tests/test_cutover_first_start.py','-k','not first_start_transport and not first_start_management and not first_start_failed'],424),
    ('runtime-owner',['tests/test_owner_handoff_runtime.py'],98),
    ('handoff-reader',['tests/test_handoff_reader.py','tests/test_handoff_reader_integration.py'],25),
    ('recovery-checkpoint',expand('tests/test_recovery*.py'),392),
    ('auth-bridge',expand('tests/test_live_integrity_*.py','tests/test_hosted_live_integrity_integration.py','tests/test_strategy_live_integrity_verifier.py'),180),
    ('canonical-successor',['tests/test_canonical_strategy_successor.py'],19),
    ('broader-auth',['tests/'+name+'.py' for name in ['test_live_order_intent','test_live_decimal_prices','test_live_identity_metadata_integration','test_broker_execution_metadata','test_strategy_studio_live_candidate','test_fundamental_execution_guard','test_strategy_engine_evaluator','test_account_approval']],164),
    ('multi-user',['tests/test_multi_user_auth.py'],4),
    ('preenable',['tests/test_live_integrity_preenable.py'],41),
    ('owner-adoption',expand('tests/test_owner_handoff*.py','tests/test_legacy_adoption*.py'),454),
    ('recovery-runtime',expand('tests/test_recovery*.py','tests/test_runtime_paths*.py'),412),
    ('hosted-verifier',expand('tests/test_live_integrity_*.py','tests/test_hosted_live_integrity_integration.py','tests/test_strategy_live_integrity_verifier.py'),169),
    ('broader-affected',['tests/'+name+'.py' for name in ['test_live_order_intent','test_live_decimal_prices','test_live_identity_metadata_integration','test_broker_execution_metadata','test_strategy_studio_live_candidate','test_fundamental_execution_guard','test_strategy_engine_evaluator']],157),
    ('database-config',['tests/test_database_configuration.py'],7),
    ('owner',expand('tests/test_owner_handoff*.py'),270),
    ('focused',expand('tests/test_cutover_evidence.py','tests/test_cutover_first_start.py','tests/test_legacy_adoption_*.py','tests/test_owner_handoff*.py'),903),
    ('safe-recovery',recovery,444),
    ('cutover',cutover,766),
    ('bounded',bounded,1237),
]
results = {}
try:
    prove_deadline_regression(ROOT, EVIDENCE)
    verify(ROOT)
    for name, paths, count in suites:
        verify(ROOT)
        adapter=temporary_git_shim(ROOT) if name in {'bounded','recovery-runtime'} else nullcontext(None)
        overlay=auth_overlay(ROOT,Path(__file__).parent) if name=='auth-bridge' else nullcontext(None)
        with adapter as test_env, overlay as auth_inputs:
            selected=list(paths)
            if auth_inputs is not None:
                selected.append(str(auth_inputs['test']))
                (EVIDENCE/'auth-overlay.json').write_text(json.dumps(dict(source=AUTH_SOURCE,inputs=AUTH_INPUTS,classification='TEST_ONLY',selected_test=str(auth_inputs['test']),helper_collected=False),sort_keys=True))
            if name=='bounded':
                (EVIDENCE/'git-shim-before.json').write_text(json.dumps(prove_git_shim(ROOT,test_env),sort_keys=True))
            result = subprocess.run([sys.executable,'-B','-m','pytest','-q','-p','no:cacheprovider',
                '-p','fixture_plugin','--junitxml='+str(EVIDENCE/(name+'.xml')),*selected],
                stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,env=test_env)
        if auth_inputs is not None:
            assert not auth_inputs['directory'].exists()
            (EVIDENCE/'auth-overlay-cleanup.json').write_text(json.dumps(dict(removed=True,source=verify(ROOT))))
        if name=='bounded':
            (EVIDENCE/'git-shim-after.json').write_text(json.dumps({'shim_removed':True,'source':verify(ROOT)},sort_keys=True))
        (EVIDENCE/(name+'.log')).write_text(result.stdout)
        verify(ROOT)
        xml = ET.parse(EVIDENCE/(name+'.xml'))
        if name=='auth-bridge':
            cases=list(xml.iter('testcase'))
            assert sum('test_strategy_studio_owner_boundary' in c.get('classname','') for c in cases)==11, 'OWNER_BOUNDARY_COVERAGE_MISSING'
            assert not any('test_strategy_studio_service' in c.get('classname','') for c in cases), 'HELPER_ACCIDENTALLY_COLLECTED'
        totals = {k: sum(int(s.get(k,0)) for s in xml.iter('testsuite')) for k in ['tests','failures','errors','skipped']}
        results[name] = totals
        (EVIDENCE/'backend-results.json').write_text(json.dumps(results,sort_keys=True,indent=2))
        print(json.dumps({'suite':name,**totals}),flush=True)
        assert result.returncode == 0 and totals == {'tests':count,'failures':0,'errors':0,'skipped':0}, 'BACKEND_GATE_FAILED_'+name
    perf_env=dict(os.environ,FRESHNESS_BENCHMARK_MODE='production_concurrent',FRESHNESS_EVIDENCE=str(EVIDENCE))
    perf_env['PYTHONPATH']=os.environ['PYTHONPATH']+os.pathsep+str(ROOT/'Backend')+os.pathsep+str(ROOT/'Backend/tests')
    result=subprocess.run([sys.executable,'-B','-m','pytest','-q','-p','no:cacheprovider',
        '--junitxml='+str(EVIDENCE/'freshness-performance.xml'),str(Path(__file__).with_name('test_freshness_performance.py'))],
        stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,env=perf_env)
    (EVIDENCE/'freshness-performance.log').write_text(result.stdout)
    totals={k:sum(int(s.get(k,0)) for s in ET.parse(EVIDENCE/'freshness-performance.xml').iter('testsuite')) for k in ['tests','failures','errors','skipped']}
    assert result.returncode==0 and totals==dict(tests=6,failures=0,errors=0,skipped=0),'NATIVE_FRESHNESS_PERFORMANCE_FAILED'
    measured=[json.loads((EVIDENCE/f'freshness-performance-production_concurrent-{i}.json').read_text()) for i in range(1,7)]
    assert all(r['performance_pass'] and r['database_unchanged'] and r['filesystem_unchanged'] and r['broker_observations']==2 and r['fixture_snapshot_bytes']>=3403219 for r in measured)
    assert max(r['total_seconds'] for r in measured)+1.421998<20,'MEASURED_OPERATIONAL_RESERVE_EXHAUSTED'
    results['freshness-performance']=totals
    (EVIDENCE/'backend-results.json').write_text(json.dumps(results,sort_keys=True,indent=2))
finally:
    assert not (ROOT/'Backend/database/flowsignal.db').exists(), 'FIXTURE_NOT_REMOVED'
    (EVIDENCE/'source-after.json').write_text(json.dumps(verify(ROOT),sort_keys=True))
