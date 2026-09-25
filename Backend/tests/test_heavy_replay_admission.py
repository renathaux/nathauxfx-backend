"""Catch per-route/per-process locks that permit overlapping heavy work."""
import os
import subprocess
import sys
from pathlib import Path
import pytest
from services.heavy_replay_admission import heavy_replay_lease, HeavyReplayBusy


def test_same_process_and_another_process_cannot_enter(tmp_path,monkeypatch):
    monkeypatch.setenv('HEAVY_REPLAY_LOCK_PATH',str(tmp_path/'heavy.lock'))
    with heavy_replay_lease():
        with pytest.raises(HeavyReplayBusy):
            with heavy_replay_lease():pass
        code='from services.heavy_replay_admission import heavy_replay_lease,HeavyReplayBusy\ntry:\n with heavy_replay_lease():pass\nexcept HeavyReplayBusy:raise SystemExit(23)'
        result=subprocess.run([sys.executable,'-c',code],env=os.environ.copy())
        assert result.returncode==23
    with heavy_replay_lease():pass


def test_lease_is_released_after_failure(tmp_path,monkeypatch):
    monkeypatch.setenv('HEAVY_REPLAY_LOCK_PATH',str(tmp_path/'heavy.lock'))
    with pytest.raises(ValueError):
        with heavy_replay_lease():raise ValueError('calculation failed')
    with heavy_replay_lease():pass


def test_child_inherits_lease_until_reaped(tmp_path,monkeypatch):
    monkeypatch.setenv('HEAVY_REPLAY_LOCK_PATH',str(tmp_path/'heavy.lock'))
    with heavy_replay_lease() as fd:
        child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],pass_fds=(fd,))
    try:
        with pytest.raises(HeavyReplayBusy):
            with heavy_replay_lease():pass
    finally:
        child.terminate();child.wait()
    with heavy_replay_lease():pass


def test_all_heavy_http_routes_share_fast_worker_lease(tmp_path,monkeypatch):
    import asyncio
    from services.heavy_replay_admission import HeavyReplayAdmission
    monkeypatch.setenv('HEAVY_REPLAY_LOCK_PATH',str(tmp_path/'heavy.lock'))
    called=[]
    async def app(scope,receive,send):called.append(scope['path'])
    gate=HeavyReplayAdmission(app)
    async def run():
        for path in ['/strategy-simulator/run','/strategy-lab/replay','/strategy-simulator/manual-history','/strategy-studio/parity/run']:
            messages=[]
            async def send(value):messages.append(value)
            with heavy_replay_lease():
                await gate({'type':'http','method':'POST','path':path},None,send)
            assert messages[0]['status']==429
        assert not called
        await gate({'type':'http','method':'POST','path':'/strategy-simulator/run'},None,None)
        assert called==['/strategy-simulator/run']
    asyncio.run(run())


def test_studio_readiness_does_not_compute_while_heavy_job_runs(tmp_path, monkeypatch):
    from routes import strategy_studio as studio
    monkeypatch.setenv('HEAVY_REPLAY_LOCK_PATH', str(tmp_path / 'heavy.lock'))
    calls = []
    monkeypatch.setattr(studio, 'has_unresolved_studio_reconciliation', lambda owner: False)
    monkeypatch.setattr(studio, '_active_strategy', lambda owner: calls.append(owner))
    with heavy_replay_lease():
        result = studio.evaluate_live_handoff_readiness('owner')
    assert not calls
    assert result['ready'] is False
    assert result['reason'] == 'HEAVY_BACKTEST_BUSY'
    studio.evaluate_live_handoff_readiness('owner')
    assert calls == ['owner']
