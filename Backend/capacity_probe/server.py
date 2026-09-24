"""Temporary full-API staging adapter, not a production entry point."""
import os
if os.environ.get('CAPACITY_STAGING')!='1':raise RuntimeError('Staging only')
if len(os.environ.get('CAPACITY_TOKEN',''))<32:raise RuntimeError('Strong staging token required')
import json,time,threading,types,sys,hmac
from pathlib import Path
root=Path(os.environ['CAPACITY_STATE_ROOT'])
from capacity_probe.instrumentation import Recorder
recorder=Recorder(root)
# Install deny-all order adapters before the API binds connector functions.
import ctrader_connector

def forbidden_order(*a,**k):raise PermissionError('STAGING_ORDER_SUBMISSION_DISABLED')
for name in ['place_market_order','modify_position_sltp','modify_position_stop_loss','close_position','connect_account','exchange_ctrader_authorization_code']:
    if hasattr(ctrader_connector,name):setattr(ctrader_connector,name,forbidden_order)
import closed_market_bootstrap
app=closed_market_bootstrap.app
import models
from db import Base,engine
Base.metadata.create_all(engine)
import api
api.LIVE_AUTO_TRADE_ENABLED['enabled']=False
from routes import strategy_simulator as routes
from ctrader_account_context import pinned_account,AccountIdentity
routes._actor=lambda *a,**k:{'email':'capacity@invalid.test','role':'admin'}
routes.pinned_account=lambda:pinned_account(AccountIdentity('capacity-isolated','demo'))
routes.get_ctrader_account_snapshot=lambda:{'balance':10000}
# Keep the normal simulator HTTP handlers, manager, child, serializer and ASGI
# response construction. Only authentication/account inputs are staging fixtures.
from services import strategy_fast_jobs as jobs
fault_state={'fault':None,'monotonic_calls':0}
real_popen=jobs.subprocess.Popen

def popen(*args,**kwargs):
    child=real_popen(*args,**kwargs);recorder.children.append(child)
    recorder.event('spawn',pid=child.pid);return child
jobs.subprocess.Popen=popen
real_reap=jobs._terminate_and_reap

def reap(child,*a,**k):
    try:return real_reap(child,*a,**k)
    finally:recorder.event('reaped',pid=child.pid,returncode=child.returncode)
jobs._terminate_and_reap=reap

def mono():
    value=time.monotonic()
    if threading.current_thread().name=='fast-job-manager' and fault_state['fault']=='timeout':
        fault_state['monotonic_calls']+=1
        if fault_state['monotonic_calls']>1:value+=601
    return value

def sleep(seconds):
    time.sleep(seconds)
    if threading.current_thread().name=='fast-job-manager' and fault_state['fault']=='manager':
        raise RuntimeError('injected manager failure')
jobs.time=types.SimpleNamespace(time=time.time,monotonic=mono,sleep=sleep)
from fastapi import Request,HTTPException
from fastapi.responses import FileResponse

@app.get('/probe/health')
def health():return {'ok':True,'pid':os.getpid()}

@app.get('/probe/snapshot')
def snapshot():
    return {'latest':recorder.latest,'sampled_peak':recorder.maximum,'events':recorder.events,'pid':os.getpid(),'python':sys.version,'live_children':[p.pid for p in recorder.children if p.poll() is None],'database':'isolated SQLite','broker_credentials':False,'order_submission':'blocked','api_routes':len(app.routes),'boot_time':recorder.start}

@app.post('/probe/control')
def control(payload:dict):
    if any(p.poll() is None for p in recorder.children):raise HTTPException(409,'Worker still active')
    value=payload.get('fault')
    if value not in (None,'calculation','serialization','manager','timeout'):raise HTTPException(400,'Unknown fault')
    fault_state.update(fault=value,monotonic_calls=0)
    (root/'fault.json').write_text(json.dumps({'fault':value}))
    recorder.label=payload.get('label','idle');recorder.event('control',**payload)
    return {'ok':True}

@app.post('/probe/mark')
def mark(payload:dict):
    recorder.label=payload.get('label',recorder.label);recorder.event('mark',**payload);return {'ok':True}

@app.get('/probe/evidence')
def evidence():return FileResponse(recorder.path,media_type='application/x-ndjson')

# Only the probe and simulator endpoints are exposed, behind a staging token.
class Gate:
    def __init__(self,app):self.app=app
    async def __call__(self,scope,receive,send):
        if scope['type']=='http':
            path=scope['path'];headers=dict(scope['headers'])
            valid=hmac.compare_digest(headers.get(b'authorization',b'').decode(),'Bearer '+os.environ['CAPACITY_TOKEN'])
            if path!='/probe/health' and (not valid or not (path.startswith('/probe/') or path.startswith('/strategy-simulator/') or path=='/strategy-lab/replay')):
                await send({'type':'http.response.start','status':403,'headers':[]})
                await send({'type':'http.response.body','body':b'Staging access only'})
                return
        result_request=scope['type']=='http' and scope.get('method')=='GET' and scope.get('path','').startswith('/strategy-simulator/fast-jobs/')
        if result_request:recorder.event('result_request_start')
        try:
            await self.app(scope,receive,send)
        finally:
            if result_request:recorder.event('result_response_complete')

if __name__=='__main__':
    import uvicorn
    uvicorn.run(Gate(app),host='0.0.0.0',port=int(os.environ.get('PORT','10000')),access_log=False)
