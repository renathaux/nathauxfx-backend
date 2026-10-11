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
    return sorted({f for pattern in patterns for f in glob.glob(pattern)})
cutover = ['tests/test_cutover_'+name+'.py' for name in ['cli','files','first_start','manifest','postgres','service']]
recovery = expand('tests/test_recovery_*.py','tests/test_strategy_live_version_binding.py')
bounded = sorted(set(recovery + cutover + ['tests/test_runtime_paths.py','tests/test_runtime_paths_postgres.py','tests/test_database_configuration.py']))
suites = [
    ('database-config',['tests/test_database_configuration.py'],7),
    ('owner',expand('tests/test_owner_handoff*.py'),71),
    ('focused',expand('tests/test_cutover_evidence.py','tests/test_cutover_first_start.py','tests/test_legacy_adoption_*.py','tests/test_owner_handoff*.py'),281),
    ('safe-recovery',recovery,444),
    ('cutover',cutover,321),
    ('bounded',bounded,792),
]
results = {}
try:
    prove_deadline_regression(ROOT, EVIDENCE)
    verify(ROOT)
    for name, paths, count in suites:
        verify(ROOT)
        adapter=temporary_git_shim(ROOT) if name=='bounded' else nullcontext(None)
        with adapter as test_env:
            if name=='bounded':
                (EVIDENCE/'git-shim-before.json').write_text(json.dumps(prove_git_shim(ROOT,test_env),sort_keys=True))
            result = subprocess.run([sys.executable,'-B','-m','pytest','-q','-p','no:cacheprovider',
                '-p','fixture_plugin','--junitxml='+str(EVIDENCE/(name+'.xml')),*paths],
                stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,env=test_env)
        if name=='bounded':
            (EVIDENCE/'git-shim-after.json').write_text(json.dumps({'shim_removed':True,'source':verify(ROOT)},sort_keys=True))
        (EVIDENCE/(name+'.log')).write_text(result.stdout)
        verify(ROOT)
        xml = ET.parse(EVIDENCE/(name+'.xml'))
        totals = {k: sum(int(s.get(k,0)) for s in xml.iter('testsuite')) for k in ['tests','failures','errors','skipped']}
        results[name] = totals
        (EVIDENCE/'backend-results.json').write_text(json.dumps(results,sort_keys=True,indent=2))
        print(json.dumps({'suite':name,**totals}),flush=True)
        assert result.returncode == 0 and totals == {'tests':count,'failures':0,'errors':0,'skipped':0}, 'BACKEND_GATE_FAILED_'+name
finally:
    assert not (ROOT/'Backend/database/flowsignal.db').exists(), 'FIXTURE_NOT_REMOVED'
    (EVIDENCE/'source-after.json').write_text(json.dumps(verify(ROOT),sort_keys=True))
