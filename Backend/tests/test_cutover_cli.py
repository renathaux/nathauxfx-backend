"""Explicit local CLI subprocesses, private PostgreSQL schemas, no broker path."""
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from sqlalchemy.engine import make_url
from tests.cutover_fixture import pg, native_inventory, snapshot_db
from tests.test_cutover_files import snapshot
from startup_recovery.cutover_files import CopyTarget

GUARD = r'''
import importlib.abc, os, runpy, sys
from pathlib import Path
class Guard(importlib.abc.MetaPathFinder):
 def find_spec(self,fullname,path=None,target=None):
  if '--help' in sys.argv and fullname.startswith(('sqlalchemy','startup_recovery.cutover_service','startup_recovery.cutover_db','startup_recovery.cutover_files')):
   raise AssertionError('dependency import during help')
  if fullname in {'api','db','models','ctrader_connector','trade_submission_service','services.trade_submission_service','startup_recovery.bootstrap','startup_recovery.coordinator','startup_recovery.store','startup_recovery.checkpoint_store','dotenv'} or ('transport' in fullname and not fullname.startswith('asyncio.')):
   raise AssertionError('forbidden import')
sys.meta_path.insert(0,Guard())
def audit(event,args):
 if event=='open' and isinstance(args[0],str) and Path(args[0]).name=='.env':
  raise AssertionError('dotenv read')
sys.addaudithook(audit)
sys.argv=['startup_recovery.cutover',*sys.argv[1:]]
runpy.run_module('startup_recovery.cutover',run_name='__main__')
'''


def cli(args,pg=None,env=None):
    variables={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8','TZ':'UTC','PYTHONDONTWRITEBYTECODE':'1',
        'DATABASE_URL':'postgresql://SYNTHETIC_SECRET@unreachable.invalid/not-used'}
    if pg:
        # Private schema only, also for tables declared without schema_translate_map.
        variables['CUTOVER_DATABASE_URL']=make_url(os.environ['VERIFIER_TEST_PG_DSN']).update_query_dict(
            {'options':'-csearch_path='+pg[1]}).render_as_string(hide_password=False)
    variables.update(env or {})
    result=subprocess.run([sys.executable,'-B','-c',GUARD,*map(str,args)],env=variables,
        capture_output=True,text=True)
    assert 'SYNTHETIC_SECRET' not in result.stdout+result.stderr
    assert 'Traceback' not in result.stderr, result.stderr
    return result


def inventory_args(n):
    scope=n['source'].parent/'scope.json'
    scope.write_text(json.dumps(n['scope']))
    return ['inventory','--source-root',n['source'],'--state-root',n['state'],
        '--scope',scope,'--evidence-root',n['evidence']]


def transport(n,pg):
    first=cli(inventory_args(n),pg)
    assert first.returncode==2 and not first.stderr
    report=json.loads(first.stdout)
    assert report['operation']=='inventory' and not report['blockers']
    pin=report['manifest_sha256']; manifest=report['manifest_path']
    copied=cli(['copy','--manifest',manifest,'--manifest-sha256',pin],pg)
    assert copied.returncode==2 and not copied.stderr
    result=json.loads(copied.stdout)
    assert result['copy_verified'] and not result['ready_for_cutover']
    return pin,n['state']/'.cutover'/(pin+'.json')


def test_cli_exact_flow_safe_output_pg_unchanged(pg,native_inventory):
    n=native_inventory; before=snapshot_db(pg[0],pg[1]),snapshot(n['source'])
    pin,installed=transport(n,pg)
    state_before=snapshot(n['state']); evidence_before=snapshot(n['evidence'])
    result=cli(['verify','--manifest',installed,'--manifest-sha256',pin],pg)
    assert result.returncode==2 and not result.stderr
    report=json.loads(result.stdout)
    assert set(report)=={'operation','manifest_sha256','manifest_path','copy_verified','ready_for_cutover',
        'blockers','operational_blockers','remaining_unknowns','copied_count','reused_count'}
    assert report['copy_verified'] and not report['ready_for_cutover'] and not report['blockers']
    assert 'LEGACY_CUTOVER_REQUIRED' in report['operational_blockers']
    assert 'LEGACY_RECONCILIATION_REQUIRED' in report['operational_blockers']
    assert 'synthetic_legacy' not in result.stdout
    assert (snapshot_db(pg[0],pg[1]),snapshot(n['source']))==before
    assert snapshot(n['state'])==state_before and snapshot(n['evidence'])==evidence_before


@pytest.mark.parametrize('env,code',[
    ({},'CUTOVER_DATABASE_URL_REQUIRED'),
    ({'CUTOVER_DATABASE_URL':'sqlite:///:memory:'},'CUTOVER_POSTGRESQL_REQUIRED'),
    ({'CUTOVER_DATABASE_URL':'SYNTHETIC_SECRET'},'CUTOVER_DATABASE_URL_INVALID')])
def test_cli_database_is_explicit_pg_only_no_env_load(native_inventory,env,code):
    n=native_inventory
    (n['source']/'.env').write_text('CUTOVER_DATABASE_URL=postgresql://SYNTHETIC_SECRET@invalid/test')
    result=cli(inventory_args(n),env=env)
    assert result.returncode==2 and not result.stderr
    assert json.loads(result.stdout)['blockers']==[code]
    assert not n['evidence'].exists() and not n['state'].exists()


@pytest.mark.parametrize('args',[
    [],['unexpected','SYNTHETIC_SECRET'],['copy'],['verify','--manifest','/missing'],
    ['copy','--manifest','/missing','--manifest-sha256','A'*64],
    ['verify','--manifest','/missing','--manifest-sha256','bad-SYNTHETIC_SECRET'],
    ['inventory','--source-root','/missing','--force','SYNTHETIC_SECRET']])
def test_cli_invalid_arguments_and_pins_are_safe(args):
    result=cli(args)
    assert result.returncode==2 and not result.stderr
    report=json.loads(result.stdout)
    assert report['blockers'] and not report['copy_verified']


@pytest.mark.parametrize('subcommand',[None,'inventory','copy','verify'])
def test_cli_help_inert_no_optional_import_or_io(subcommand):
    result=cli(([subcommand] if subcommand else [])+['--help'])
    assert result.returncode==0 and not result.stderr
    assert 'usage:' in result.stdout


def test_cli_import_inert_without_db_or_files():
    code=GUARD[:GUARD.index('sys.argv=')]+r'''
class NoDependencies(importlib.abc.MetaPathFinder):
 def find_spec(self,fullname,path=None,target=None):
  if fullname.startswith(('sqlalchemy','startup_recovery.cutover_service','startup_recovery.cutover_db','startup_recovery.cutover_files')):
   raise AssertionError('dependency import during inert import')
sys.meta_path.insert(0,NoDependencies())
import startup_recovery.cutover
assert callable(startup_recovery.cutover.main)
'''
    result=subprocess.run([sys.executable,'-B','-c',code],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert not result.stdout


def test_cli_busy_exit_and_blocked_unknown_state(pg,native_inventory):
    n=native_inventory; pin,installed=transport(n,pg)
    with CopyTarget(n['state']) as destination,destination.exclusive():
        result=cli(['verify','--manifest',installed,'--manifest-sha256',pin],pg)
    assert result.returncode==3
    assert json.loads(result.stdout)['blockers']==['BUSY']
    (n['source']/'users.json').write_bytes(b'SYNTHETIC_SECRET')
    report=json.loads(cli(inventory_args(n),pg).stdout)
    assert 'UNKNOWN_AUTHORITY_STATE' in report['blockers']
    assert report['remaining_unknowns']==['UNKNOWN_STATE']
    result=cli(['copy','--manifest',report['manifest_path'],'--manifest-sha256',report['manifest_sha256']],pg)
    assert result.returncode==2 and not json.loads(result.stdout)['copy_verified']


def test_cli_unsafe_exception_never_printed(monkeypatch,capsys,tmp_path):
    module=importlib.import_module('startup_recovery.cutover')
    from startup_recovery import cutover_db
    def fail():
        print('SYNTHETIC_SECRET upstream stdout')
        print('SYNTHETIC_SECRET upstream stderr',file=sys.stderr)
        raise RuntimeError('SYNTHETIC_SECRET postgresql://credential@host/db')
    monkeypatch.setattr(cutover_db,'configured_engine',fail)
    result=module.main(['inventory','--source-root',str(tmp_path/'native'),'--state-root',str(tmp_path/'state'),
        '--scope',str(tmp_path/'scope.json'),'--evidence-root',str(tmp_path/'evidence')])
    output=capsys.readouterr()
    assert result==2 and not output.err and 'SYNTHETIC_SECRET' not in output.out
    assert json.loads(output.out)['blockers']==['CUTOVER_FAILED']


@pytest.mark.parametrize('protected',['source','state','binding'])
def test_review_inventory_overlap_rejected_before_provision(pg,native_inventory,protected):
    n=native_inventory
    if protected=='binding':
        root=n['external']/'candle_cache'
        root.mkdir(mode=0o700)
    else:
        root=n[protected]
    n['evidence']=root/'new-evidence'
    args=inventory_args(n)
    before=snapshot(n['source'].parent),snapshot_db(pg[0],pg[1])
    result=cli(args,pg)
    assert result.returncode==2 and json.loads(result.stdout)['blockers']==['ROOT_OVERLAP']
    assert (snapshot(n['source'].parent),snapshot_db(pg[0],pg[1]))==before


@pytest.mark.parametrize('alias',['evidence','state','nested-evidence','state-lock','state-generation'])
def test_review_physical_alias_rejected_before_any_source_write(pg,native_inventory,alias):
    n=native_inventory
    mount_source=n['source']
    if alias in ('nested-evidence','state-generation'):
        cache=n['source']/'cache'; cache.mkdir(mode=0o700)
        mount_source=cache/'nested'; mount_source.mkdir(mode=0o700)
        n['scope']['family_locations']['19']=[str(cache)]
    mount_target=(n['state']/'.cutover' if alias=='state-lock' else
                  n['state']/'.recovery-generations' if alias=='state-generation' else
                  n['state'] if alias=='state' else n['evidence'])
    if alias in ('state-lock','state-generation'):
        n['state'].mkdir(mode=0o700)
    mount_target.mkdir(mode=0o700)
    args=inventory_args(n)
    if alias.startswith('state'):
        initial=json.loads(cli(args,pg).stdout)
        args=['copy','--manifest',initial['manifest_path'],'--manifest-sha256',initial['manifest_sha256']]
    dsn=make_url(os.environ['VERIFIER_TEST_PG_DSN']).update_query_dict(
        {'options':'-csearch_path='+pg[1]}).render_as_string(hide_password=False)
    before=snapshot(n['source']),snapshot_db(pg[0],pg[1])
    shell='''
set -eu
mount --bind "$1" "$2"
shift 2
exec setpriv --reuid=501 --regid=1000 --clear-groups env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC PYTHONDONTWRITEBYTECODE=1 PYTHON_DOTENV_DISABLED=1 "$@"
'''
    child='''
import os,runpy,sys
os.environ['CUTOVER_DATABASE_URL']=sys.argv.pop(1)
sys.argv[0]='startup_recovery.cutover'
runpy.run_module('startup_recovery.cutover',run_name='__main__')
'''
    result=subprocess.run(['sudo','unshare','--mount','--propagation','private','--','sh','-c',shell,'alias-test',
        str(mount_source),str(mount_target),sys.executable,'-B','-c',child,dsn,*map(str,args)],
        capture_output=True,text=True)
    assert result.returncode==2,result.stderr
    assert json.loads(result.stdout)['blockers']==['ROOT_OVERLAP']
    assert (snapshot(n['source']),snapshot_db(pg[0],pg[1]))==before
