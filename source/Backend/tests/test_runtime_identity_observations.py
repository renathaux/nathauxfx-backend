"""Diagnostic history is evidence, never a substitute for fenced authority."""
import importlib
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from test_recovery_store import db, store_api
from test_recovery_startup import dependencies, prepare_cutover, SCOPE


def observation():
    try:
        return importlib.import_module('startup_recovery.observability')
    except ModuleNotFoundError:
        pytest.fail('Process-local runtime identity recorder is missing')


def test_unobserved_startup_does_not_invent_identity_or_authority():
    result = observation().StartupRecorder().snapshot()
    assert result['process']['pid'] == os.getpid()
    for field in ('boot_id', 'attempt_id', 'allocated_epoch', 'attempt_created_at'):
        assert result['startup'][field] is None
    assert result['startup']['acquisition_result'] == 'UNKNOWN'
    assert result['observed_durable_owner']['observed_at'] is None


def test_staged_attempt_not_committed_until_commit_boundary():
    mod = observation()
    recorder = mod.StartupRecorder()
    attempt = str(uuid4())
    created = datetime.now(timezone.utc)
    with mod.observing(recorder):
        mod.note('attempt_staged', attempt_id=attempt, epoch=8, created_at=created)
        assert recorder.snapshot()['startup']['allocated_epoch'] is None
        mod.note('attempt_committed')
    result = recorder.snapshot()['startup']
    assert result['attempt_id'] == attempt and result['allocated_epoch'] == 8
    assert result['attempt_created_at'] == created.isoformat()


def test_ambiguous_attempt_commit_keeps_committed_identity_unknown():
    mod = observation()
    recorder = mod.StartupRecorder()
    with mod.observing(recorder):
        mod.note('attempt_staged', attempt_id=str(uuid4()), epoch=8,
                 created_at=datetime.now(timezone.utc))
        mod.note('finished', ready=False, reason='RECOVERY_STARTUP_FAILED', phases=['BOOTSTRAP'])
    assert recorder.snapshot()['startup']['allocated_epoch'] is None
    assert recorder.snapshot()['startup']['attempt_id'] is None


def test_recorder_freezes_history_and_detaches_returned_objects():
    mod = observation()
    recorder = mod.StartupRecorder()
    with mod.observing(recorder):
        mod.note('finished', ready=False, reason='RECOVERY_OWNER_BUSY', phases=['BOOTSTRAP'])
        before = recorder.snapshot()
        mod.note('acquisition_committed')
    result = recorder.snapshot()
    result['startup']['phases'].append('NEW_ENTRIES_READY')
    assert recorder.snapshot() == before


def test_concurrent_snapshots_are_detached_and_deterministic():
    mod = observation()
    recorder = mod.StartupRecorder()
    with mod.observing(recorder):
        mod.note('finished', ready=False, reason='BUILD_IDENTITY_UNVERIFIED', phases=['BOOTSTRAP'])
    with ThreadPoolExecutor(max_workers=8) as pool:
        values = list(pool.map(lambda _: recorder.snapshot(), range(64)))
    assert all(value == values[0] for value in values)
    values[0]['startup']['phases'].clear()
    assert recorder.snapshot()['startup']['phases'] == ['BOOTSTRAP']


def test_observer_failure_is_contained_and_context_is_restored():
    mod = observation()
    class Broken:
        def record(self, *args, **kwargs):
            raise RuntimeError('private diagnostic failure')
    with mod.observing(Broken()):
        mod.note('acquisition_started')
    mod.note('acquisition_started')


@pytest.mark.parametrize('value', ['https://user:secret@example.com', 'instance\nsecret', 'x' * 200])
def test_invalid_render_identifier_is_not_returned(monkeypatch, value):
    monkeypatch.setenv('RENDER_INSTANCE_ID', value)
    assert observation().StartupRecorder().snapshot()['process']['render_instance_id'] is None


def test_build_allowlist_does_not_copy_secrets_or_arbitrary_exception():
    mod = observation()
    recorder = mod.StartupRecorder()
    with mod.observing(recorder):
        mod.note('build', build={'backend_git_sha': 'a' * 40, 'token': 'secret'})
        mod.note('finished', ready=False, reason='postgresql://secret', phases=['BOOTSTRAP'])
    snapshot = recorder.snapshot()
    assert 'secret' not in str(snapshot)
    assert snapshot['startup']['reason'] == 'UNKNOWN'
    assert snapshot['startup']['build']['backend_git_sha'] == 'a' * 40


def test_real_coordinator_records_committed_attempt_and_success(db, store_api):
    from startup_recovery.coordinator import recover
    from models import RecoveryAttempt
    mod = observation()
    recorder = mod.StartupRecorder()
    prepare_cutover(store_api, db)
    trace = []
    with mod.observing(recorder):
        outcome = recover(dependencies(db, trace), SCOPE, str(uuid4()), 'a' * 40)
    snapshot = recorder.snapshot()
    assert outcome.ready
    assert snapshot['startup']['attempt_id'] == outcome.token.attempt_id
    assert snapshot['startup']['allocated_epoch'] == 2
    assert snapshot['startup']['acquisition_result'] == 'COMMITTED'
    with db() as session:
        attempt = session.get(RecoveryAttempt, outcome.token.attempt_id)
        assert datetime.fromisoformat(snapshot['startup']['attempt_created_at']) == attempt.created_at
    assert trace == ['authenticate', 'publish', 'management', 'entries']
    # Fixture publication is not the real token/producer installation boundary.
    assert snapshot['startup']['token_installed'] == 'UNKNOWN'


def test_busy_attempt_records_other_owner_without_claiming_it(db, store_api):
    from startup_recovery.coordinator import recover
    mod = observation()
    prepare_cutover(store_api, db)
    first = recover(dependencies(db, []), SCOPE, str(uuid4()), 'a' * 40)
    recorder = mod.StartupRecorder()
    with mod.observing(recorder):
        second = recover(dependencies(db, []), SCOPE, str(uuid4()), 'a' * 40)
    result = recorder.snapshot()
    assert second.reason == 'RECOVERY_OWNER_BUSY'
    assert result['startup']['acquisition_result'] == 'REJECTED'
    assert result['startup']['attempt_id'] == second.token.attempt_id
    assert result['observed_durable_owner']['attempt_id'] == first.token.attempt_id
    assert result['observed_durable_owner']['epoch'] == first.token.epoch
    assert result['observed_durable_owner']['observed_at'] is not None


def test_failure_before_acquire_does_not_report_busy(db, store_api):
    from startup_recovery.coordinator import recover
    mod = observation()
    prepare_cutover(store_api, db)
    deps = dependencies(db, [])
    deps.reconcile_submissions = lambda: {'ok': False}
    recorder = mod.StartupRecorder()
    with mod.observing(recorder):
        outcome = recover(deps, SCOPE, str(uuid4()), 'a' * 40)
    result = recorder.snapshot()['startup']
    assert outcome.reason == 'RECOVERY_OPERATIONS_UNRESOLVED'
    assert result['submission_reconciliation'] is False
    assert result['acquisition_attempted'] == 'UNKNOWN'
    assert result['acquisition_result'] == 'UNKNOWN'


def test_bootstrap_build_failure_is_observed_without_db_or_recovery(monkeypatch):
    from startup_recovery.bootstrap import start
    from live_integrity import build_identity
    def fail():
        raise ValueError('private detail')
    monkeypatch.setattr(build_identity, 'capture', fail)
    api = SimpleNamespace(ENGINE_RUNTIME_STATE={})
    outcome = start(api, engine=object(), session_factory=object())
    assert not outcome.ready
    assert hasattr(api, '_runtime_identity'), 'bootstrap must record its own failure'
    snapshot = api._runtime_identity.snapshot()['startup']
    assert snapshot['reason'] == 'BUILD_IDENTITY_UNVERIFIED'
    assert snapshot['phases'] == ['BOOTSTRAP']
    assert snapshot['attempt_id'] is None
    assert snapshot['boot_id'] is None


def test_recorder_failure_cannot_change_real_recovery(db, store_api, monkeypatch):
    from startup_recovery.coordinator import recover
    mod = observation()
    prepare_cutover(store_api, db)
    recorder = mod.StartupRecorder()
    def fail(*args, **kwargs):
        raise RuntimeError('observation unavailable')
    monkeypatch.setattr(recorder, 'record', fail)
    trace = []
    with mod.observing(recorder):
        outcome = recover(dependencies(db, trace), SCOPE, str(uuid4()), 'a' * 40)
    assert outcome.ready
    assert trace == ['authenticate', 'publish', 'management', 'entries']


@pytest.mark.parametrize('transaction', [1, 2])
@pytest.mark.parametrize('recovery_error', [False, True])
def test_failed_commit_is_not_reported_as_committed(db, store_api, transaction, recovery_error):
    from contextlib import contextmanager
    from startup_recovery.coordinator import recover
    from startup_recovery.types import RecoveryError
    mod = observation()
    prepare_cutover(store_api, db)
    class BrokenCommit:
        calls = 0
        def __call__(self):
            return db()
        @contextmanager
        def begin(self):
            self.calls += 1
            with db.begin() as session:
                yield session
                if self.calls == transaction:
                    if recovery_error:
                        raise RecoveryError('RECOVERY_DB_UNAVAILABLE')
                    raise RuntimeError('simulated connection loss before commit')
    deps = dependencies(db, [])
    deps.session_factory = BrokenCommit()
    recorder = mod.StartupRecorder()
    with mod.observing(recorder):
        result = recover(deps, SCOPE, str(uuid4()), 'a' * 40)
    assert not result.ready
    snapshot = recorder.snapshot()['startup']
    if transaction == 1:
        assert snapshot['attempt_id'] is None
        assert snapshot['allocated_epoch'] is None
    else:
        assert snapshot['allocated_epoch'] == 2
    assert snapshot['acquisition_result'] == 'UNKNOWN'


def test_real_bootstrap_binds_boot_uuid_to_committed_attempt(db, store_api, monkeypatch):
    from startup_recovery.bootstrap import start
    from live_integrity import build_identity
    from models import RuntimeSetting, RecoveryAttempt
    prepare_cutover(store_api, db)
    with db.begin() as session:
        session.add(RuntimeSetting(setting_name='ctrader_active_account',
            setting_value='{"account_id":"7","env":"demo"}',
            updated_at=datetime.now(timezone.utc), updated_by='fixture'))
    monkeypatch.setattr(build_identity, 'capture', lambda: {'backend_git_sha': 'a' * 40})
    api = SimpleNamespace(ENGINE_RUNTIME_STATE={})
    outcome = start(api, engine=db.kw['bind'], session_factory=db,
                    dependencies_factory=lambda **kw: dependencies(db, []))
    assert outcome.ready
    result = api._runtime_identity.snapshot()['startup']
    with db() as session:
        attempt = session.get(RecoveryAttempt, outcome.token.attempt_id)
        assert result['boot_id'] == attempt.boot_id
        assert result['attempt_id'] == attempt.attempt_id
    assert result['finalized'] is True and result['ready'] is True
    assert result['phases'][-1] == 'NEW_ENTRIES_READY'


def test_real_publication_records_token_and_producer_separately(db, store_api, tmp_path, monkeypatch):
    from test_recovery_server_adapter import test_server_publication_requires_durable_phase_and_does_not_write_checkpoints
    mod = observation()
    recorder = mod.StartupRecorder()
    with mod.observing(recorder):
        test_server_publication_requires_durable_phase_and_does_not_write_checkpoints(
            store_api, db, tmp_path, monkeypatch, False, False)
    assert recorder.snapshot()['startup']['token_installed'] is True
    assert recorder.snapshot()['startup']['checkpoint_producer_admitted'] is True


def test_forked_process_never_reads_parent_startup_under_inherited_lock(monkeypatch):
    from threading import Thread
    mod = observation()
    recorder = mod.StartupRecorder()
    parent = os.getpid()
    monkeypatch.setattr(mod.os, 'getpid', lambda: parent + 1)
    results = []
    recorder._lock.acquire()
    thread = Thread(target=lambda: results.append(recorder.snapshot()), daemon=True)
    try:
        thread.start()
        thread.join(timeout=1)
        assert not thread.is_alive(), 'child must not wait on inherited parent lock'
        assert results[0]['process']['pid'] == parent + 1
        assert results[0]['startup']['attempt_id'] is None
    finally:
        recorder._lock.release()
        thread.join(timeout=1)


@pytest.mark.parametrize('dropped', ['acquisition_started', 'acquisition_validated',
                                   'acquisition_committed', 'owner_observed'])
def test_lost_event_then_downstream_failure_never_fabricates_rejection(db, store_api, monkeypatch, dropped):
    from startup_recovery.coordinator import recover
    from startup_recovery.types import RecoveryError
    mod = observation()
    prepare_cutover(store_api, db)
    recorder = mod.StartupRecorder()
    original = recorder.record
    def intermittent(event, **fields):
        if event == dropped:
            raise RuntimeError('lost observation')
        return original(event, **fields)
    monkeypatch.setattr(recorder, 'record', intermittent)
    deps = dependencies(db, [])
    def fail():
        raise RecoveryError('RECOVERY_DB_UNAVAILABLE')
    deps.verify_database = fail
    with mod.observing(recorder):
        result = recover(deps, SCOPE, str(uuid4()), 'a' * 40)
    assert result.reason == 'RECOVERY_DB_UNAVAILABLE'
    expected = 'UNKNOWN' if dropped == 'acquisition_committed' else 'COMMITTED'
    assert recorder.snapshot()['startup']['acquisition_result'] == expected


def test_committed_observation_cannot_be_overwritten_by_rejection():
    recorder = observation().StartupRecorder()
    recorder.record('acquisition_started')
    recorder.record('acquisition_committed')
    recorder.record('acquisition_rejected')
    assert recorder.snapshot()['startup']['acquisition_result'] == 'COMMITTED'
