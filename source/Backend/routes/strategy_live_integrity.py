"""Opt-in selector-only admin diagnostic. Never calls an execution service."""
from typing import Literal
import os
import time
from fastapi import APIRouter,Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel,ConfigDict,Field
from live_integrity.executor import run,TIMEOUT_SECONDS,preload,shutdown

router=APIRouter()
router.add_event_handler('startup', preload)
router.add_event_handler('shutdown', shutdown)
_HEADERS={'Cache-Control':'no-store','Pragma':'no-cache'}


class Selectors(BaseModel):
    model_config=ConfigDict(extra='forbid')
    strategy_id: str=Field(min_length=1,max_length=64,pattern=r'^[A-Za-z0-9_-]+$')
    symbol: Literal['EURUSD','XAUUSD']


def runtime_snapshot():
    # Installed by the normal application. Never infer or enable LIVE state.
    return {'live_auto_enabled':False}


@router.post('/admin/strategy-live-integrity/verify',include_in_schema=False)
def verify(selectors:Selectors,request:Request):
    if os.getenv('STRATEGY_LIVE_INTEGRITY_VERIFY_ENABLED')!='1':
        return JSONResponse({'detail':'NOT_FOUND'},status_code=404,headers=_HEADERS)
    headers=request.headers.getlist('Authorization')
    header=headers[0] if len(headers)==1 else ''
    scheme='modern'
    if len(headers)>1:
        return JSONResponse({'detail':'ACCESS_DENIED'},status_code=401,headers=_HEADERS)
    if headers:
        if header.startswith('Bearer '):
            scheme='legacy-owner';token=header[len('Bearer '):]
        elif header.startswith('FlowSignalUser '):
            token=header[len('FlowSignalUser '):].strip()
        else:
            return JSONResponse({'detail':'ACCESS_DENIED'},status_code=401,headers=_HEADERS)
        if not token or any(c.isspace() for c in token):
            return JSONResponse({'detail':'ACCESS_DENIED'},status_code=401,headers=_HEADERS)
    else:
        token=request.cookies.get('flowsignal_session','')
    csrf=request.headers.get('X-FlowSignal-CSRF','')
    if not token:
        return JSONResponse({'detail':'ACCESS_DENIED'},status_code=401,headers=_HEADERS)
    if len(token)>2048 or (scheme=='modern' and (not csrf or len(csrf)>128)):
        return JSONResponse({'detail':'ACCESS_DENIED'},status_code=403,headers=_HEADERS)
    if scheme=='legacy-owner':csrf=''
    try:
        deadline=time.monotonic()+TIMEOUT_SECONDS
        before=runtime_snapshot()
        if time.monotonic()>=deadline:raise TimeoutError()
        result=run(dict(strategy_id=selectors.strategy_id,symbol=selectors.symbol,token=token,csrf=csrf,auth_scheme=scheme,runtime=before,deadline=deadline))
        if before!=runtime_snapshot():
            result=dict(status=200,body=dict(decision='WOULD_BLOCK',REAL_ORDER_DISPATCH_AVAILABLE=False,block_reasons=['RUNTIME_IDENTITY_CHANGED']))
        if time.monotonic()>=deadline:raise TimeoutError()
    except TimeoutError:
        result=dict(status=504,body=dict(decision='WOULD_BLOCK',REAL_ORDER_DISPATCH_AVAILABLE=False,block_reasons=['DIAGNOSTIC_TIMEOUT']))
    except Exception:
        # No arbitrary parent-side exception text, request credentials or cache
        # contents enter responses/logs. Child execution has its own cleanup.
        result=dict(status=503,body=dict(decision='WOULD_BLOCK',REAL_ORDER_DISPATCH_AVAILABLE=False,block_reasons=['DIAGNOSTIC_UNAVAILABLE']))
    return JSONResponse(result['body'],status_code=result['status'],headers=_HEADERS)
