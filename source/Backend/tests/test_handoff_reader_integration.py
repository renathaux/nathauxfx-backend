"""Real fenced adapter/PG authority with concurrent synthetic read observations."""
from copy import deepcopy
from time import time

import pytest

from models import RecoveryCheckpointHead, RecoveryLegacyAdoption
from startup_recovery import owner_handoff, store, handoff_reader
from startup_recovery.types import RecoveryError
from test_owner_handoff_runtime import runtime_args, repin, release
from test_owner_handoff_ready import ready_args
from test_legacy_adoption_postgres import prepared, db, migrated_database


def concurrent(args, monkeypatch, *, fault=None):
    original = args['safety_reader']
    broker_value = original(args['scope'])['broker']
    count = [0]
    class Database:
        def __init__(self, engine, *, history_from): pass
        def credentials(self, scope): return {'synthetic': True}
        def __call__(self, scope):
            return original(scope)['database']
        def read_connection(self, connection, scope):
            raise AssertionError('locked path uses explicit test fixture session')
    class Broker:
        def __init__(self, *args): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def snapshot(self, history):
            count[0] += 1
            value = deepcopy(broker_value)
            value['observed_at'] = value['history_to'] = time()
            if count[0] == 2:
                if fault == 'position': value['positions'] = [{'id': 'synthetic'}]
                if fault == 'pending': value['orders'] = [{'id': 'synthetic'}]
                if fault == 'history': value['history_complete'] = False
                if fault == 'expired': value['observed_at'] -= 21
            return value
    monkeypatch.setattr(handoff_reader, 'DatabaseReader', Database)
    monkeypatch.setattr(handoff_reader, 'BrokerReader', Broker)
    class Reader(handoff_reader.ConcurrentHandoffReader):
        def database(self, session, scope):
            value = original.database(session, scope)
            if fault == 'account': value['selection']['account_id'] = 'changed'
            elif fault == 'live': value['settings'][0]['setting_value'] = 'true'
            elif fault == 'unresolved': value['submissions'] = [{'attempt_status': 'SUBMITTING'}]
            elif fault == 'head': value['checkpoint_heads'][0]['file_hash'] = 'f'*64
            return value
    args['safety_reader'] = Reader('synthetic', history_from=0)
    return count


def test_concurrent_reader_matches_sequential_authority_and_exactly_one_mutation(runtime_args, monkeypatch):
    sequential = repin(runtime_args)
    count = concurrent(runtime_args, monkeypatch)
    assert repin(runtime_args) == sequential
    count[0] = 0
    calls = []
    original = store.relinquish
    def mutation(*args):
        calls.append(1)
        return original(*args)
    monkeypatch.setattr(store, 'relinquish', mutation)
    assert release(runtime_args).status == 'RELINQUISHED'
    assert calls == [1] and count == [2]
    with runtime_args['session_factory']() as session:
        assert store.account_state(session, runtime_args['scope']).owner_attempt_id is None


@pytest.mark.parametrize('fault', ['position', 'pending', 'history', 'expired', 'account',
    'live', 'unresolved', 'head', 'generation', 'lineage', 'receipt', 'owner', 'termination'])
def test_concurrent_reader_never_bypasses_fresh_authority(runtime_args, monkeypatch, fault):
    repin(runtime_args)
    concurrent(runtime_args, monkeypatch, fault=fault)
    if fault in {'generation', 'lineage'}:
        with runtime_args['session_factory']() as session:
            head = session.query(RecoveryCheckpointHead).filter_by(kind='live_backup').one()
            root = runtime_args['state_root']/'.recovery-generations'/head.manifest_hash
        (root/('manifest.json' if fault == 'lineage' else 'live_backup.json')).write_bytes(b'{}')
    elif fault in {'receipt', 'owner'}:
        with runtime_args['session_factory'].begin() as session:
            if fault == 'receipt': session.query(RecoveryLegacyAdoption).one().receipt_digest = 'f'*64
            else: store.account_state(session, runtime_args['scope']).owner_epoch += 1
    elif fault == 'termination': runtime_args['evidence_document'] += b' '
    monkeypatch.setattr(store, 'relinquish', lambda *a: pytest.fail('unsafe state reached ownership mutation'))
    with pytest.raises(RecoveryError): release(runtime_args)
    with runtime_args['session_factory']() as session:
        assert store.account_state(session, runtime_args['scope']).owner_attempt_id == runtime_args['expected_attempt_id']


def test_failed_concurrent_call_cannot_reuse_prevalidated_generation(runtime_args, monkeypatch):
    repin(runtime_args)
    concurrent(runtime_args, monkeypatch)
    original = owner_handoff._runtime_fresh
    calls = []
    def expire(*args):
        calls.append(1)
        if len(calls) == 3: raise RecoveryError('RECOVERY_TERMINATION_EVIDENCE_INVALID')
        return original(*args)
    monkeypatch.setattr(owner_handoff, '_runtime_fresh', expire)
    with pytest.raises(RecoveryError): release(runtime_args)
    monkeypatch.setattr(owner_handoff, '_runtime_fresh', original)
    with runtime_args['session_factory']() as session:
        head = session.query(RecoveryCheckpointHead).filter_by(kind='live_backup').one()
        path = runtime_args['state_root']/'.recovery-generations'/head.manifest_hash/'live_backup.json'
    path.write_bytes(b'{}')
    monkeypatch.setattr(store, 'relinquish', lambda *a: pytest.fail('cross-call cache authorized mutation'))
    with pytest.raises(RecoveryError): release(runtime_args)


def test_two_admin_attempts_contend_on_real_account_lock_only_one_relinquishes(runtime_args, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    repin(runtime_args)
    concurrent(runtime_args, monkeypatch)
    first = dict(runtime_args)
    second = dict(runtime_args)
    # Separate reader instances model separate admin invocations/processes.
    second['safety_reader'] = type(first['safety_reader'])('synthetic', history_from=0)
    entered, finish = Event(), Event()
    original = store.relinquish
    mutations = []
    def held(*args):
        entered.set()
        assert finish.wait(5)
        mutations.append(1)
        return original(*args)
    monkeypatch.setattr(store, 'relinquish', held)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(release, first)
        assert entered.wait(5)
        try:
            with pytest.raises(RecoveryError, match='RECOVERY_ACCOUNT_BUSY'): release(second)
        finally: finish.set()
        assert pending.result().status == 'RELINQUISHED'
    assert mutations == [1]
