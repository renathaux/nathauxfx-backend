"""Scheduling tests use disposable doubles at DB/broker I/O boundaries only."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, get_ident
import importlib
import time

import pytest

from startup_recovery.types import AccountScope, RecoveryError


def module():
    # Missing implementation is a test failure, not a collection error.
    from startup_recovery import readers
    assert importlib.util.find_spec('startup_recovery.handoff_reader') is not None
    return importlib.import_module('startup_recovery.handoff_reader')


def install(monkeypatch, *, barrier=None, fail=None, finished=None):
    m = module()
    calls = []
    class Database:
        def __init__(self, engine, *, history_from):
            assert engine == 'disposable'; self.history_from = history_from
        def credentials(self, scope):
            calls.append(('credentials', get_ident(), scope))
            return ('synthetic',)
        def __call__(self, scope):
            calls.append(('database', get_ident(), scope))
            if barrier: barrier.wait(timeout=2)
            try:
                if fail == 'database': raise ValueError('synthetic DB failure')
                return {'selection': {'account_id': scope.account_id}, 'rows': [1, 2]}
            finally:
                if finished: finished.set()
        def read_connection(self, connection, scope):
            assert connection == 'locked-connection'
            return {'fresh': scope.account_id}
    class Broker:
        def __init__(self, account, environment, credentials, deadline):
            assert (account, environment, credentials) == ('7', 'demo', ('synthetic',))
            assert deadline <= time.monotonic() + 20
        def __enter__(self): return self
        def __exit__(self, *args): calls.append(('closed', get_ident(), None))
        def snapshot(self, history_from):
            assert history_from == 0
            calls.append(('broker', get_ident(), None))
            if barrier: barrier.wait(timeout=2)
            if fail == 'broker': raise ValueError('synthetic broker failure')
            return {'positions': [], 'orders': [], 'history_complete': True}
    monkeypatch.setattr(m, 'DatabaseReader', Database)
    monkeypatch.setattr(m, 'BrokerReader', Broker)
    return m.ConcurrentHandoffReader('disposable', history_from=0), calls


def test_complete_reads_overlap_broker_stays_on_caller_and_no_cross_request_cache(monkeypatch):
    reader, calls = install(monkeypatch, barrier=Barrier(2))
    scope = AccountScope('ctrader', 'demo', '7')
    caller = get_ident()
    first = reader(scope)
    first['database']['rows'].append(3)
    second = reader(scope)
    assert second == {'database': {'selection': {'account_id': '7'}, 'rows': [1, 2]},
                      'broker': {'positions': [], 'orders': [], 'history_complete': True}}
    assert [c[0] for c in calls].count('credentials') == 2
    assert all(t == caller for kind, t, _ in calls if kind in {'broker', 'closed', 'credentials'})
    assert all(t != caller for kind, t, _ in calls if kind == 'database')


@pytest.mark.parametrize('failure', ['database', 'broker'])
def test_failed_branch_joins_all_work_and_returns_no_partial_observation(monkeypatch, failure):
    finished = Event()
    reader, calls = install(monkeypatch, barrier=Barrier(2), fail=failure, finished=finished)
    with pytest.raises(RecoveryError): reader(AccountScope('ctrader', 'demo', '7'))
    assert finished.is_set()
    assert [c[0] for c in calls].count('closed') == 1


def test_observation_rejected_before_io_inside_ownership_transaction(monkeypatch):
    from startup_recovery import unit_of_work
    reader, calls = install(monkeypatch)
    marker = unit_of_work._active.set(unit_of_work.UnitOfWork(object(), None))
    try:
        with pytest.raises(RecoveryError): reader(AccountScope('ctrader', 'demo', '7'))
    finally: unit_of_work._active.reset(marker)
    assert calls == []


def test_concurrent_invocation_rejected_without_extra_reads(monkeypatch):
    m = module()
    entered, finish = Event(), Event()
    reader, calls = install(monkeypatch)
    original = m.BrokerReader.snapshot
    def snapshot(self, history_from):
        entered.set(); assert finish.wait(2)
        return original(self, history_from)
    monkeypatch.setattr(m.BrokerReader, 'snapshot', snapshot)
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(reader, AccountScope('ctrader', 'demo', '7'))
        assert entered.wait(2)
        try:
            with pytest.raises(RecoveryError): reader(AccountScope('ctrader', 'demo', '7'))
        finally: finish.set()
        assert first.result()['broker']['history_complete'] is True
    assert [c[0] for c in calls].count('credentials') == 1


def test_expired_complete_observation_is_not_returned(monkeypatch):
    m = module()
    reader, _ = install(monkeypatch)
    ticks = iter([0.0, 20.001])
    monkeypatch.setattr(m, 'monotonic', lambda: next(ticks))
    with pytest.raises(RecoveryError): reader(AccountScope('ctrader', 'demo', '7'))


def test_locked_reread_uses_supplied_transaction_without_network(monkeypatch):
    reader, calls = install(monkeypatch)
    class Session:
        def connection(self): return 'locked-connection'
    assert reader.database(Session(), AccountScope('ctrader', 'demo', '7')) == {'fresh': '7'}
    assert calls == []


def test_broker_failure_waits_for_slow_database_and_next_call_is_fresh(monkeypatch):
    from concurrent.futures import TimeoutError
    m = module()
    reader, calls = install(monkeypatch)
    worker_entered, broker_failed, release_worker = Event(), Event(), Event()
    original_db = m.DatabaseReader.__call__
    original_broker = m.BrokerReader.snapshot
    def slow(self, scope):
        worker_entered.set()
        assert release_worker.wait(3)
        return original_db(self, scope)
    def broken(self, history):
        assert worker_entered.wait(2)
        broker_failed.set()
        raise ValueError('secret-like synthetic value must not escape')
    monkeypatch.setattr(m.DatabaseReader, '__call__', slow)
    monkeypatch.setattr(m.BrokerReader, 'snapshot', broken)
    scope = AccountScope('ctrader', 'demo', '7')
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(reader, scope)
        assert broker_failed.wait(2)
        try:
            with pytest.raises(TimeoutError): pending.result(timeout=.05)
        finally: release_worker.set()
        with pytest.raises(RecoveryError, match='^RECOVERY_TERMINATION_EVIDENCE_INVALID$'):
            pending.result()
    monkeypatch.setattr(m.DatabaseReader, '__call__', original_db)
    monkeypatch.setattr(m.BrokerReader, 'snapshot', original_broker)
    assert reader(scope)['database']['rows'] == [1, 2]
    assert [c[0] for c in calls].count('credentials') == 2


def test_credential_failure_starts_neither_branch(monkeypatch):
    m = module()
    reader, calls = install(monkeypatch)
    def fail(*args): raise ValueError('synthetic credential error')
    monkeypatch.setattr(m.DatabaseReader, 'credentials', fail)
    with pytest.raises(RecoveryError, match='^RECOVERY_TERMINATION_EVIDENCE_INVALID$'):
        reader(AccountScope('ctrader', 'demo', '7'))
    assert calls == []
