"""Bounded child process, not a timed-out background thread or shared worker."""
import json
from pathlib import Path
import subprocess
import sys
import time
from threading import Lock

_LOCAL_FLIGHT = Lock()
TIMEOUT_SECONDS = 10


def run(payload):
    if not _LOCAL_FLIGHT.acquire(blocking=False):
        return dict(status=409,body=dict(decision='WOULD_BLOCK',REAL_ORDER_DISPATCH_AVAILABLE=False,block_reasons=['DIAGNOSTIC_BUSY']))
    process = None
    try:
        deadline=min(float(payload.get('deadline',time.monotonic()+TIMEOUT_SECONDS)),time.monotonic()+TIMEOUT_SECONDS)
        payload = dict(payload,deadline=deadline)
        if time.monotonic()>=deadline:
            return dict(status=504,body=dict(decision='WOULD_BLOCK',REAL_ORDER_DISPATCH_AVAILABLE=False,block_reasons=['DIAGNOSTIC_TIMEOUT']))
        process = subprocess.Popen([sys.executable,'-I','-B',str(Path(__file__).with_name('worker.py'))],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
        try:
            stdout,_ = process.communicate(json.dumps(payload).encode()+b'\n',timeout=max(0,deadline-time.monotonic()))
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            return dict(status=504,body=dict(decision='WOULD_BLOCK',REAL_ORDER_DISPATCH_AVAILABLE=False,block_reasons=['DIAGNOSTIC_TIMEOUT']))
        if process.returncode or len(stdout)>65536:
            raise ValueError()
        result=json.loads(stdout)
        if result['body']['REAL_ORDER_DISPATCH_AVAILABLE'] is not False:
            raise ValueError()
        return result
    except Exception:
        return dict(status=503,body=dict(decision='WOULD_BLOCK',REAL_ORDER_DISPATCH_AVAILABLE=False,block_reasons=['DIAGNOSTIC_UNAVAILABLE']))
    finally:
        if process is not None and process.poll() is None:
            process.kill(); process.communicate()
        _LOCAL_FLIGHT.release()
