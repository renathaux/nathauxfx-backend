import json
import pytest
from services.strategy_fast_jobs import FastJobs,JobNotFound,JobBusy,write_json,read_json
from services.strategy_fast_cache import FactsCache,facts_key

@pytest.fixture
def payload():return dict(strategy_id='s',symbol='XAUUSD',start='2025-01-01',end='2025-02-01')

def test_jobs_are_owned_bounded_and_cancellable(tmp_path,payload):
    jobs=FastJobs(tmp_path,start_worker=False)
    a=jobs.create('alice',payload)
    assert a['status']=='PENDING' and a['result'] is None and 'owner' not in a
    with pytest.raises(JobNotFound):jobs.get('bob',a['job_id'])
    with pytest.raises(JobNotFound):jobs.cancel('bob',a['job_id'])
    with pytest.raises(JobBusy):jobs.create('alice',payload)
    jobs.create('bob',payload)
    with pytest.raises(JobBusy):jobs.create('carol',payload)
    assert jobs.cancel('alice',a['job_id'])['status']=='CANCELLED'
    assert jobs.get('alice',a['job_id'])['result'] is None
    assert jobs.create('alice',payload)['status']=='PENDING'

def test_restart_fails_unfinished_without_partial_result(tmp_path,payload):
    jobs=FastJobs(tmp_path,start_worker=False);j=jobs.create('a',payload)
    write_json(tmp_path/j['job_id']/'result.json',{'partial':True})
    jobs=FastJobs(tmp_path,start_worker=False)
    assert jobs.get('a',j['job_id'])['status']=='FAILED'
    assert jobs.get('a',j['job_id'])['result'] is None

def test_worker_failure_publishes_failed_not_partial(tmp_path,payload,monkeypatch):
    import services.strategy_fast_jobs as module
    class FailedProcess:
        returncode=1
        def __init__(self,*a,**kw):pass
        def poll(self):return 1
    monkeypatch.setattr(module.subprocess,'Popen',FailedProcess)
    jobs=FastJobs(tmp_path,start_worker=False);j=jobs.create('a',payload)
    jobs._work()
    result=jobs.get('a',j['job_id']);assert result['status']=='FAILED';assert result['result'] is None

def test_cancelled_pending_job_does_not_block_queue(tmp_path,payload,monkeypatch):
    jobs=FastJobs(tmp_path,start_worker=False);j=jobs.create('a',payload);jobs.cancel('a',j['job_id']);jobs._work()
    assert jobs.thread is None

def test_facts_cache_key_has_history_version_and_timeframes(tmp_path):
    args=('v1','XAUUSD','2021','2026','5m','15m','1h');key=facts_key(*args)
    assert key!=facts_key('v2',*args[1:])
    assert key!=facts_key(*args[:-1],'4h')
    cache=FactsCache(tmp_path,max_bytes=60)
    assert cache.put(key,{'one':1});assert cache.get(key)=={'one':1}
    other=facts_key('v2',*args[1:]);cache.put(other,{'two':2})
    assert sum(p.stat().st_size for p in tmp_path.glob('*.pickle'))<=60
    assert not cache.put(key,'x'*100)
    with pytest.raises(ValueError):cache.get('../input')

def test_cleanup_never_removes_current_process_and_only_evicts_needed_result(tmp_path,payload):
    jobs=FastJobs(tmp_path,start_worker=False,max_jobs=3)
    a=jobs.create('a',payload);jobs.cancel('a',a['job_id'])
    b=jobs.create('b',payload);jobs.cancel('b',b['job_id'])
    c=jobs.create('c',payload);jobs.active_job=c['job_id'];jobs.cancel('c',c['job_id'])
    jobs.create('d',payload)
    assert (tmp_path/c['job_id']/'state.json').exists()
    assert (tmp_path/b['job_id']/'state.json').exists()
    assert not (tmp_path/a['job_id']).exists()


def test_queued_workers_never_overlap(tmp_path,payload,monkeypatch):
    import services.strategy_fast_jobs as module
    assert module.FAST_JOB_CONCURRENCY == 1
    live = []
    finished = []
    class ControlledProcess:
        returncode = None
        def __init__(self,args,**kwargs):
            assert not live, 'second heavy worker started before first exited'
            self.directory = module.Path(args[-1])
            live.append(self)
        def poll(self):
            write_json(self.directory/'result.json',{'ok':True})
            self.returncode = 0
            live.remove(self)
            finished.append(self.directory.name)
            return 0
    monkeypatch.setattr(module.subprocess,'Popen',ControlledProcess)
    jobs=FastJobs(tmp_path,start_worker=False)
    first=jobs.create('alice',payload);second=jobs.create('bob',payload)
    jobs._work()
    assert finished == [first['job_id'],second['job_id']]
    assert not live and jobs.active_job is None
    assert jobs.get('alice',first['job_id'])['status']=='COMPLETED'
    assert jobs.get('bob',second['job_id'])['status']=='COMPLETED'


def test_failed_serialization_keeps_previous_result_and_removes_temporary(tmp_path):
    target=tmp_path/'result.json'
    write_json(target,{'ok':True})
    with pytest.raises(ValueError):write_json(target,{'bad':float('nan')})
    assert read_json(target)=={'ok':True}
    assert list(tmp_path.iterdir()) == [target]


def test_isolated_worker_imports_no_live_bootstrap():
    import subprocess,sys
    from pathlib import Path
    backend=Path(__file__).resolve().parents[1]
    script='''
from fast_backtest_worker import initialize_worker_namespace
initialize_worker_namespace()
import services.strategy_fast_worker
import sys
blocked = ('ctrader_connector', 'api', 'database', 'sqlalchemy',
           'services.paper_live_entry_service', 'services.indicator_event_stream_service',
           'services.neon_observer_optimization')
assert not set(blocked).intersection(sys.modules), set(blocked).intersection(sys.modules)
assert services.__spec__.loader.__class__.__name__ == 'NamespaceLoader'
'''
    subprocess.run([sys.executable,'-c',script],cwd=backend,check=True)
