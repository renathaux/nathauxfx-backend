"""Publication staging is pure and preserves execution-time intent."""
import copy
import importlib
import pytest
from test_recovery_reconciliation import recovery, snapshot, empty
from test_recovery_open_position import occupied
from startup_recovery.types import RecoveryError


def publication():
    try:
        return importlib.import_module('startup_recovery.publication')
    except ModuleNotFoundError:
        pytest.fail('Pure recovery publication staging missing')


def accepted_candidate(tmp_path, database, kind, payload):
    from startup_recovery.checkpoints import produced_envelope, write_generation, read_committed
    from startup_recovery.reconcile import checkpoint_dependencies
    from test_recovery_checkpoints import identity
    fields = identity()
    fields.update(account_scope='CTRADER:DEMO:7',
        dependencies_hash=checkpoint_dependencies(kind, database, payload))
    created = produced_envelope(kind, payload, fields, parent_hash=None,
        admission_hash='d' * 64, produced_event_id='new-state', timestamp='2026-10-01T12:00:00Z')
    manifest = write_generation(tmp_path, {kind: created}, None)
    database['checkpoint_heads'] = [dict(kind=kind, identity=fields,
        file_hash=manifest.file_hashes[kind], manifest_hash=manifest.manifest_hash)]
    return read_committed(tmp_path, manifest.manifest_hash)


def test_bounded_history_retains_accepted_scoped_close_and_execution_watermarks(tmp_path):
    api = recovery()
    d, b = empty()
    d['runtime_owners'] = ['owner']
    payload = dict(live_active_orders={}, account_close_times={'CTRADER:DEMO:7': {'EURUSD': 900}},
        live_last_execution_time={'EURUSD': 800}, last_live_reset=0)
    candidates = accepted_candidate(tmp_path, d, 'live_backup', payload)
    source = snapshot(api, d, b)
    result = api.reconcile(source, candidates)
    assert result.ok
    output = publication().runtime_projection(result, source)
    assert output['last_position_closed_at']['EURUSD'] == 900
    assert output['last_execution_time']['EURUSD'] == 800
    assert output['closed_history'] == []  # No invented deal outside bounded history.


@pytest.mark.parametrize('bad', [None, 'bad', -1])
def test_invalid_accepted_watermark_is_not_defaulted_to_zero(tmp_path, bad):
    api = recovery()
    d, b = empty()
    d['runtime_owners'] = ['owner']
    payload = dict(live_active_orders={}, account_close_times={'CTRADER:DEMO:7': {'EURUSD': bad}},
        live_last_execution_time={}, last_live_reset=0)
    candidates = accepted_candidate(tmp_path, d, 'live_backup', payload)
    source = snapshot(api, d, b)
    with pytest.raises(RecoveryError):
        publication().runtime_projection(api.reconcile(source, candidates), source)


def test_stage_open_position_keeps_actual_levels_separate_from_frozen_intent():
    api = recovery()
    d, b = occupied()
    before = copy.deepcopy((d, b))
    source = snapshot(api, d, b)
    result = api.reconcile(source, {})
    staged = publication().stage(result, source)
    trade = staged.active_orders['EURUSD']
    assert trade['position_id'] == '42'
    assert trade['studio_setup_id'] == 'setup'
    assert trade['studio_strategy_id'] == 'saved-old'
    assert trade['entry'] == '1.1000'
    assert trade['sl'] == '1.0950'
    assert trade['planned_sl'] == '1.0950'
    assert trade['tp1'] == '1.1050'
    assert trade['tp2'] == '1.1100'
    assert trade['risk_amount'] == '50'
    assert staged.account['balance'] == '9896.51'
    trade['sl'] = 'bad-mutated-copy'
    assert staged.active_orders['EURUSD']['sl'] == '1.0950'
    assert (d, b) == before


def test_stage_does_not_enable_missing_or_disabled_preferences():
    api = recovery()
    d, b = empty()
    d['settings'] = [dict(setting_name='live_auto_trade_enabled', setting_value='false')]
    source = snapshot(api, d, b)
    staged = publication().stage(api.reconcile(source, {}), source)
    assert staged.preferences == {'live_auto_trade_enabled': False}
    assert staged.active_orders == {}
    assert staged.checkpoints == {}


def test_stage_cannot_publish_conflicting_or_different_epoch_result():
    from dataclasses import replace
    api = recovery()
    source = snapshot(api)
    result = api.reconcile(source, {})
    for invalid in (replace(result, epoch=result.epoch + 1),
                    replace(result, conflicts=('RECOVERY_POSITION_AMBIGUOUS',))):
        with pytest.raises(RecoveryError):
            publication().stage(invalid, source)


def test_two_same_symbol_positions_cannot_be_collapsed_into_one_legacy_slot():
    api = recovery()
    d, b = occupied()
    second = copy.deepcopy(d['lifecycles'][0])
    second.update(setup_id='setup-2', broker_position_id='43')
    d['lifecycles'].append(second)
    b['positions'].append({**b['positions'][0], 'position_id': '43'})
    source = snapshot(api, d, b)
    result = api.reconcile(source, {})
    assert result.ok and result.capacity_used == 2
    with pytest.raises(RecoveryError, match='RECOVERY_RUNTIME_POSITION_CAPACITY_UNSUPPORTED'):
        publication().stage(result, source)


def test_risk_history_is_exact_and_missing_monetary_evidence_is_not_zero():
    from decimal import Decimal
    project = publication().project_closed_history
    deal = dict(deal_id='1', position_id='42', order_id='2', is_close=True,
        symbol='EURUSD', side='SELL', execution_timestamp='2026-10-01T00:00:00+00:00',
        execution_price='1.12345', volume_units='5000',
        close_detail=dict(moneyDigits=2, grossProfit=12345, swap=-23,
                          commission=-150, pnlConversionFee=0, entryPrice='1.12000'))
    source = copy.deepcopy(deal)
    rows = project([deal], 'CTRADER:DEMO:7')
    assert rows[0]['pnl'] == Decimal('121.72')
    assert rows[0]['position_id'] == '42' and rows[0]['account_scope'] == 'CTRADER:DEMO:7'
    assert deal == source
    del deal['close_detail']['grossProfit']
    with pytest.raises(RecoveryError, match='RECOVERY_RISK_HISTORY_UNRESOLVED'):
        project([deal], 'CTRADER:DEMO:7')


def test_paper_restore_cannot_backfill_identity_or_partially_publish_invalid_state(monkeypatch):
    from strategies import shared
    original = shared.snapshot_paper_backup()
    def no_uuid(): pytest.fail('restoration must not invent historical identity')
    monkeypatch.setattr(shared.uuid, 'uuid4', no_uuid)
    payload = dict(paper_trades={}, paper_active_trades=[
        dict(symbol='EURUSD', status='OPEN', opened_at='2026-10-01T00:00:00Z')],
        paper_trade_history=[], paper_setup_locks={}, last_paper_reset=0)
    with pytest.raises(RecoveryError, match='RECOVERY_PAPER_IDENTITY_MISSING'):
        shared.restore_admitted_paper_payload(payload)
    assert shared.snapshot_paper_backup() == original


def test_runtime_projection_preserves_absence_and_does_not_invent_checkpoint_owner():
    api = recovery()
    d, b = empty()
    source = snapshot(api, d, b)
    result = api.reconcile(source, {})
    with pytest.raises(RecoveryError, match='RECOVERY_RUNTIME_OWNER_UNRESOLVED'):
        publication().runtime_projection(result, source)
    d['runtime_owners'] = ['owner']
    source = snapshot(api, d, b)
    output = publication().runtime_projection(api.reconcile(source, {}), source)
    assert output['owner_id'] == 'owner'
    assert output['preferences'] == {}
    assert output['closed_history'] == []
    assert output['checkpoints'] == {}
    assert output['last_execution_time'] == {'EURUSD': 0, 'XAUUSD': 0}
    d['runtime_owners'] = ['owner', 'other']
    source = snapshot(api, d, b)
    with pytest.raises(RecoveryError, match='RECOVERY_RUNTIME_OWNER_UNRESOLVED'):
        publication().runtime_projection(api.reconcile(source, {}), source)


def test_execution_checkpoint_identity_copies_only_preserved_binding():
    project = publication().execution_checkpoint_identity
    payload = {'studio_binding': {'strategy_identity': {'owner_id': 'owner',
        'strategy_id': 'saved', 'config_hash': 'a' * 64}, 'frozen_plan_hash': 'b' * 64}}
    result = project(payload)
    assert result == {'strategy_identity': {'owner_id': 'owner', 'strategy_id': 'saved',
        'config_hash': 'a' * 64}, 'frozen_plan_hash': 'b' * 64}
    result['strategy_identity']['owner_id'] = 'mutated'
    assert payload['studio_binding']['strategy_identity']['owner_id'] == 'owner'
    assert project({'studio_strategy_id': 'saved'}) == {
        'strategy_identity': None, 'frozen_plan_hash': None}


def test_published_watermark_supports_existing_float_clock_cooldown():
    api = recovery()
    d, b = empty()
    d['runtime_owners'] = ['owner']
    d['submissions'] = [dict(attempt_status='ACCEPTED', symbol='EURUSD',
        reconciliation_status='NOT_REQUIRED',
        request_started_at='2026-10-01T00:00:00.125000+00:00')]
    source = snapshot(api, d, b)
    output = publication().runtime_projection(api.reconcile(source, {}), source)
    assert 1790812801.125 - output['last_execution_time']['EURUSD'] == 1.0
