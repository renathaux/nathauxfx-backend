"""Read-only discovery must distinguish absence from failed/incomplete reads."""
import copy
import importlib
from types import SimpleNamespace
import pytest

from startup_recovery.types import AccountScope, ManagerToken, RecoveryError

SCOPE = AccountScope('ctrader', 'demo', '7')
TOKEN = ManagerToken(SCOPE, 'attempt', 3, 'boot')


def recovery():
    try:
        return importlib.import_module('startup_recovery.reconcile')
    except ModuleNotFoundError:
        pytest.fail('Pure recovery reconciliation missing')


def empty():
    selection = dict(account_id='7', environment='demo', revision='selection-1', scope='CTRADER:DEMO:7')
    db = dict(selection=selection, lifecycles=[], submissions=[], legacy_executions=[],
              risk_history_required_from=1000, settings_revision='settings-1',
              runtime_owners=[], stream_generations=[], stream_heads=[], event_lifecycles=[],
              saved_strategies=[], strategy_selections=[])
    broker = dict(account_id='7', environment='demo', complete=True, authenticated=True,
                  balance='9896.51', equity='9896.51', positions=[], orders=[], deals=[],
                  history_from=1000, history_to=2000, history_complete=True,
                  observed_at=2000, metadata={})
    return db, broker


def snapshot(api, db=None, broker=None):
    d, b = empty()
    return api.DiscoverySnapshot.create(TOKEN, db or d, broker or b)


def test_complete_empty_positions_are_distinct_from_failed_read():
    api = recovery()
    result = api.reconcile(snapshot(api), {})
    assert result.ok and result.positions == () and result.capacity_used == 0
    _, broken = empty()
    broken['complete'] = False
    blocked = api.reconcile(snapshot(api, broker=broken), {})
    assert not blocked.ok and 'RECOVERY_BROKER_INCOMPLETE' in blocked.conflicts


@pytest.mark.parametrize('field,value,reason', [
    ('account_id', '8', 'RECOVERY_ACCOUNT_CONFLICT'),
    ('history_complete', False, 'RECOVERY_RISK_HISTORY_UNRESOLVED'),
    ('history_from', 1001, 'RECOVERY_RISK_HISTORY_UNRESOLVED'),
    ('authenticated', False, 'RECOVERY_AUTH_REAUTH_REQUIRED'),
    ('balance', None, 'RECOVERY_RISK_UNRESOLVED')])
def test_incomplete_account_or_risk_evidence_blocks(field, value, reason):
    api = recovery()
    d, b = empty()
    b[field] = value
    assert reason in api.reconcile(snapshot(api, d, b), {}).conflicts


def test_unknown_order_and_pending_claim_are_not_removed_from_capacity():
    api = recovery()
    d, b = empty()
    d['submissions'] = [dict(id=1, attempt_status='SUBMITTING', reconciliation_status='PENDING')]
    b['orders'] = [dict(order_id='pending-1')]
    result = api.reconcile(snapshot(api, d, b), {})
    assert not result.ok and result.capacity_used == 2
    assert 'RECOVERY_OPERATIONS_UNRESOLVED' in result.conflicts


def test_legacy_candidate_never_changes_or_becomes_versioned(tmp_path):
    api = recovery()
    from startup_recovery.checkpoints import read_candidate
    path = tmp_path / 'backup.json'
    raw = b'{"live_active_orders":{"EURUSD":{"position_id":"missing-identity"}}}'
    path.write_bytes(raw)
    result = api.reconcile(snapshot(api), {'live_backup': read_candidate(path, 'live_backup')})
    assert not result.ok and 'RECOVERY_CHECKPOINT_INCOMPATIBLE:live_backup' in result.conflicts
    assert path.read_bytes() == raw and list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize('kind', ['visits', 'paper_backup', 'feature_flags'])
def test_analytics_corruption_does_not_block_unrelated_live(tmp_path, kind):
    api = recovery()
    from startup_recovery.checkpoints import read_candidate
    path = tmp_path / (kind + '.json')
    path.write_text('{')
    result = api.reconcile(snapshot(api), {kind: read_candidate(path, kind)})
    assert result.ok
    assert kind in result.quarantined


def test_discovery_has_two_pass_bound_and_no_snapshot_mutation():
    api = recovery()
    assert hasattr(api, 'discover'), 'Bounded discovery missing'
    d, b = empty()
    count = {'broker': 0}
    def changing_broker(scope):
        count['broker'] += 1
        return {**copy.deepcopy(b), 'orders': [{'order_id': str(count['broker'])}]}
    readers = SimpleNamespace(database=lambda scope: copy.deepcopy(d), broker=changing_broker)
    with pytest.raises(RecoveryError, match='RECOVERY_SNAPSHOT_UNSTABLE'):
        api.discover(readers, SCOPE, TOKEN)
    assert count['broker'] == 4
    result = api.discover(SimpleNamespace(database=lambda _: copy.deepcopy(d), broker=lambda _: copy.deepcopy(b)), SCOPE, TOKEN)
    altered = result.database
    altered['selection']['account_id'] = 'other'
    assert result.database['selection']['account_id'] == '7'


def test_versioned_checkpoint_requires_both_db_head_and_current_dependency_match(tmp_path):
    api = recovery()
    from startup_recovery.checkpoints import produced_envelope, write_generation, read_committed
    from test_recovery_checkpoints import identity
    d, b = empty()
    d['runtime_owners'] = ['owner']
    payload = {'EURUSD': {'event_id': 'event-1'}}
    dependencies = api.checkpoint_dependencies('final_signal_hold', d, payload)
    fields = identity()
    fields.update(account_scope='CTRADER:DEMO:7', dependencies_hash=dependencies)
    created = produced_envelope('final_signal_hold', payload, fields,
        parent_hash=None, admission_hash='d' * 64, produced_event_id='new-event',
        timestamp='2026-10-01T12:00:00Z')
    manifest = write_generation(tmp_path, {'final_signal_hold': created}, None)
    candidates = read_committed(tmp_path, manifest.manifest_hash)
    d['checkpoint_heads'] = [dict(kind='final_signal_hold', identity=fields,
        file_hash=manifest.file_hashes['final_signal_hold'], manifest_hash=manifest.manifest_hash)]
    result = api.reconcile(snapshot(api, d, b), candidates)
    assert result.ok, result.conflicts
    assert result.checkpoints['final_signal_hold'] == {'EURUSD': {'event_id': 'event-1'}}
    without_checkpoint = api.reconcile(snapshot(api, d, b), {})
    assert not without_checkpoint.ok
    assert 'RECOVERY_CHECKPOINT_MISSING:final_signal_hold' in without_checkpoint.conflicts
    assert result.manifest_hash != without_checkpoint.manifest_hash
    d['settings_revision'] = 'new-consumption-evidence'
    assert api.reconcile(snapshot(api, d, b), candidates).ok
    d['event_lifecycles'] = [{'event_id': 'event-1', 'status': 'CONSUMED',
        'owner_id': 'owner', 'account_id': '7', 'mode': 'LIVE'}]
    assert not api.reconcile(snapshot(api, d, b), candidates).ok
