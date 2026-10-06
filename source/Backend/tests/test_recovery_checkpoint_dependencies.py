import copy
import pytest
from test_recovery_reconciliation import empty
from test_recovery_store import store_api, db


def semantic_fixture():
    database, _ = empty()
    database['runtime_owners'] = ['owner']
    database['saved_strategies'] = [dict(owner_id='owner', strategy_id='saved',
        updated_at='version-1', schema_version=1, definition_json={'symbol': 'EURUSD', 'risk': '1'})]
    database['strategy_selections'] = [dict(owner_id='owner', strategy_id='saved', updated_at='poll-1')]
    database['stream_generations'] = [dict(root_key='root', timeframe='15m', generation=1,
        public_symbol='EURUSD', scope='CTRADER:DEMO:7', status='ACTIVE',
        storage_key='storage', configuration_version='v1', created_at='poll-1')]
    database['stream_heads'] = [dict(root_key='root', timeframe='15m', active_generation=1)]
    database['event_lifecycles'] = [dict(event_id='event', owner_id='owner', account_id='7',
        mode='LIVE', status='READY', updated_at='poll-1', consumed_at=None)]
    return database, {'CTRADER:DEMO:7:EURUSD': {'symbol': 'EURUSD', 'indicator_event_id': 'event'}}


def producer(database):
    from types import SimpleNamespace
    from startup_recovery.server_adapter import ProductionDependencies
    from test_recovery_reconciliation import SCOPE
    instance = ProductionDependencies.__new__(ProductionDependencies)
    instance.scope = SCOPE
    instance._baseline_database = copy.deepcopy(database)
    instance.readers = SimpleNamespace(database=lambda scope: copy.deepcopy(database))
    return instance


@pytest.mark.parametrize('kind', ['final_signal_hold', 'fifteen_m_swing_watch'])
def test_polling_and_unrelated_rows_do_not_rebind_populated_signal(kind):
    database, payload = semantic_fixture()
    service = producer(database)
    before = service.produced_dependencies(kind, payload)
    database['event_lifecycles'][0]['updated_at'] = 'poll-2'
    database['strategy_selections'][0]['updated_at'] = 'poll-2'
    database['lifecycles'].append({'setup_id': 'unrelated', 'updated_at': 'poll-2'})
    database['saved_strategies'].append({'owner_id': 'owner', 'strategy_id': 'not-selected'})
    assert service.produced_dependencies(kind, payload) == before


@pytest.mark.parametrize('change', ['config', 'saved_version', 'generation', 'selection', 'account'])
def test_semantically_relevant_change_rejects_old_signal(change):
    from startup_recovery.types import RecoveryError
    database, payload = semantic_fixture()
    service = producer(database)
    service.produced_dependencies('final_signal_hold', payload)
    if change == 'config': database['saved_strategies'][0]['definition_json']['risk'] = '2'
    if change == 'saved_version': database['saved_strategies'][0]['updated_at'] = 'version-2'
    if change == 'generation': database['stream_heads'][0]['active_generation'] = 2
    if change == 'selection': database['strategy_selections'][0]['strategy_id'] = 'another'
    if change == 'account': database['selection']['account_id'] = 'another'
    with pytest.raises(RecoveryError):
        service.produced_dependencies('final_signal_hold', payload)


@pytest.mark.parametrize('kind', ['final_signal_hold', 'fifteen_m_swing_watch'])
def test_empty_signal_has_no_unrelated_execution_dependencies(kind):
    database, _ = semantic_fixture()
    service = producer(database)
    before = service.produced_dependencies(kind, {})
    for key in ('saved_strategies', 'strategy_selections', 'stream_heads', 'stream_generations',
                'lifecycles', 'submissions', 'event_lifecycles'):
        database[key] = [{'irrelevant': 'changed'}]
    database['settings_revision'] = 'poll-2'
    assert service.produced_dependencies(kind, {}) == before


def test_new_consumption_can_publish_but_old_unconsumed_payload_cannot():
    from startup_recovery.types import RecoveryError
    database, payload = semantic_fixture()
    service = producer(database)
    database['event_lifecycles'][0].update(status='CONSUMED', consumed_at='2026-10-01T00:00:00Z')
    with pytest.raises(RecoveryError):
        service.produced_dependencies('final_signal_hold', payload)
    payload['CTRADER:DEMO:7:EURUSD']['consumed_at'] = '2026-10-01T00:00:00Z'
    assert len(service.produced_dependencies('final_signal_hold', payload)) == 64


def test_runtime_read_rechecks_relevant_version_even_without_a_write(store_api, db, tmp_path):
    import json
    from startup_recovery.checkpoint_store import RuntimeWriter
    from startup_recovery.types import RecoveryError
    from test_recovery_checkpoint_publication import admitted, envelope
    database, payload = semantic_fixture()
    service = producer(database)
    token = admitted(store_api, db)
    fields = json.loads(envelope(token).raw)['identity']
    fields['dependencies_hash'] = service.produced_dependencies('final_signal_hold', payload)
    path = tmp_path / 'hold.json'
    writer = RuntimeWriter(db, token, {'final_signal_hold': (path, fields, payload)}, 'd' * 64,
                           dependency_provider=service.produced_dependencies)
    database['event_lifecycles'][0]['updated_at'] = 'poll-2'
    assert writer.read(path, 'final_signal_hold') == payload
    database['saved_strategies'][0]['definition_json']['risk'] = '2'
    with pytest.raises(RecoveryError, match='CHECKPOINT_EXECUTION_VERSION_CHANGED'):
        writer.read(path, 'final_signal_hold')
    assert not path.exists()


def test_accepted_position_polling_does_not_invalidate_checkpoint_identity():
    from startup_recovery.reconcile import checkpoint_dependencies
    from test_recovery_open_position import occupied
    database, _ = occupied()
    payload = {'live_active_orders': {'EURUSD': {'position_id': '42'}}}
    before = checkpoint_dependencies('live_backup', database, payload)
    database['lifecycles'][0]['updated_at'] = 'poll-2'
    database['lifecycles'][0]['management_state']['last_checked_at'] = 'poll-2'
    assert checkpoint_dependencies('live_backup', database, payload) == before
    database['lifecycles'][0]['entry_binding']['frozen_plan_hash'] = 'different'
    assert checkpoint_dependencies('live_backup', database, payload) != before


def test_other_owner_consumption_and_retired_generation_are_not_dependencies():
    database, payload = semantic_fixture()
    service = producer(database)
    before = service.produced_dependencies('final_signal_hold', payload)
    database['event_lifecycles'].append(dict(database['event_lifecycles'][0], owner_id='other', status='CONSUMED'))
    database['stream_generations'].append(dict(database['stream_generations'][0], generation=0, status='RETIRED'))
    assert service.produced_dependencies('final_signal_hold', payload) == before


def test_discovery_and_admission_identity_ignore_management_poll_timestamps_only():
    from startup_recovery.reconcile import discover, reconcile
    from test_recovery_reconciliation import SCOPE, TOKEN, snapshot, recovery
    from test_recovery_open_position import occupied
    from types import SimpleNamespace
    database, broker = occupied()
    before = reconcile(snapshot(recovery(), database, broker), {})
    reads = []
    def polling(scope):
        changed = copy.deepcopy(database)
        row = changed['lifecycles'][0]
        row['updated_at'] = 'poll-' + str(len(reads))
        row['management_state']['last_management_timestamp'] = row['updated_at']
        reads.append(changed)
        return changed
    observed = discover(SimpleNamespace(database=polling, broker=lambda scope: copy.deepcopy(broker)), SCOPE, TOKEN)
    assert len(reads) == 2
    assert reconcile(observed, {}).manifest_hash == before.manifest_hash
    # A genuine protection change remains incompatible, not ignored as polling.
    database['lifecycles'][0]['management_state']['protection_state'] = 'PENDING'
    assert not reconcile(snapshot(recovery(), database, broker), {}).ok


def test_dependencies_follow_relevant_authority_not_unrelated_account_samples():
    from startup_recovery.reconcile import checkpoint_dependencies
    database, _ = empty()
    database.update(runtime_owners=['owner'], stream_generations=[],
        event_lifecycles=[], saved_strategies=[], strategy_selections=[])
    before = checkpoint_dependencies('app_settings', database)
    changed = copy.deepcopy(database)
    changed['lifecycles'] = [{'setup_id': 'new-position'}]
    assert checkpoint_dependencies('app_settings', changed) == before
    assert checkpoint_dependencies('live_backup', changed) == checkpoint_dependencies('live_backup', database)
    changed = copy.deepcopy(database)
    changed['stream_generations'] = [{'generation': 2, 'public_symbol': 'EURUSD', 'status': 'ACTIVE'}]
    payload = {'EURUSD': {'symbol': 'EURUSD'}}
    assert checkpoint_dependencies('final_signal_hold', changed, payload) != checkpoint_dependencies('final_signal_hold', database, payload)
    changed = copy.deepcopy(database)
    changed['selection']['revision'] = 'selected-again'
    assert checkpoint_dependencies('app_settings', changed) != before


def test_missing_stream_evidence_cannot_authorize_signal_checkpoint():
    from startup_recovery.reconcile import checkpoint_dependencies
    from startup_recovery.types import RecoveryError
    database, _ = empty()
    del database['stream_generations']
    with pytest.raises(RecoveryError, match='CHECKPOINT_DEPENDENCIES_UNAVAILABLE'):
        checkpoint_dependencies('final_signal_hold', database)


def test_live_checkpoint_cannot_acquire_missing_execution_identity_from_current_database():
    from startup_recovery.publication import validate_produced_checkpoint
    from startup_recovery.types import RecoveryError
    from test_recovery_open_position import occupied
    database, broker = occupied()
    legacy = {'live_active_orders': {'EURUSD': {'position_id': '42'}}}
    with pytest.raises(RecoveryError, match='CHECKPOINT_EXECUTION_IDENTITY_MISSING'):
        validate_produced_checkpoint('live_backup', legacy, database)


def test_signal_checkpoint_does_not_rebind_an_unconsumed_signal_to_consumed_db_evidence():
    from startup_recovery.publication import validate_produced_checkpoint
    from startup_recovery.types import RecoveryError
    database, _ = empty()
    database['runtime_owners'] = ['owner']
    database['event_lifecycles'] = [{'event_id': 'event', 'status': 'CONSUMED',
        'owner_id': 'owner', 'account_id': '7', 'mode': 'LIVE'}]
    with pytest.raises(RecoveryError, match='CHECKPOINT_EVENT_ALREADY_CONSUMED'):
        validate_produced_checkpoint('final_signal_hold',
            {'EURUSD': {'source_indicator_event_id': 'event'}}, database)
    validate_produced_checkpoint('final_signal_hold',
        {'EURUSD': {'source_indicator_event_id': 'event', 'consumed_at': '2026-10-01T00:00:00Z'}}, database)
