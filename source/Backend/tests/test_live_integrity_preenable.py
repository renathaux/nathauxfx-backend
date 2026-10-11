"""Pre-enable diagnostics require real OFF evidence, never execution authority."""
import copy
import json
import time

import pytest
from sqlalchemy import text

from test_hosted_live_integrity_integration import current_case, database_digest
from test_live_integrity_read_store import pg
from test_live_integrity_admission import evidence


def set_switch(engine, value):
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM runtime_settings WHERE setting_name='live_auto_trade_enabled'"))
        if value is not None:
            conn.execute(text("INSERT INTO runtime_settings(setting_name,setting_value,updated_at,updated_by) VALUES ('live_auto_trade_enabled',:value,NOW(),'fixture')"), {'value': value})
        conn.execute(text("INSERT INTO runtime_settings(setting_name,setting_value,updated_at,updated_by) VALUES ('news_trading_mode','OFF',NOW(),'fixture') ON CONFLICT(setting_name) DO UPDATE SET setting_value='OFF'"))


def off_runtime(tmp_path):
    runtime = evidence()
    runtime['live_auto_enabled'] = False
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps({'risk': runtime.pop('risk_settings')}))
    runtime['risk_settings_path'] = str(path)
    return runtime


def invoke(c, runtime):
    from live_integrity.verifier import verify
    from live_integrity.read_store import read_only
    with read_only(c['pg']) as conn:
        return verify(conn,c['owner'],c['strategy_id'],'EURUSD',runtime,time.monotonic()+10,reader_factory=c['Broker'])


def test_explicit_off_allows_only_detached_preenable_snapshot(current_case,tmp_path,monkeypatch):
    import api
    c=current_case; set_switch(c['pg'],'false'); runtime=off_runtime(tmp_path)
    monkeypatch.setattr(api,'LIVE_AUTO_TRADE_ENABLED',{'enabled':False})
    original=copy.deepcopy(runtime); before=database_digest(c['pg'])
    files={p.name:(p.read_bytes(),p.stat().st_mtime_ns) for p in tmp_path.iterdir()}
    result=invoke(c,runtime)
    assert result['decision']==result['final_integrity_authorization']=='WOULD_ALLOW'
    assert result['decision_scope']=='PRE_ENABLE_SNAPSHOT_INTEGRITY_ONLY'
    assert result['live_auto_currently_enabled'] is False
    assert result['live_auto_simulated_for_pre_enable_check'] is True
    assert result['REAL_ORDER_DISPATCH_AVAILABLE'] is False
    assert result['setup_persisted'] is False
    for field in ('durable_claim','atomic_dispatch','broker_acceptance'):
        assert result[field]=='NOT_EXERCISED_READ_ONLY'
    assert runtime==original and api.LIVE_AUTO_TRADE_ENABLED=={'enabled':False}
    assert database_digest(c['pg'])==before
    assert {p.name:(p.read_bytes(),p.stat().st_mtime_ns) for p in tmp_path.iterdir()}==files
    # The fixture supplies only read operations, never an order transport.
    assert not hasattr(c['Broker'],'send_order')


@pytest.mark.parametrize('runtime_value,db_value,reason',[
    (True,'false','LIVE_AUTO_ALREADY_ENABLED_DURING_PREENABLE_VERIFY'),
    (False,'true','LIVE_AUTO_ALREADY_ENABLED_DURING_PREENABLE_VERIFY'),
    (True,'true','LIVE_AUTO_ALREADY_ENABLED_DURING_PREENABLE_VERIFY'),
    (None,'false','LIVE_AUTO_STATE_UNVERIFIED_DURING_PREENABLE_VERIFY'),
    (0,'false','LIVE_AUTO_STATE_UNVERIFIED_DURING_PREENABLE_VERIFY'),
    ('false','false','LIVE_AUTO_STATE_UNVERIFIED_DURING_PREENABLE_VERIFY'),
    (False,None,'LIVE_AUTO_STATE_UNVERIFIED_DURING_PREENABLE_VERIFY'),
    (False,'','LIVE_AUTO_STATE_UNVERIFIED_DURING_PREENABLE_VERIFY'),
    (False,'garbage','LIVE_AUTO_STATE_UNVERIFIED_DURING_PREENABLE_VERIFY'),
])
def test_on_or_unknown_cannot_reach_broker(current_case,runtime_value,db_value,reason):
    c=current_case;set_switch(c['pg'],db_value)
    before=database_digest(c['pg']);runtime={} if runtime_value is None else {'live_auto_enabled':runtime_value}
    with pytest.raises(ValueError,match='^'+reason+'$'):invoke(c,runtime)
    assert database_digest(c['pg'])==before
    assert c['state']['opened']==0


@pytest.mark.parametrize('value,expected',[(None,None),('',None),('garbage',None),('false',False),('0',False),('off',False),('no',False),('true',True),('1',True),('yes',True),('on',True)])
def test_durable_switch_is_tristate(current_case,value,expected):
    from live_integrity.snapshots import admission_state
    from live_integrity.read_store import read_only
    set_switch(current_case['pg'],value)
    with read_only(current_case['pg']) as conn:
        assert admission_state(conn)['live_enabled'] is expected


@pytest.mark.parametrize('fault,reason',[
    ('risk','LOSS_HISTORY_READ_ONLY_UNAVAILABLE'),
    ('news','NEWS_READ_ONLY_EVIDENCE_UNAVAILABLE'),
    ('feed','WAIT_STALE_MARKET_FEED'),
    ('fundamentals','FUNDAMENTAL_READ_ONLY_EVIDENCE_UNAVAILABLE'),
    ('active','LOCAL_EXECUTION_STATE_BLOCKED'),
    ('inflight','LOCAL_EXECUTION_STATE_BLOCKED'),
])
def test_preenable_does_not_simulate_other_admission_evidence(current_case,tmp_path,fault,reason):
    c=current_case;set_switch(c['pg'],'false');runtime=off_runtime(tmp_path)
    if fault=='risk':(tmp_path/'settings.json').write_text(json.dumps({'risk':{'maxDailyLoss':100,'maxWeeklyLoss':None}}))
    if fault=='news':
        with c['pg'].begin() as conn:conn.execute(text("UPDATE runtime_settings SET setting_value='BLOCK_ONLY' WHERE setting_name='news_trading_mode'"))
    if fault=='feed':runtime['health_fetch_times']['EURUSD:5min']=0
    if fault=='fundamentals':runtime['fundamentals']={}
    if fault=='active':runtime['active_orders']=True
    if fault=='inflight':runtime['inflight']=True
    result=invoke(c,runtime)
    assert result['decision']=='WOULD_BLOCK' and result['block_reasons']==[reason]
    assert runtime['live_auto_enabled'] is False


@pytest.mark.parametrize('field',['positions','orders'])
def test_existing_broker_exposure_blocks_preenable(current_case,tmp_path,monkeypatch,field):
    c=current_case;set_switch(c['pg'],'false');runtime=off_runtime(tmp_path)
    state=dict(balance='10000',positions=[],orders=[]);state[field]=[{'id':'synthetic'}]
    monkeypatch.setattr(c['Broker'],'account_state',lambda self:copy.deepcopy(state))
    with pytest.raises(ValueError,match='EXISTING_EXPOSURE_REQUIRES_EXECUTION_STATE'):
        invoke(c,runtime)


@pytest.mark.parametrize('changed',['true','invalid'])
def test_durable_switch_change_during_snapshot_blocks(current_case,tmp_path,monkeypatch,changed):
    c=current_case;set_switch(c['pg'],'false');runtime=off_runtime(tmp_path)
    original=c['Broker'].quote
    def quote(self,record):
        # Concurrent operator change in fixture, never a diagnostic write.
        with c['pg'].begin() as conn:
            conn.execute(text("UPDATE runtime_settings SET setting_value=:v WHERE setting_name='live_auto_trade_enabled'"),{'v':changed})
        return original(self,record)
    monkeypatch.setattr(c['Broker'],'quote',quote)
    with pytest.raises(ValueError,match='RUNTIME_IDENTITY_CHANGED'):invoke(c,runtime)


def test_parent_runtime_switch_change_discards_allow(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes import strategy_live_integrity as route
    monkeypatch.setenv('STRATEGY_LIVE_INTEGRITY_VERIFY_ENABLED','1')
    switch={'live_auto_enabled':False}
    monkeypatch.setattr(route,'runtime_snapshot',lambda:copy.deepcopy(switch))
    def child(payload):
        switch['live_auto_enabled']=True
        return dict(status=200,body=dict(decision='WOULD_ALLOW',REAL_ORDER_DISPATCH_AVAILABLE=False))
    monkeypatch.setattr(route,'run',child)
    app=FastAPI();app.include_router(route.router)
    with TestClient(app) as client:
        result=client.post('/admin/strategy-live-integrity/verify',json={'strategy_id':'s','symbol':'EURUSD'},headers={'Authorization':'FlowSignalUser fixture','X-FlowSignal-CSRF':'fixture'})
    assert result.json()['decision']=='WOULD_BLOCK'
    assert result.json()['block_reasons']==['RUNTIME_IDENTITY_CHANGED']
    assert result.json()['REAL_ORDER_DISPATCH_AVAILABLE'] is False


@pytest.mark.parametrize('reason',[
    'LIVE_AUTO_ALREADY_ENABLED_DURING_PREENABLE_VERIFY',
    'LIVE_AUTO_STATE_UNVERIFIED_DURING_PREENABLE_VERIFY',
])
def test_worker_returns_specific_nonexecuting_preenable_block(reason):
    import subprocess
    import sys
    from pathlib import Path
    # Isolate the real worker import denylist. Stub its external I/O only;
    # exercise main()'s actual response/error sanitization boundary.
    script='''import contextlib,io,json,os,runpy,sys,time
w=runpy.run_path(sys.argv[1],run_name='test_worker')
g=w['main'].__globals__
class Engine:
 def dispose(self):pass
g['create_engine']=lambda *a,**k:Engine()
g['read_only']=lambda e:contextlib.nullcontext(None)
g['single_flight']=lambda c:contextlib.nullcontext()
g['read_admin']=lambda *a:'synthetic-owner'
g['capture']=lambda:{'test_only':True}
def blocked(*a):raise ValueError(sys.argv[2])
g['verify']=blocked
os.environ['STRATEGY_LIVE_INTEGRITY_VERIFY_ENABLED']='1'
os.environ['DATABASE_URL']='postgresql://synthetic.invalid/unused'
payload=dict(deadline=time.monotonic()+10,token='synthetic',csrf='synthetic',strategy_id='s',symbol='EURUSD',runtime={'live_auto_enabled':False})
sys.stdin=io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode()))
print(json.dumps(w['main']()))
'''
    result=subprocess.run([sys.executable,'-I','-B','-c',script,str(Path.cwd()/'live_integrity/worker.py'),reason],capture_output=True,text=True,check=True)
    response=json.loads(result.stdout)
    assert response['status']==200
    assert response['body']['decision']=='WOULD_BLOCK'
    assert response['body']['block_reasons']==[reason]
    assert response['body']['REAL_ORDER_DISPATCH_AVAILABLE'] is False


@pytest.mark.parametrize('shared,expected',[
    ({'enabled':False},False),({'enabled':True},True),({},None),
    ({'enabled':None},None),({'enabled':0},None),({'enabled':'false'},None),
    (None,None),
])
def test_real_parent_capture_preserves_explicit_boolean_only(monkeypatch,shared,expected):
    import api
    monkeypatch.setattr(api,'LIVE_AUTO_TRADE_ENABLED',copy.deepcopy(shared))
    before=copy.deepcopy(api.LIVE_AUTO_TRADE_ENABLED)
    assert api.capture_integrity_runtime()['live_auto_enabled'] is expected
    assert api.LIVE_AUTO_TRADE_ENABLED==before
