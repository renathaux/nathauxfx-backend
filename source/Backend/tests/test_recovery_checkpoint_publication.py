"""The real DB pointer, not the newest disk generation, authorizes loading."""
import importlib
import pytest

from test_recovery_store import store_api, db, begin, cutover


def publication():
    try:
        return importlib.import_module('startup_recovery.checkpoint_store')
    except ModuleNotFoundError:
        pytest.fail('DB-anchored checkpoint publication missing')


def admitted(store, db):
    token = begin(store, db)
    cutover(store, db, token)
    with db.begin() as s:
        store.acquire_owner(s, token)
    phases = ['BOOTSTRAP', 'DB_READY', 'BROKER_AUTHENTICATED', 'STATE_DISCOVERED',
              'STATE_RECONCILED', 'POSITION_MANAGEMENT_READY']
    for old, new in zip(phases, phases[1:]):
        with db.begin() as s:
            store.advance(s, token, old, new, 'd' * 64)
    return token


def envelope(token, parent=None, sequence=1):
    from startup_recovery.checkpoints import produced_envelope
    from test_recovery_checkpoints import identity
    fields = identity()
    fields.update(epoch=token.epoch, boot_id=token.boot_id, build_id='a' * 40,
                  generation=sequence)
    return produced_envelope('live_backup', {'protected_sl': '1.1025'}, fields,
        parent_hash=parent, admission_hash='d' * 64, produced_event_id=f'event-{sequence}',
        timestamp='2026-10-01T12:00:00Z')


def test_unadmitted_owner_cannot_publish_or_create_files(store_api, db, tmp_path):
    p = publication()
    token = begin(store_api, db)
    with pytest.raises(store_api.RecoveryError):
        p.publish(db, token, tmp_path, envelope(token), expected_parent=None)
    assert list(tmp_path.iterdir()) == []


def test_publication_commits_pointer_only_after_durable_files(store_api, db, tmp_path):
    p = publication()
    token = admitted(store_api, db)
    result = p.publish(db, token, tmp_path, envelope(token), expected_parent=None)
    accepted = p.load(db, token.scope, tmp_path, 'live_backup')
    assert accepted.payload == {'protected_sl': '1.1025'}
    assert accepted.checkpoint_hash == result.file_hashes['live_backup']
    with pytest.raises(store_api.RecoveryError, match='CHECKPOINT_PARENT_INVALID'):
        p.publish(db, token, tmp_path, envelope(token), expected_parent=None)


def test_db_failure_after_rename_preserves_old_commit_pointer(store_api, db, tmp_path, monkeypatch):
    p = publication()
    token = admitted(store_api, db)
    old = p.publish(db, token, tmp_path, envelope(token), expected_parent=None)
    from sqlalchemy import event
    def fail_commit(session):
        raise RuntimeError('commit interrupted')
    event.listen(db.class_, 'before_commit', fail_commit)
    try:
        with pytest.raises(RuntimeError, match='commit interrupted'):
            p.publish(db, token, tmp_path, envelope(token, old.manifest_hash, 2),
                      expected_parent=old.manifest_hash)
    finally:
        event.remove(db.class_, 'before_commit', fail_commit)
    assert p.load(db, token.scope, tmp_path, 'live_backup').checkpoint_hash == old.file_hashes['live_backup']
    # Durable but unaccepted files are evidence, not recovery authority.
    assert len(list((tmp_path / '.recovery-generations').iterdir())) == 2


def test_wrong_epoch_build_admission_or_skipped_sequence_cannot_publish(store_api, db, tmp_path):
    import json
    from startup_recovery.checkpoints import ProducedEnvelope
    p = publication()
    token = admitted(store_api, db)
    for path, value in [('epoch', 999), ('build_id', 'f' * 40), ('generation', 2)]:
        body = json.loads(envelope(token).raw)
        body['identity'][path] = value
        with pytest.raises(store_api.RecoveryError):
            p.publish(db, token, tmp_path, ProducedEnvelope('live_backup', json.dumps(body).encode()), expected_parent=None)
    assert list(tmp_path.iterdir()) == []


def test_runtime_writer_requires_admission_and_never_promotes_unchanged_legacy(store_api, db, tmp_path):
    p = publication()
    assert hasattr(p, 'RuntimeWriter'), 'Admitted runtime writer missing'
    token = admitted(store_api, db)
    import json
    body = json.loads(envelope(token).raw)
    legacy = tmp_path / 'live_backup.json'
    legacy.write_text('{"protected_sl":"1.1025"}')
    original = legacy.read_bytes()
    writer = p.RuntimeWriter(db, token, {'live_backup': (
        str(legacy), body['identity'], {'protected_sl': '1.1025'})}, 'd' * 64)
    # Reconciled candidate is merely read; publication of the same bytes is a no-op.
    assert writer.write(legacy, 'live_backup', {'protected_sl': '1.1025'}) is None
    assert not (tmp_path / '.recovery-generations').exists()
    writer.write(legacy, 'live_backup', {'protected_sl': '1.1030'})
    assert p.load(db, token.scope, tmp_path, 'live_backup').payload == {'protected_sl': '1.1030'}
    assert legacy.read_bytes() == original
    with pytest.raises(store_api.RecoveryError, match='CHECKPOINT_PATH_INVALID'):
        writer.write(tmp_path / 'wrong.json', 'live_backup', {'protected_sl': '1.1040'})


def test_runtime_writer_without_explicit_context_never_writes(tmp_path):
    p = publication()
    assert hasattr(p, 'write_runtime'), 'Runtime checkpoint gate missing'
    with pytest.raises(p.RecoveryError, match='CHECKPOINT_PRODUCER_NOT_ADMITTED'):
        p.write_runtime(tmp_path / 'live_backup.json', 'live_backup', {'risk': 1})
    assert list(tmp_path.iterdir()) == []


def test_runtime_reader_uses_admitted_generation_not_retained_legacy_file(store_api, db, tmp_path):
    import json
    p = publication()
    token = admitted(store_api, db)
    fields = json.loads(envelope(token).raw)['identity']
    target = tmp_path / 'live_backup.json'
    target.write_text('{"protected_sl":"untrusted-old"}')
    writer = p.RuntimeWriter(db, token, {'live_backup': (
        str(target), fields, {'protected_sl': '1.1025'})}, 'd' * 64)
    with p.checkpoint_producer(writer):
        assert p.read_runtime(target, 'live_backup') == {'protected_sl': '1.1025'}
        writer.write(target, 'live_backup', {'protected_sl': '1.1030'})
        value = p.read_runtime(target, 'live_backup')
        assert value == {'protected_sl': '1.1030'}
        value['protected_sl'] = 'modified-copy'
        assert p.read_runtime(target, 'live_backup') == {'protected_sl': '1.1030'}
    assert target.read_text() == '{"protected_sl":"untrusted-old"}'
    with pytest.raises(p.RecoveryError, match='CHECKPOINT_PRODUCER_NOT_ADMITTED'):
        p.read_runtime(target, 'live_backup')


@pytest.mark.parametrize('database_unavailable', [False, True])
def test_critical_checkpoint_failure_revokes_owner_even_if_block_commit_is_unavailable(
    store_api, db, tmp_path, monkeypatch, database_unavailable
):
    import json
    from startup_recovery.types import Phase
    p = publication()
    token = admitted(store_api, db)
    with db.begin() as session:
        store_api.advance(session, token, Phase.POSITION_MANAGEMENT_READY,
                          Phase.NEW_ENTRIES_READY, 'd' * 64)
    class InterruptedDB:
        def __call__(self): return db()
        def begin(self): raise RuntimeError('DB unavailable during revocation')
    factory = InterruptedDB() if database_unavailable else db
    target = tmp_path / 'live_backup.json'
    writer = p.RuntimeWriter(factory, token, {'live_backup': (
        str(target), json.loads(envelope(token).raw)['identity'], {'risk': '50'})}, 'd' * 64)
    def fail_publish(*args, **kwargs): raise OSError('disk unavailable')
    monkeypatch.setattr(p, 'publish', fail_publish)
    with pytest.raises(OSError):
        writer.write(target, 'live_backup', {'risk': '51'})
    with db() as session:
        assert not store_api.entries_ready(session, token)
        with pytest.raises(p.RecoveryError):
            store_api.require_owner(session, token)
    # Retrying the old capability after DB recovery cannot revive the manager.
    with pytest.raises(p.RecoveryError):
        writer.read(target, 'live_backup')


def test_reconciled_absence_is_distinct_from_unchanged_in_memory_defaults(store_api, db, tmp_path):
    import json
    p = publication()
    token = admitted(store_api, db)
    target = tmp_path / 'live_backup.json'
    writer = p.RuntimeWriter(db, token, {'live_backup': (
        str(target), json.loads(envelope(token).raw)['identity'], {'active': {}})},
        'd' * 64, absent_kinds={'live_backup'})
    assert writer.read(target, 'live_backup') is None
    assert writer.write(target, 'live_backup', {'active': {}}) is None
    assert writer.read(target, 'live_backup') is None
    assert list(tmp_path.iterdir()) == []
    writer.write(target, 'live_backup', {'active': {'EURUSD': {'position_id': '42'}}})
    assert writer.read(target, 'live_backup') == {'active': {'EURUSD': {'position_id': '42'}}}
    assert not target.exists()  # Original absent path remains absent; committed envelope is separate.


@pytest.mark.parametrize('kind', ['paper_backup', 'visits', 'feature_flags'])
def test_non_live_checkpoint_failure_is_isolated_and_sticky(store_api, db, tmp_path, monkeypatch, kind):
    import json
    p = publication()
    token = admitted(store_api, db)
    target = tmp_path / (kind + '.json')
    before = [] if kind == 'visits' else {'value': 1}
    after = [{'time': 1, 'visitor_id': 'v', 'country': 'Local'}] if kind == 'visits' else {'value': 2}
    writer = p.RuntimeWriter(db, token, {kind: (
        str(target), json.loads(envelope(token).raw)['identity'], before)}, 'd' * 64)
    def interrupted(*args, **kwargs): raise OSError('interrupted')
    monkeypatch.setattr(p, 'publish', interrupted)
    with pytest.raises(OSError):
        writer.write(target, kind, after)
    with db() as session:
        store_api.require_owner(session, token)
    with pytest.raises(p.RecoveryError, match='CHECKPOINT_KIND_REVOKED'):
        writer.read(target, kind)
    with pytest.raises(p.RecoveryError, match='CHECKPOINT_KIND_REVOKED'):
        writer.write(target, kind, before)


def test_failed_critical_checkpoint_read_cannot_resume_after_database_returns(store_api, db, tmp_path):
    import json
    p = publication()
    token = admitted(store_api, db)
    class Unavailable:
        def __call__(self): raise OSError('private database details')
        def begin(self): raise OSError('private database details')
    target = tmp_path / 'live_backup.json'
    writer = p.RuntimeWriter(Unavailable(), token, {'live_backup': (
        target, json.loads(envelope(token).raw)['identity'], {})}, 'd' * 64)
    with pytest.raises(p.RecoveryError, match='RECOVERY_CHECKPOINT_READ_FAILED'):
        writer.read(target, 'live_backup')
    with db() as session:
        with pytest.raises(p.RecoveryError, match='RECOVERY_TOKEN_INVALID'):
            store_api.require_owner(session, token)


def test_new_produced_state_uses_fresh_dependencies_without_rebinding_unchanged_state(store_api, db, tmp_path):
    import json
    from unittest.mock import Mock
    p = publication()
    token = admitted(store_api, db)
    target = tmp_path / 'live_backup.json'
    provider = Mock(return_value='f' * 64)
    fields = json.loads(envelope(token).raw)['identity']
    writer = p.RuntimeWriter(db, token, {'live_backup': (target, fields, {'value': 1})},
        'd' * 64, dependency_provider=provider)
    assert writer.write(target, 'live_backup', {'value': 1}) is None
    provider.assert_not_called()
    writer.write(target, 'live_backup', {'value': 2})
    provider.assert_called_once_with('live_backup', {'value': 2})
    from models import RecoveryCheckpointHead
    with db() as session:
        head = session.query(RecoveryCheckpointHead).one()
        assert head.identity['dependencies_hash'] == 'f' * 64
    assert not target.exists()
