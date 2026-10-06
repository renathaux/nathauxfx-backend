"""Frozen DB intent and broker facts stay separate; no management calls here."""
import copy
import pytest
from live_integrity.binding import fingerprint
from test_recovery_reconciliation import recovery, empty, snapshot
from test_broker_execution_metadata import snapshot as metadata_snapshot, NOW


def occupied():
    d, b = empty()
    definition = {'schema_version': 1, 'tp1': {'enabled': True, 'close_percent': 50}}
    plan = dict(symbol='EURUSD', side='BUY', account_scope='CTRADER:DEMO:7',
                entry='1.1000', sl='1.0950', tp1='1.1050', tp2='1.1100',
                intended_risk_dollars='50', account_balance='9896.51',
                protection_prices={'protected': '1.1025', 'step_levels': []})
    identity = dict(owner_id='owner', strategy_id='saved-old', updated_at='2026-09-01T00:00:00Z',
                    schema_version=1, canonical_version=1, config_hash=fingerprint(definition))
    row = dict(setup_id='setup', owner_id='owner', strategy_id='saved-old', account_id='7',
               account_scope='CTRADER:DEMO:7', symbol='EURUSD', direction='BUY', status='CONSUMED',
               broker_position_id='42', initial_volume_units=10000, tp1_requested_at=None,
               tp1_completed_at=None, protection_applied_at=None, management_suspended_at=None,
               definition_snapshot=definition, entry_binding={'strategy_identity': identity,
                    'frozen_plan': plan, 'frozen_plan_hash': fingerprint(plan)},
               execution_snapshot={'entry': '1.1000', 'initial_sl': '1.0950', 'tp2': '1.1100'},
               management_state={}, updated_at='revision-1')
    d['lifecycles'] = [row]
    b.update(observed_at=NOW, history_to=NOW, positions=[dict(position_id='42', symbol='EURUSD',
        symbol_id=1, side='BUY', entry='1.1000', sl='1.0950', tp2='1.1100', volume_units='10000')],
        metadata={'EURUSD': metadata_snapshot()})
    return d, b


def test_db_bound_position_recovers_exact_execution_intent_without_mutation():
    api = recovery()
    d, b = occupied()
    original = copy.deepcopy((d, b))
    result = api.reconcile(snapshot(api, d, b), {})
    assert result.ok, result.conflicts
    position = result.positions[0]
    assert position['position_id'] == '42'
    assert position['setup_id'] == 'setup' and position['strategy_id'] == 'saved-old'
    assert position['intended_risk'] == '50' and position['current_risk'] == '50'
    assert position['intended']['tp1'] == '1.1050'
    assert position['current']['sl'] == '1.0950'
    assert result.capacity_used == 1 and result.epoch == 3
    assert (d, b) == original


@pytest.mark.parametrize('change,reason', [
    ('missing_snapshot', 'RECOVERY_EXECUTION_SNAPSHOT_MISSING'),
    ('missing_version', 'RECOVERY_STRATEGY_IDENTITY_MISSING'),
    ('missing_risk', 'RECOVERY_RISK_UNRESOLVED'),
    ('manual_sl', 'RECOVERY_PROTECTION_CONFLICT'),
    ('unknown_volume', 'RECOVERY_VOLUME_CONFLICT'),
    ('unowned', 'RECOVERY_POSITION_AMBIGUOUS'),
    ('pending_tp1', 'RECOVERY_OPERATIONS_UNRESOLVED'),
    ('wrong_account', 'RECOVERY_ACCOUNT_CONFLICT')])
def test_recovery_never_guesses_original_risk_or_legacy_identity(change, reason):
    api = recovery()
    d, b = occupied()
    row = d['lifecycles'][0]
    if change == 'missing_snapshot': row['execution_snapshot'] = None
    if change == 'missing_version': row['entry_binding'] = None
    if change == 'missing_risk':
        row['entry_binding']['frozen_plan'].pop('intended_risk_dollars')
        row['entry_binding']['frozen_plan_hash'] = fingerprint(row['entry_binding']['frozen_plan'])
    if change == 'manual_sl': b['positions'][0]['sl'] = '1.1025'
    if change == 'unknown_volume': b['positions'][0]['volume_units'] = '5000'
    if change == 'unowned': d['lifecycles'] = []
    if change == 'pending_tp1': row['tp1_requested_at'] = 'request'
    if change == 'wrong_account': row['account_scope'] = 'CTRADER:LIVE:7'
    result = api.reconcile(snapshot(api, d, b), {})
    assert not result.ok and reason in result.conflicts
    assert result.capacity_used == 1  # Unowned/invalid exposure never disappears.


def test_current_saved_strategy_cannot_replace_execution_version():
    api = recovery()
    d, b = occupied()
    d['current_saved_strategy'] = {'strategy_id': 'saved-old', 'definition': {'risk': 999}}
    result = api.reconcile(snapshot(api, d, b), {})
    assert result.ok and result.positions[0]['intended_risk'] == '50'
    assert result.positions[0]['intended']['tp2'] == '1.1100'


def test_confirmed_partial_close_requires_correlated_deal_and_keeps_original_risk():
    api = recovery()
    d, b = occupied()
    row = d['lifecycles'][0]
    row.update(tp1_requested_at='2026-09-01T12:00:00+00:00',
               tp1_completed_at='2026-09-01T12:00:02+00:00',
               protection_applied_at='2026-09-01T12:00:03+00:00')
    row['management_state'] = dict(tp1_partial_close_confirmed=True, tp1_requested_volume=5000,
        tp1_volume_before=10000, tp1_closed_volume=5000, protection_state='CONFIRMED',
        target_protected_sl='1.1025', broker_confirmed_sl='1.1025')
    b['positions'][0].update(volume_units='5000', sl='1.1025')
    b['deals'] = [dict(deal_id='deal-1', position_id='42', is_close=True, volume_units='5000',
                       execution_timestamp='2026-09-01T12:00:01+00:00')]
    result = api.reconcile(snapshot(api, d, b), {})
    assert result.ok, result.conflicts
    assert result.positions[0]['intended_risk'] == '50'
    assert result.positions[0]['current_risk'] == '0'
    b['deals'] = []
    assert 'RECOVERY_VOLUME_CONFLICT' in api.reconcile(snapshot(api, d, b), {}).conflicts


def test_open_db_position_missing_from_complete_broker_view_requires_closed_deal_proof():
    api = recovery()
    d, b = occupied()
    b['positions'] = []
    result = api.reconcile(snapshot(api, d, b), {})
    assert not result.ok and 'RECOVERY_POSITION_DISAPPEARED' in result.conflicts


def test_exact_v3b_levels_alone_do_not_fabricate_missing_historical_identity():
    api = recovery()
    d, b = occupied()
    d['lifecycles'] = []
    d['legacy_executions'] = [dict(position_id='42', account_id='7', broker_environment='demo',
        snapshot_json=dict(strategy_execution_profile='V3B_M5_FROZEN', entry='1.1000',
            sl='1.0950', tp1='1.1050', tp2='1.1100', protected_sl_price='1.1025'))]
    original = copy.deepcopy(d)
    result = api.reconcile(snapshot(api, d, b), {})
    assert not result.ok and 'RECOVERY_STRATEGY_IDENTITY_MISSING' in result.conflicts
    assert d == original and result.capacity_used == 1


def test_matching_mutated_management_stop_is_not_authorized_by_broker_agreement():
    api = recovery()
    d, b = occupied()
    row = d['lifecycles'][0]
    row['protection_applied_at'] = '2026-09-01T12:00:00+00:00'
    row['management_state'] = dict(protection_state='CONFIRMED',
                                  target_protected_sl='1.1020', broker_confirmed_sl='1.1020')
    b['positions'][0]['sl'] = '1.1020'
    result = api.reconcile(snapshot(api, d, b), {})
    assert not result.ok and 'RECOVERY_PROTECTION_CONFLICT' in result.conflicts
