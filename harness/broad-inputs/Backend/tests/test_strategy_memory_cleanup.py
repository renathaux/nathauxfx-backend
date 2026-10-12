"""Manager owns scratch cleanup even when a killed worker cannot run finally."""
import pytest
from services.strategy_fast_jobs import FastJobs, write_json


@pytest.mark.parametrize('outcome',['success','error','exception','cancel','timeout'])
def test_manager_cleans_worker_scratch_after_every_outcome(tmp_path,monkeypatch,outcome):
    jobs=FastJobs(tmp_path,start_worker=False)
    created=jobs.create('test',dict(strategy_id='s',symbol='XAUUSD',start='2025-01-01',end='2025-02-01'))
    directory=tmp_path/created['job_id']
    def worker(path):
        scratch=path/'chunks-abandoned';scratch.mkdir()
        write_json(scratch/'0.json',{'partial':True})
        (path/'result.tmp-abandoned').write_text('partial')
        write_json(path/'result.json',{'ok':True})
        if outcome=='exception':raise RuntimeError('manager failed')
        if outcome=='cancel':jobs.cancel('test',created['job_id'])
        return None if outcome=='success' else outcome
    monkeypatch.setattr(jobs,'_run_worker',worker)
    jobs._work()
    assert not list(directory.glob('chunks-*'))
    assert not list(directory.glob('*.tmp-*'))
    assert (directory/'result.json').exists()==(outcome=='success')
    assert jobs.active_job is None


def test_job_boundary_releases_unreachable_http_cycles_after_worker_reaping(tmp_path,monkeypatch):
    import gc
    import weakref
    jobs=FastJobs(tmp_path,start_worker=False)
    jobs.create('test',dict(strategy_id='s',symbol='XAUUSD',start='2025-01-01',end='2025-02-01'))
    references=[]
    class ClosedConnection:
        pass
    def worker(path):
        # Closed ASGI connection/task graphs can await cyclic collection despite
        # having no live request owner. Model that lifetime without importing
        # a particular Uvicorn version into the manager unit test.
        connection=ClosedConnection();connection.cycle=connection
        references.append(weakref.ref(connection))
        write_json(path/'result.json',{'ok':True})
        return None  # _run_worker returns only after the actual child is reaped.
    monkeypatch.setattr(jobs,'_run_worker',worker)
    enabled=gc.isenabled();gc.disable()
    try:
        jobs._work()
        assert references and all(ref() is None for ref in references)
    finally:
        if enabled:gc.enable()
        gc.collect()
