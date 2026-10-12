"""Real FAST supervisors/children competing with synchronous HTTP replay work."""
import asyncio
from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest
from services import heavy_replay_admission as admission
from services import strategy_fast_jobs as fast


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setenv('HEAVY_REPLAY_LOCK_PATH', str(tmp_path / 'heavy.lock'))
    children = []
    launched = threading.Event()
    blocked = threading.Event()
    real_popen = subprocess.Popen
    original_lease = admission.heavy_replay_lease

    @contextmanager
    def observed_lease():
        try:
            with original_lease() as fd:
                yield fd
        except admission.HeavyReplayBusy:
            blocked.set()
            raise

    def launch(args, **kwargs):
        code = '''import sys,time
from pathlib import Path
p=Path(sys.argv[1])
while not (p/'release').exists():time.sleep(.01)
(p/'result.json').write_text('{}')
'''
        child = real_popen([sys.executable, '-c', code, args[-1]], **kwargs)
        children.append(child)
        launched.set()
        return child

    monkeypatch.setattr(admission, 'heavy_replay_lease', observed_lease)
    monkeypatch.setattr(fast.subprocess, 'Popen', launch)
    managers = []
    def make(name):
        manager = fast.FastJobs(tmp_path / name)
        managers.append(manager)
        job = manager.create(name, dict(strategy_id='s', symbol='XAUUSD', start='2025-01-01', end='2025-02-01'))
        return manager, tmp_path / name / job['job_id']
    yield make, children, launched, blocked
    for manager in managers:
        manager.close()
    for child in children:
        assert child.poll() is not None
        with pytest.raises(ChildProcessError):
            os.waitpid(child.pid, os.WNOHANG)
    with original_lease():
        pass


@pytest.mark.parametrize('path', ['/strategy-lab/replay', '/strategy-simulator/run', '/strategy-simulator/manual-history', '/strategy-studio/parity/run'])
def test_fast_blocks_each_http_replay(harness, path):
    make, children, launched, _ = harness
    jobs, directory = make('fast')
    assert launched.wait(5)
    called = []
    messages = []
    async def app(*args): called.append(True)
    async def send(message): messages.append(message)
    asyncio.run(admission.HeavyReplayAdmission(app)({'type':'http', 'method':'POST', 'path':path}, None, send))
    assert not called and messages[0]['status'] == 429
    (directory / 'release').touch()
    jobs.thread.join(5)
    assert children[0].returncode == 0


@pytest.mark.parametrize('path', ['/strategy-lab/replay', '/strategy-simulator/run', '/strategy-studio/parity/run'])
def test_http_replay_blocks_fast_until_response_finishes(harness, path):
    make, children, launched, blocked = harness
    async def app(*args):
        jobs, directory = make('fast')
        assert await asyncio.to_thread(blocked.wait, 5)
        assert not launched.is_set() and not children
        # The response still owns the lease here.
        (directory / 'release').touch()
    asyncio.run(admission.HeavyReplayAdmission(app)({'type':'http', 'method':'POST', 'path':path}, None, None))
    assert launched.wait(5)


def test_two_independent_fast_managers_share_one_permit(harness):
    make, children, launched, blocked = harness
    first, first_dir = make('first')
    assert launched.wait(5)
    second, second_dir = make('second')
    assert blocked.wait(5)
    assert len(children) == 1 and children[0].poll() is None
    (first_dir / 'release').touch()
    thread = first.thread
    thread.join(5)
    assert not thread.is_alive() and children[0].returncode == 0
    (second_dir / 'release').touch()
    thread = second.thread
    if thread: thread.join(5)
    assert len(children) == 2 and all(child.returncode == 0 for child in children)


@pytest.mark.parametrize('fault', ['cancel', 'exception', 'timeout', 'crash'])
def test_fast_fault_reaps_child_then_releases_global_permit(tmp_path, monkeypatch, fault):
    monkeypatch.setenv('HEAVY_REPLAY_LOCK_PATH', str(tmp_path / 'heavy.lock'))
    jobs = fast.FastJobs(tmp_path / 'jobs', start_worker=False)
    job = jobs.create('owner', dict(strategy_id='s', symbol='XAUUSD', start='2025-01-01', end='2025-02-01'))
    directory = jobs.root / job['job_id']
    real_popen = subprocess.Popen
    children = []
    def launch(args, **kwargs):
        code = 'import os;os._exit(7)' if fault == 'crash' else 'import time;time.sleep(60)'
        child = real_popen([sys.executable, '-c', code], **kwargs)
        children.append(child)
        with pytest.raises(admission.HeavyReplayBusy):
            with admission.heavy_replay_lease(): pass
        if fault == 'cancel': jobs.cancel('owner', job['job_id'])
        if fault == 'exception':
            original_poll = child.poll
            def broken_poll():
                child.poll = original_poll
                raise RuntimeError('injected supervisor error')
            child.poll = broken_poll
        return child
    monkeypatch.setattr(fast.subprocess, 'Popen', launch)
    if fault == 'timeout':
        ticks = iter([0, 1801])
        monkeypatch.setattr(fast.time, 'monotonic', lambda: next(ticks))
    jobs._work()
    assert jobs.get('owner', job['job_id'])['status'] == ('CANCELLED' if fault == 'cancel' else 'FAILED')
    assert children[0].returncode is not None
    with pytest.raises(ChildProcessError): os.waitpid(children[0].pid, os.WNOHANG)
    with admission.heavy_replay_lease(): pass


def test_http_exception_releases_permit(tmp_path, monkeypatch):
    monkeypatch.setenv('HEAVY_REPLAY_LOCK_PATH', str(tmp_path / 'heavy.lock'))
    async def app(*args): raise ValueError('calculation error')
    with pytest.raises(ValueError):
        asyncio.run(admission.HeavyReplayAdmission(app)({'type':'http', 'method':'POST', 'path':'/strategy-lab/replay'}, None, None))
    with admission.heavy_replay_lease(): pass
