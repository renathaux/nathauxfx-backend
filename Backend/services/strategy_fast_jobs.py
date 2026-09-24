"""Bounded, owned FAST jobs for the existing single-process Render service.

A separate Python subprocess isolates CPU work from the LIVE HTTP process.
Job files are ephemeral, private, and bounded; no candles or jobs go to Neon.
A service restart marks interrupted jobs failed; completed results expire.
"""
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

FAST_JOB_CONCURRENCY = 1  # One serial manager; never start overlapping workers.
TERMINAL={'COMPLETED','FAILED','CANCELLED'}

def write_json(path,value):
    temporary=path.with_suffix('.tmp-'+uuid.uuid4().hex)
    try:
        with temporary.open('w') as stream:
            json.dump(value,stream,allow_nan=False)
        os.replace(temporary,path)
    finally:
        temporary.unlink(missing_ok=True)

def read_json(path):
    return json.loads(path.read_text())

class JobNotFound(Exception):pass
class JobBusy(Exception):pass

class FastJobs:
    def __init__(self, root, *, start_worker=True, ttl=3600, max_jobs=20):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.lock=threading.RLock();self.start_worker=start_worker;self.ttl=ttl;self.max_jobs=max_jobs
        self.thread=None;self.active_job=None
        for path in self.root.glob('*/state.json'):
            value=read_json(path)
            if value['status'] not in TERMINAL:
                value.update(status='FAILED',error='Backtest interrupted by service restart.',completed_at=time.time());write_json(path,value)
    def _cleanup(self):
        import shutil
        paths=sorted(self.root.glob('*/state.json'),key=lambda p:p.stat().st_mtime)
        remaining=len(paths)
        for path in paths:
            if path.parent.name == self.active_job:continue
            value=read_json(path)
            if value['status'] in TERMINAL and (time.time()-value['created_at']>self.ttl or remaining>=self.max_jobs):
                shutil.rmtree(path.parent);remaining-=1
    def create(self,owner,payload):
        with self.lock:
            self._cleanup()
            states=[read_json(p) for p in self.root.glob('*/state.json')]
            active=[s for s in states if s['status'] not in TERMINAL]
            if len(states)>=self.max_jobs or len(active)>=FAST_JOB_CONCURRENCY+1 or any(s['owner']==owner for s in active):
                raise JobBusy('A backtest is already running or the worker queue is full. Try again after it finishes.')
            job_id=uuid.uuid4().hex;directory=self.root/job_id;directory.mkdir(mode=0o700)
            write_json(directory/'input.json',payload)
            state=dict(job_id=job_id,owner=owner,strategy_id=payload['strategy_id'],symbol=payload['symbol'],start=payload['start'],end=payload['end'],status='PENDING',progress=0,current_stage='Queued',created_at=time.time(),completed_at=None,error=None)
            write_json(directory/'state.json',state)
            if self.start_worker and (not self.thread or not self.thread.is_alive()):
                self.thread=threading.Thread(target=self._work,daemon=True,name='fast-job-manager');self.thread.start()
            return self.get(owner,job_id)
    def _path(self,job_id):
        if len(job_id)!=32 or any(c not in '0123456789abcdef' for c in job_id):raise JobNotFound()
        return self.root/job_id/'state.json'
    def get(self,owner,job_id):
        with self.lock:
            try:value=read_json(self._path(job_id))
            except (FileNotFoundError,ValueError):raise JobNotFound()
            if value['owner']!=owner:raise JobNotFound()
            value.pop('owner');value['result']=None
            progress_path=self.root/job_id/'progress.json'
            if value['status']=='RUNNING' and progress_path.exists():value.update(read_json(progress_path))
            if value['status']=='COMPLETED':value['result']=read_json(self.root/job_id/'result.json')
            return value
    def cancel(self,owner,job_id):
        with self.lock:
            self.get(owner,job_id)
            path=self._path(job_id);value=read_json(path)
            if value['status'] not in TERMINAL:
                (path.parent/'cancel').touch()
                value.update(status='CANCELLED',current_stage='Cancelled',completed_at=time.time(),error='Backtest cancelled. No completed result was changed.')
                write_json(path,value)
            return self.get(owner,job_id)
    def _work(self):
        while True:
            with self.lock:
                pending=[read_json(p) for p in self.root.glob('*/state.json')]
                pending=sorted((s for s in pending if s['status']=='PENDING'),key=lambda s:s['created_at'])
                if not pending:
                    self.thread=None
                    return
                value=pending[0];directory=self.root/value['job_id'];path=directory/'state.json'
                self.active_job=value['job_id']
                value.update(status='RUNNING',current_stage='Starting worker');write_json(path,value)
            error=None
            try:
                process=subprocess.Popen([sys.executable,str(Path(__file__).resolve().parents[1]/'fast_backtest_worker.py'),str(directory)],cwd=Path(__file__).resolve().parents[1],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                deadline=time.monotonic()+600
                while process.poll() is None:
                    if (directory/'cancel').exists() or time.monotonic()>deadline:
                        process.terminate()
                        try:process.wait(timeout=5)
                        except subprocess.TimeoutExpired:process.kill();process.wait()
                        error='Backtest worker timed out or was cancelled.'
                        break
                    time.sleep(.25)
                if process.returncode and not error:error='Backtest worker stopped unexpectedly.'
                if (directory/'error.json').exists():error=read_json(directory/'error.json')['error']
                if not (directory/'result.json').exists() and not error:error='Backtest worker returned no result.'
            except Exception as exc:error=str(exc)
            with self.lock:
                self.active_job=None
                if not path.exists():continue
                state=read_json(path)
                if state['status'] not in TERMINAL:
                    state.update(status='FAILED' if error else 'COMPLETED',current_stage='Failed' if error else 'Complete',progress=state['progress'] if error else 100,completed_at=time.time(),error=error)
                    write_json(path,state)

_instance=None
_instance_lock=threading.Lock()
def manager():
    global _instance
    with _instance_lock:
        if _instance is None:_instance=FastJobs(os.environ.get('SIMULATOR_FAST_JOB_DIR','/tmp/nathauxfx-fast-jobs'))
        return _instance
