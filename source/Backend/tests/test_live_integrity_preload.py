"""Readiness, isolation and bounded work, using real disposable child processes."""
import json
import subprocess
import sys
import time
from pathlib import Path
import selectors

import pytest


@pytest.fixture
def slot(monkeypatch, tmp_path):
    from live_integrity import executor
    if hasattr(executor, 'shutdown'):
        executor.shutdown()
    monkeypatch.setenv('STRATEGY_LIVE_INTEGRITY_VERIFY_ENABLED', '1')
    real_popen = subprocess.Popen

    def install(body):
        script = tmp_path / 'fixture_worker.py'
        script.write_text(body)
        children = []
        def spawn(*args, **kwargs):
            child = real_popen([sys.executable, '-B', str(script)], **kwargs)
            children.append(child)
            return child
        monkeypatch.setattr(executor.subprocess, 'Popen', spawn)
        return children
    yield executor, install
    if hasattr(executor, 'shutdown'):
        executor.shutdown()


def test_cold_import_cost_precedes_request_deadline_and_child_is_single_use(slot, monkeypatch):
    executor, install = slot
    assert callable(getattr(executor, 'preload', None)), 'No startup preload lifecycle'
    children = install('''import json,sys,time,os
time.sleep(.3)
print('READY', flush=True)
payload=json.loads(sys.stdin.buffer.readline())
print(json.dumps({'status':200,'body':{'REAL_ORDER_DISPATCH_AVAILABLE':False,'pid':os.getpid(),'value':payload['value']}}))
''')
    monkeypatch.setattr(executor, 'TIMEOUT_SECONDS', .2)
    assert executor.preload()
    first = executor.run({'value': 'first'})
    assert first['status'] == 200
    assert first['body']['value'] == 'first'
    # The next request cannot cold-start or reuse an authenticated child.
    second = executor.run({'value': 'second'})
    assert second['status'] == 503
    assert second['body']['block_reasons'] == ['DIAGNOSTIC_UNAVAILABLE']
    assert executor.preload()
    third = executor.run({'value': 'third'})
    assert third['status'] == 200
    assert third['body']['pid'] != first['body']['pid']
    assert children[0].poll() is not None


def test_substantive_work_is_still_killed_at_deadline(slot, monkeypatch):
    executor, install = slot
    assert callable(getattr(executor, 'preload', None)), 'No startup preload lifecycle'
    children = install("import sys,time\nprint('READY',flush=True)\nsys.stdin.buffer.readline()\ntime.sleep(60)\n")
    assert executor.preload()
    monkeypatch.setattr(executor, 'TIMEOUT_SECONDS', .15)
    started = time.monotonic()
    result = executor.run({})
    assert result['status'] == 504
    assert result['body']['block_reasons'] == ['DIAGNOSTIC_TIMEOUT']
    assert time.monotonic() - started < 2
    assert children[0].poll() is not None


def test_shutdown_reaps_idle_worker_and_does_not_cold_spawn(slot):
    executor, install = slot
    assert callable(getattr(executor, 'preload', None)), 'No startup preload lifecycle'
    children = install("import sys\nprint('READY',flush=True)\nsys.stdin.buffer.readline()\n")
    assert executor.preload()
    executor.shutdown()
    assert children[0].poll() is not None
    assert executor.run({})['status'] == 503
    assert len(children) == 1


def test_disabled_preload_has_no_process(slot, monkeypatch):
    executor, install = slot
    assert callable(getattr(executor, 'preload', None)), 'No startup preload lifecycle'
    children = install("raise AssertionError('must not run')")
    monkeypatch.delenv('STRATEGY_LIVE_INTEGRITY_VERIFY_ENABLED')
    assert not executor.preload()
    assert children == []


def test_real_cold_preload_cannot_connect_create_engine_or_write_state(monkeypatch):
    worker = Path.cwd() / 'live_integrity' / 'worker.py'
    code = '''import sys,os,runpy
def audit(event,args):
    if event in {'socket.connect','socket.bind','subprocess.Popen','os.system','os.mkdir','os.remove','os.rename'}:
        raise AssertionError('forbidden side effect: '+event)
    if event == 'open':
        mode,flags=args[1],args[2]
        if (isinstance(mode,str) and any(c in mode for c in 'wax+')) or (isinstance(flags,int) and flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND)):
            raise AssertionError('forbidden write')
sys.addaudithook(audit)
import sqlalchemy
def forbidden(*args,**kwargs):raise AssertionError('engine creation during preload')
sqlalchemy.create_engine=forbidden
path=sys.argv[1];sys.argv=[path,'--preload']
runpy.run_path(path,run_name='__main__')
'''
    monkeypatch.setenv('STRATEGY_LIVE_INTEGRITY_VERIFY_ENABLED', '1')
    with subprocess.Popen([sys.executable, '-I', '-B', '-c', code, str(worker)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as child:
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdout, selectors.EVENT_READ)
                assert selector.select(30), 'Import did not finish'
            assert child.stdout.readline() == b'READY\n'
            assert child.poll() is None  # Waiting for a request; no DB/broker work.
        finally:
            child.kill()
            child.communicate()


def test_failed_startup_preload_reaps_without_a_request(slot, monkeypatch):
    executor, install = slot
    children = install('import time\ntime.sleep(60)\n')
    monkeypatch.setattr(executor, 'PRELOAD_TIMEOUT_SECONDS', .05)
    assert not executor.preload()
    assert children[0].poll() is not None


def test_replacement_preload_is_bounded_without_future_requests(slot, monkeypatch):
    executor, install = slot
    children = install("import sys,json\nprint('READY',flush=True)\nsys.stdin.buffer.readline()\nprint(json.dumps({'status':200,'body':{'REAL_ORDER_DISPATCH_AVAILABLE':False}}))\n")
    assert executor.preload()
    replacement = install('import time\ntime.sleep(60)\n')
    monkeypatch.setattr(executor, 'PRELOAD_TIMEOUT_SECONDS', .05)
    assert executor.run({})['status'] == 200
    limit = time.monotonic() + 2
    while replacement[0].poll() is None and time.monotonic() < limit:
        time.sleep(.01)
    assert replacement[0].poll() is not None


def test_dead_idle_worker_fails_closed_and_next_startup_can_recover(slot):
    executor, install = slot
    children = install("import sys,json\nprint('READY',flush=True)\nsys.stdin.buffer.readline()\nprint(json.dumps({'status':200,'body':{'REAL_ORDER_DISPATCH_AVAILABLE':False}}))\n")
    assert executor.preload()
    children[0].kill()
    children[0].wait()
    assert executor.run({})['status'] == 503
    assert executor.preload()
    assert executor.run({})['status'] == 200


def test_router_lifecycle_preloads_and_cleans_up_single_use_child(slot):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes.strategy_live_integrity import router
    executor, install = slot
    children = install("import sys,json\nprint('READY',flush=True)\np=json.loads(sys.stdin.buffer.readline())\nprint(json.dumps({'status':200,'body':{'REAL_ORDER_DISPATCH_AVAILABLE':False,'strategy':p['strategy_id']}}))\n")
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        assert len(children) == 1 and children[0].poll() is None
        response = client.post('/admin/strategy-live-integrity/verify',
            headers={'Authorization':'Bearer fixture'}, json={'strategy_id':'test','symbol':'XAUUSD'})
        assert response.status_code == 200
        assert response.json() == {'REAL_ORDER_DISPATCH_AVAILABLE':False,'strategy':'test'}
    assert all(child.poll() is not None for child in children)


def test_real_cold_and_preloaded_workers_enforce_same_expired_deadline(monkeypatch):
    monkeypatch.setenv('STRATEGY_LIVE_INTEGRITY_VERIFY_ENABLED','1')
    worker = str(Path.cwd() / 'live_integrity' / 'worker.py')
    payload = json.dumps({'deadline':time.monotonic()-1}).encode() + b'\n'
    results = []
    for args in ([], ['--preload']):
        child = subprocess.Popen([sys.executable,'-I','-B',worker,*args],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:
            if args:
                with selectors.DefaultSelector() as selector:
                    selector.register(child.stdout,selectors.EVENT_READ)
                    assert selector.select(30)
                assert child.stdout.readline() == b'READY\n'
            output,_ = child.communicate(payload,timeout=30)
            assert child.returncode == 0
            results.append(json.loads(output))
        finally:
            if child.poll() is None:child.kill()
            child.communicate()
    assert results[0] == results[1]
    assert results[0]['status'] == 504
    assert results[0]['body']['block_reasons'] == ['DIAGNOSTIC_TIMEOUT']
