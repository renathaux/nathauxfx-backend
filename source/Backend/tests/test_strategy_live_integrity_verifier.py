"""Route never executes inline trading code and defaults closed."""
import importlib
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def route():
    try:
        return importlib.import_module('routes.strategy_live_integrity')
    except ModuleNotFoundError:
        pytest.fail('Read-only admin verifier route missing')


@pytest.mark.parametrize('flag',[None,'0'])
def test_flag_off_no_worker_or_broker(monkeypatch,flag):
    mod = route()
    monkeypatch.delenv('STRATEGY_LIVE_INTEGRITY_VERIFY_ENABLED',raising=False)
    if flag is not None:monkeypatch.setenv('STRATEGY_LIVE_INTEGRITY_VERIFY_ENABLED',flag)
    def forbidden(*args):pytest.fail('Disabled route reached worker')
    monkeypatch.setattr(mod,'run',forbidden)
    app = FastAPI(); app.include_router(mod.router)
    with TestClient(app) as client:
        r = client.post('/admin/strategy-live-integrity/verify',json={'strategy_id':'s','symbol':'EURUSD'})
    assert r.status_code == 404
    assert r.headers['cache-control'] == 'no-store'


def test_no_session_rejected_without_worker(monkeypatch):
    mod = route()
    monkeypatch.setenv('STRATEGY_LIVE_INTEGRITY_VERIFY_ENABLED','1')
    app = FastAPI(); app.include_router(mod.router)
    with TestClient(app) as client:
        r = client.post('/admin/strategy-live-integrity/verify',json={'strategy_id':'s','symbol':'EURUSD'})
    assert r.status_code == 401


@pytest.mark.parametrize('extra',[{'owner':'foreign'},{'account_id':'8'},{'entry':1.1},{'dry_run':False}])
def test_only_selectors_accepted(monkeypatch,extra):
    mod = route()
    monkeypatch.setenv('STRATEGY_LIVE_INTEGRITY_VERIFY_ENABLED','1')
    app = FastAPI(); app.include_router(mod.router)
    with TestClient(app) as client:
        r = client.post('/admin/strategy-live-integrity/verify',json=dict(strategy_id='s',symbol='EURUSD',**extra))
    assert r.status_code in (401,422)


def test_runtime_capture_error_is_sanitized_and_never_starts_worker(monkeypatch):
    mod=route(); monkeypatch.setenv('STRATEGY_LIVE_INTEGRITY_VERIFY_ENABLED','1')
    def broken():raise ValueError('fixture-secret-must-not-be-returned')
    monkeypatch.setattr(mod,'runtime_snapshot',broken)
    def forbidden(*args):pytest.fail('Failed snapshot reached worker')
    monkeypatch.setattr(mod,'run',forbidden)
    app=FastAPI();app.include_router(mod.router)
    with TestClient(app,raise_server_exceptions=False) as client:
        result=client.post('/admin/strategy-live-integrity/verify',json={'strategy_id':'s','symbol':'EURUSD'},headers={'Authorization':'FlowSignalUser fixture','X-FlowSignal-CSRF':'fixture'})
    assert result.status_code==503
    assert result.json()['block_reasons']==['DIAGNOSTIC_UNAVAILABLE']
    assert 'fixture-secret' not in result.text
    assert result.headers['cache-control']=='no-store'


@pytest.mark.parametrize('stage',['before','after'])
def test_parent_snapshots_share_one_deadline_without_budget_reset(monkeypatch,stage):
    mod=route(); monkeypatch.setenv('STRATEGY_LIVE_INTEGRITY_VERIFY_ENABLED','1')
    clock=[100.0]; captures=[]; calls=[]
    monkeypatch.setattr(mod.time,'monotonic',lambda:clock[0])
    def capture():
        captures.append(1)
        if stage=='before' or len(captures)==2:clock[0]=111.0
        return {'live_auto_enabled':True}
    def child(payload):
        calls.append(payload)
        assert payload['deadline']==110.0
        return dict(status=200,body=dict(decision='WOULD_ALLOW',REAL_ORDER_DISPATCH_AVAILABLE=False))
    monkeypatch.setattr(mod,'runtime_snapshot',capture)
    monkeypatch.setattr(mod,'run',child)
    app=FastAPI();app.include_router(mod.router)
    with TestClient(app) as client:
        result=client.post('/admin/strategy-live-integrity/verify',json={'strategy_id':'s','symbol':'EURUSD'},headers={'Authorization':'FlowSignalUser fixture','X-FlowSignal-CSRF':'fixture'})
    assert result.status_code==504
    assert result.json()['block_reasons']==['DIAGNOSTIC_TIMEOUT']
    assert len(calls)==(0 if stage=='before' else 1)
