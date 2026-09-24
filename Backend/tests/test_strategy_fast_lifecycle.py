"""Actual OS children, with deterministic faults in the supervisor lifecycle.

These are cleanup tests, not substitutes for full five-year memory workloads.
"""
import os
from pathlib import Path
import subprocess
import sys
import pytest
from services import strategy_fast_jobs as jobs_module


def payload():
    return dict(strategy_id='s',symbol='XAUUSD',start='2025-01-01',end='2025-02-01')


@pytest.mark.parametrize('fault', ['manager_exception', 'manager_interrupt', 'cancel', 'timeout'])
def test_supervisor_reaps_real_child_on_every_interruption(tmp_path,monkeypatch,fault):
    jobs=jobs_module.FastJobs(tmp_path,start_worker=False)
    job=jobs.create('test',payload());directory=tmp_path/job['job_id']
    real_popen=subprocess.Popen;children=[]
    def launch(*args,**kwargs):
        child=real_popen([sys.executable,'-c','import time; time.sleep(60)'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        children.append(child)
        if fault=='cancel':(directory/'cancel').touch()
        return child
    monkeypatch.setattr(jobs_module.subprocess,'Popen',launch)
    if fault.startswith('manager_'):
        original_sleep = jobs_module.time.sleep
        def broken_sleep(_):
            monkeypatch.setattr(jobs_module.time, 'sleep', original_sleep)
            if fault=='manager_interrupt':raise KeyboardInterrupt('injected')
            raise RuntimeError('injected manager exception')
        monkeypatch.setattr(jobs_module.time,'sleep',broken_sleep)
    if fault=='timeout':
        ticks=iter([0,601]);monkeypatch.setattr(jobs_module.time,'monotonic',lambda:next(ticks))
    try:
        if fault=='manager_interrupt':
            with pytest.raises(KeyboardInterrupt):jobs._work()
        else:jobs._work()
        assert len(children)==1
        child=children[0]
        assert child.poll() is not None, 'supervisor left the OS child alive'
        with pytest.raises(ChildProcessError):os.waitpid(child.pid,os.WNOHANG)
        assert jobs.active_job is None
        assert jobs.thread is None
    finally:
        for child in children:
            if child.poll() is None:child.kill()
            child.wait()


@pytest.mark.parametrize('outcome',['success','download_failure','calculation_failure','serialization_failure'])
def test_real_worker_completion_and_failure_are_reaped(tmp_path,monkeypatch,outcome):
    jobs=jobs_module.FastJobs(tmp_path,start_worker=False)
    job=jobs.create('test',payload());directory=tmp_path/job['job_id']
    real_popen=subprocess.Popen;children=[]
    code='''
import sys
from fast_backtest_worker import initialize_worker_namespace
initialize_worker_namespace()
from services import strategy_fast_worker as worker
mode=sys.argv[2]
def execute(*args,**kwargs):
    if mode in ('download_failure','calculation_failure'):raise RuntimeError(mode)
    if mode=='serialization_failure':return {'bad':float('nan')}
    return {'ok':True}
worker.execute=execute
worker.main(sys.argv[1])
'''
    def launch(*args,**kwargs):
        child=real_popen([sys.executable,'-c',code,str(directory),outcome],cwd=Path(__file__).resolve().parents[1],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        children.append(child);return child
    monkeypatch.setattr(jobs_module.subprocess,'Popen',launch)
    jobs._work()
    child=children[0]
    assert child.poll() is not None
    with pytest.raises(ChildProcessError):os.waitpid(child.pid,os.WNOHANG)
    assert jobs.get('test',job['job_id'])['status']==('COMPLETED' if outcome=='success' else 'FAILED')
    assert not list(directory.glob('*.tmp-*'))
    assert jobs.active_job is None and jobs.thread is None


def test_sigterm_resistant_child_is_killed_reaped_and_pipes_closed(tmp_path):
    import time
    ready=tmp_path/'ready'
    code='import signal,time,sys; from pathlib import Path; signal.signal(signal.SIGTERM,signal.SIG_IGN); Path(sys.argv[1]).touch(); time.sleep(60)'
    child=subprocess.Popen([sys.executable,'-c',code,str(ready)],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    try:
        deadline=time.monotonic()+5
        while not ready.exists() and time.monotonic()<deadline:time.sleep(.01)
        assert ready.exists()
        jobs_module._terminate_and_reap(child,grace_seconds=.05)
        assert child.returncode == -9
        assert child.stdout.closed and child.stderr.closed
        with pytest.raises(ChildProcessError):os.waitpid(child.pid,os.WNOHANG)
    finally:
        if child.poll() is None:child.kill()
        child.wait()
        child.stdout.close();child.stderr.close()


def test_status_serialization_failure_cannot_retain_child_or_active_job(tmp_path,monkeypatch):
    jobs=jobs_module.FastJobs(tmp_path,start_worker=False)
    job=jobs.create('test',payload());directory=tmp_path/job['job_id']
    real_popen=subprocess.Popen;children=[]
    def launch(*args,**kwargs):
        code='import sys; from pathlib import Path; (Path(sys.argv[1])/"result.json").write_text("{}")'
        child=real_popen([sys.executable,'-c',code,str(directory)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        children.append(child);return child
    real_write=jobs_module.write_json
    def fail_status(path,value):
        if value.get('status')=='COMPLETED':raise ValueError('injected status encoding error')
        return real_write(path,value)
    monkeypatch.setattr(jobs_module.subprocess,'Popen',launch)
    monkeypatch.setattr(jobs_module,'write_json',fail_status)
    with pytest.raises(ValueError):jobs._work()
    assert jobs.active_job is None and jobs.thread is None
    assert children[0].returncode==0
    with pytest.raises(ChildProcessError):os.waitpid(children[0].pid,os.WNOHANG)


def test_api_response_failure_does_not_orphan_detached_job(tmp_path,monkeypatch):
    """Disconnect/response encoding isn't cancellation: manager owns the job."""
    jobs=jobs_module.FastJobs(tmp_path)
    real_popen=subprocess.Popen;children=[]
    def launch(args,**kwargs):
        code='import sys,time; from pathlib import Path; time.sleep(.1); (Path(sys.argv[1])/"result.json").write_text("{}")'
        child=real_popen([sys.executable,'-c',code,args[-1]],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        children.append(child);return child
    monkeypatch.setattr(jobs_module.subprocess,'Popen',launch)
    def fail_response(*args):raise ConnectionError('client disconnected/API response failed')
    monkeypatch.setattr(jobs,'get',fail_response)
    with pytest.raises(ConnectionError):jobs.create('test',payload())
    thread=jobs.thread
    assert thread is not None
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert len(children)==1 and children[0].returncode==0
    with pytest.raises(ChildProcessError):os.waitpid(children[0].pid,os.WNOHANG)
    assert jobs.active_job is None and jobs.thread is None


def test_exception_during_wait_still_reaps_child(monkeypatch):
    child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    real_wait=child.wait
    def broken_wait(*args,**kwargs):
        monkeypatch.setattr(child,'wait',real_wait)
        raise RuntimeError('injected wait interruption')
    monkeypatch.setattr(child,'wait',broken_wait)
    try:
        with pytest.raises(RuntimeError,match='wait interruption'):
            jobs_module._terminate_and_reap(child)
        assert child.returncode is not None
        with pytest.raises(ChildProcessError):os.waitpid(child.pid,os.WNOHANG)
    finally:
        if child.poll() is None:child.kill()
        real_wait()


def test_cancellation_between_selection_and_start_is_not_overwritten(tmp_path,monkeypatch):
    import threading
    jobs=jobs_module.FastJobs(tmp_path,start_worker=False)
    job=jobs.create('test',payload())
    class InterleavingLock:
        def __init__(self):self.lock=threading.RLock();self.armed=True
        def __enter__(self):self.lock.acquire();return self
        def __exit__(self,*args):
            self.lock.release()
            if self.armed and jobs.active_job is not None:
                self.armed=False
                jobs.cancel('test',job['job_id'])
    jobs.lock=InterleavingLock()
    started=[]
    def forbidden(*args,**kwargs):
        started.append(True);raise AssertionError('cancelled job must not launch')
    monkeypatch.setattr(jobs_module.subprocess,'Popen',forbidden)
    jobs._work()
    assert not started
    assert jobs.get('test',job['job_id'])['status']=='CANCELLED'
    assert jobs.active_job is None


def test_cleanup_failure_blocks_queue_until_real_child_is_reaped(tmp_path,monkeypatch):
    jobs=jobs_module.FastJobs(tmp_path,start_worker=False)
    jobs.create('first',payload());jobs.create('second',payload())
    real_popen=subprocess.Popen;children=[]
    def launch(*args,**kwargs):
        assert all(child.returncode is not None for child in children)
        child=real_popen([sys.executable,'-c','import time; time.sleep(60)'])
        children.append(child)
        (Path(args[0][-1])/'cancel').touch()
        return child
    real_cleanup=jobs_module._terminate_and_reap;attempts=[]
    def flaky_cleanup(child,*args,**kwargs):
        attempts.append(child.pid)
        if len(attempts)<=2:raise OSError('injected signal failure')
        return real_cleanup(child,*args,**kwargs)
    monkeypatch.setattr(jobs_module.subprocess,'Popen',launch)
    monkeypatch.setattr(jobs_module,'_terminate_and_reap',flaky_cleanup)
    try:
        jobs._work()
        assert len(children)==2 and len(attempts)>=4
        for child in children:
            with pytest.raises(ChildProcessError):os.waitpid(child.pid,os.WNOHANG)
    finally:
        for child in children:
            if child.poll() is None:child.kill()
            child.wait()


def test_graceful_shutdown_reaps_worker_and_rejects_new_jobs(tmp_path,monkeypatch):
    import threading
    jobs=jobs_module.FastJobs(tmp_path)
    real_popen=subprocess.Popen;children=[];launched=threading.Event()
    def launch(*args,**kwargs):
        child=real_popen([sys.executable,'-c','import time; time.sleep(60)'])
        children.append(child);launched.set();return child
    monkeypatch.setattr(jobs_module.subprocess,'Popen',launch)
    try:
        jobs.create('first',payload())
        assert launched.wait(5)
        jobs.close()
        assert jobs.thread is None and jobs.active_job is None
        with pytest.raises(ChildProcessError):os.waitpid(children[0].pid,os.WNOHANG)
        with pytest.raises(jobs_module.JobBusy):jobs.create('next',payload())
    finally:
        for child in children:
            if child.poll() is None:child.kill()
            child.wait()
