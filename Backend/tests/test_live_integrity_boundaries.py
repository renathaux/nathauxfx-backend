import importlib
import pytest


def reader():
    try:
        return importlib.import_module('live_integrity.ctrader_reader')
    except ModuleNotFoundError:
        pytest.fail('Restricted transport missing')


@pytest.mark.parametrize('kind',[2106,2107,2108,2109,2110,2173,9999])
def test_mutating_protocol_rejected_before_wire(kind):
    mod = reader()
    with pytest.raises(ValueError,match='READ_ONLY'):
        mod.permit(kind,{})


def test_only_explicit_read_requests_allowed():
    mod = reader()
    for kind in (2100,2102,2112,2114,2116,2121,2124,2127):
        mod.permit(kind,{})


@pytest.mark.parametrize('failure',['success','timeout','auth','protocol','exception'])
def test_dedicated_socket_cleanup_and_secret_free_errors(monkeypatch,failure):
    import time,json
    from websockets.sync import client
    mod=reader()
    class Socket:
        closed=False
        sent=[]
        def send(self,value):self.sent.append(json.loads(value))
        def recv(self,timeout):
            last=self.sent[-1]
            if failure=='timeout':raise TimeoutError('fixture-secret')
            if failure=='auth':return json.dumps({'payloadType':2142,'payload':{'description':'fixture-secret'}})
            if failure=='protocol':return '{malformed'
            if failure=='exception':raise RuntimeError('fixture-secret')
            return json.dumps({'payloadType':mod.READ_RESPONSES[last['payloadType']],'clientMsgId':last['clientMsgId'],'payload':{'ctidTraderAccountId':7}})
        def close(self):self.closed=True
    wire=Socket()
    monkeypatch.setattr(client,'connect',lambda *a,**k:wire)
    broker=mod.Reader('7','demo',dict(client_id='fixture',client_secret='fixture-secret',access_token='fixture-secret'),time.monotonic()+2)
    if failure=='success':
        with broker:pass
    else:
        with pytest.raises(Exception):
            with broker:pass
    assert wire.closed
    assert broker._credentials is None
    assert all(m['payloadType'] in (2100,2102) for m in wire.sent)


def test_hard_timeout_kills_worker_not_only_response(monkeypatch,tmp_path):
    import subprocess,sys,time
    from live_integrity import executor
    script=tmp_path/'sleep.py';script.write_text('import time\ntime.sleep(60)\n')
    real_popen=subprocess.Popen
    children=[]
    def child(*args,**kwargs):
        p=real_popen([sys.executable,str(script)],**kwargs);children.append(p);return p
    monkeypatch.setattr(executor.subprocess,'Popen',child)
    monkeypatch.setattr(executor,'TIMEOUT_SECONDS',.2)
    started=time.monotonic()
    result=executor.run({'fixture':True})
    assert result['status']==504 and result['body']['block_reasons']==['DIAGNOSTIC_TIMEOUT']
    assert time.monotonic()-started<2
    assert all(p.poll() is not None for p in children)
    assert executor._LOCAL_FLIGHT.acquire(False)
    executor._LOCAL_FLIGHT.release()


def test_entire_worker_dependency_graph_blocks_dispatch_imports():
    import subprocess,sys
    from pathlib import Path
    worker=Path.cwd()/'live_integrity'/'worker.py'
    code="import runpy,sys; runpy.run_path(sys.argv[1],run_name='inspect_only'); assert 'api' not in sys.modules; assert 'ctrader_connector' not in sys.modules; import ctrader_connector"
    result=subprocess.run([sys.executable,'-I','-B','-c',code,str(worker)],capture_output=True,text=True)
    assert result.returncode!=0
    assert 'DIAGNOSTIC_EXECUTION_IMPORT_FORBIDDEN' in result.stderr


@pytest.mark.parametrize('fault',[None,'dirty','revision'])
def test_build_identity_requires_clean_matching_commit(monkeypatch,fault):
    from live_integrity import build_identity
    sha='a'*40; tree='b'*40
    def git(args,**kwargs):
        if 'status' in args:return ' M Backend/api.py' if fault=='dirty' else ''
        return tree if args[-1]=='HEAD^{tree}' else sha
    monkeypatch.setattr(build_identity.subprocess,'check_output',git)
    monkeypatch.setenv('RENDER_GIT_COMMIT','c'*40 if fault=='revision' else sha)
    if fault:
        with pytest.raises(ValueError,match='BUILD_IDENTITY_UNVERIFIED'):build_identity.capture()
    else:
        result=build_identity.capture()
        assert result['backend_git_sha']==sha and result['source_tree']==tree and result['clean_source']
        assert len(result['build_identity'])==64


@pytest.mark.parametrize('field',['position','order'])
@pytest.mark.parametrize('malformed',[None,{},'',0,False])
def test_explicit_malformed_exposure_is_not_empty(monkeypatch,field,malformed):
    import time
    mod=reader(); broker=mod.Reader('7','demo',{},time.monotonic()+2)
    def request(kind,body):
        if kind==2121:return {'trader':{'ctidTraderAccountId':7,'moneyDigits':2,'balance':1000000}}
        assert kind==2124
        return {field:malformed}
    monkeypatch.setattr(broker,'_request',request)
    with pytest.raises(ValueError,match='EXPOSURE_UNVERIFIED'):broker.account_state()


def test_expired_parent_budget_cannot_start_worker(monkeypatch):
    import time
    from live_integrity import executor
    def forbidden(*args,**kwargs):pytest.fail('Expired request spawned a worker')
    monkeypatch.setattr(executor.subprocess,'Popen',forbidden)
    result=executor.run({'deadline':time.monotonic()-1})
    assert result['status']==504
    assert result['body']['block_reasons']==['DIAGNOSTIC_TIMEOUT']


def test_settings_snapshot_is_bounded_read_only_and_content_bound(tmp_path):
    import json
    from live_integrity.risk_snapshot import read
    path=tmp_path/'settings.json'
    path.write_text(json.dumps({'risk':{'maxDailyLoss':None,'maxWeeklyLoss':None}}))
    raw=path.read_bytes(); stamp=path.stat().st_mtime_ns
    first=read(str(path))
    assert first['values']=={'maxDailyLoss':None,'maxWeeklyLoss':None}
    assert path.read_bytes()==raw and path.stat().st_mtime_ns==stamp
    path.write_text(json.dumps({'risk':{'maxDailyLoss':100,'maxWeeklyLoss':None}}))
    assert read(str(path))['identity']!=first['identity']


@pytest.mark.parametrize('fault',['missing','malformed','missing_field','oversize','fifo'])
def test_settings_snapshot_unavailable_never_invents_disabled_limits(tmp_path,fault):
    import os
    from live_integrity.risk_snapshot import read
    path=tmp_path/'settings.json'
    if fault=='malformed':path.write_text('secret-not-json')
    if fault=='missing_field':path.write_text('{"risk":{"maxDailyLoss":null}}')
    if fault=='oversize':path.write_text(' '*65537)
    if fault=='fifo':os.mkfifo(path)
    with pytest.raises(ValueError,match='^RISK_SETTINGS_READ_ONLY_UNAVAILABLE$'):
        read(str(path))
