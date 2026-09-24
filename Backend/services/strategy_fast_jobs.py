"""Bounded, owned FAST jobs for the existing single-process Render service.

A separate Python subprocess isolates CPU work from the LIVE HTTP process.
Job files are ephemeral, private, and bounded; no candles or jobs go to Neon.
A service restart marks interrupted jobs failed; completed results expire.
"""
import atexit
import logging
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
        self.thread=None;self.active_job=None;self.stopping=threading.Event()
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
            if self.stopping.is_set():raise JobBusy("Backtest manager is shutting down.")
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
    def close(self):
        """Stop admission and wait for the owning manager to reap its child."""
        with self.lock:
            self.stopping.set()
            thread = self.thread
        if thread is not None and thread is not threading.current_thread():
            thread.join()

    def _run_worker(self, directory):
        from services.heavy_replay_admission import heavy_replay_lease, HeavyReplayBusy
        while not self.stopping.is_set():
            lease = heavy_replay_lease()
            try:
                fd = lease.__enter__()
            except HeavyReplayBusy:
                if (directory / "cancel").exists():
                    return "Backtest cancelled while waiting for admission."
                self.stopping.wait(.25)
                continue
            try:
                return self._run_owned_worker(directory, fd)
            finally:
                lease.__exit__(None, None, None)
        return "Backtest manager is shutting down."

    def _run_owned_worker(self, directory, lease_fd):
        """Own the child until it has exited AND been reaped, even on exceptions."""
        process = None
        try:
            process = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve().parents[1] / 'fast_backtest_worker.py'), str(directory)],
                cwd=Path(__file__).resolve().parents[1],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, pass_fds=(lease_fd,),
            )
            deadline = time.monotonic() + 600
            while process.poll() is None:
                if self.stopping.is_set() or (directory / 'cancel').exists() or time.monotonic() > deadline:
                    return 'Backtest worker timed out or was cancelled.'
                time.sleep(.25)
            if (directory / 'error.json').exists():
                return read_json(directory / 'error.json')['error']
            if process.returncode:
                return 'Backtest worker stopped unexpectedly.'
            if not (directory / 'result.json').exists():
                return 'Backtest worker returned no result.'
            return None
        finally:
            if process is not None:
                while True:
                    try:
                        _terminate_and_reap(process)
                        break
                    except BaseException:
                        if process.returncode is not None:
                            raise
                        # Fail closed: retain ownership and block this queue
                        # until OS cleanup succeeds. Never launch a replacement.
                        logging.exception("FAST worker cleanup failed; retaining ownership of PID %s", process.pid)
                        threading.Event().wait(.25)

    def _work(self):
        try:
            while True:
                with self.lock:
                    if self.stopping.is_set():
                        return
                    pending = [read_json(p) for p in self.root.glob('*/state.json')]
                    pending = sorted((s for s in pending if s['status'] == 'PENDING'), key=lambda s:s['created_at'])
                    if not pending:
                        self.thread = None
                        return
                    value = pending[0]
                    directory = self.root / value['job_id']
                    path = directory / 'state.json'
                    self.active_job = value['job_id']
                try:
                    with self.lock:
                        value = read_json(path)
                        if value['status'] != 'PENDING' or self.stopping.is_set():
                            continue
                        value.update(status='RUNNING', current_stage='Starting worker')
                        write_json(path, value)
                    interrupted = None
                    try:
                        error = self._run_worker(directory)
                    except BaseException as exc:
                        error = str(exc) or type(exc).__name__
                        if not isinstance(exc, Exception):
                            interrupted = exc
                    # _run_worker's finally has already reaped the child before
                    # any status serialization, queue advancement or rethrow.
                    with self.lock:
                        if path.exists():
                            state = read_json(path)
                            if state['status'] not in TERMINAL:
                                state.update(status='FAILED' if error else 'COMPLETED', current_stage='Failed' if error else 'Complete', progress=state['progress'] if error else 100, completed_at=time.time(), error=error)
                                write_json(path, state)
                    if interrupted is not None:
                        raise interrupted
                finally:
                    with self.lock:
                        self.active_job = None
        finally:
            with self.lock:
                # A new create() may have started another manager after the
                # empty-queue return. Do not erase its thread reference.
                if self.thread is threading.current_thread():
                    self.thread = None


def _terminate_and_reap(process, grace_seconds=5):
    """No pipes are requested, but close any handles on a supplied Popen too."""
    try:
        if process.poll() is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass  # Child exited between poll and signal; still wait/reap.
        try:
            process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            process.wait()
    except BaseException:
        # A supervisor interruption during poll/signal/wait must not leave a
        # child behind. Reap before propagating the original interruption.
        try:
            process.kill()
        except ProcessLookupError:
            pass
        process.wait()
        raise
    finally:
        for name in ('stdin', 'stdout', 'stderr'):
            stream = getattr(process, name, None)
            if stream is not None:
                stream.close()

_instance=None
_instance_lock=threading.Lock()
def manager():
    global _instance
    with _instance_lock:
        if _instance is None:_instance=FastJobs(os.environ.get('SIMULATOR_FAST_JOB_DIR','/tmp/nathauxfx-fast-jobs'))
        return _instance


def shutdown_manager():
    with _instance_lock:
        instance = _instance
    if instance is not None:
        instance.close()


atexit.register(shutdown_manager)
