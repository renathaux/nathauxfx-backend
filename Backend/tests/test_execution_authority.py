from unittest.mock import Mock
from services.execution_authority import resolve_execution_authority

PROFILE = {'enabled': True, 'strategy_id': 'gold-v18', 'enabled_strategy_id': 'gold-v18', 'strategy_name': 'Gold 931 v18', 'symbols': ['XAUUSD']}

def test_gold_only_is_exclusive_and_eur_has_no_fallback():
    gold = resolve_execution_authority('XAUUSD', owner_id='owner', profile=PROFILE, legacy_symbols=['EURUSD', 'XAUUSD'])
    eur = resolve_execution_authority('EURUSD', owner_id='owner', profile=PROFILE, legacy_symbols=['EURUSD'])
    assert gold['source'] == 'STRATEGY_STUDIO'
    assert gold['strategy_id'] == 'gold-v18'
    assert eur['source'] == 'NONE'
    assert eur['reason'] == 'NO_LIVE_STRATEGY_FOR_SYMBOL'

def test_default_is_none_and_legacy_requires_explicit_symbol():
    assert resolve_execution_authority('EURUSD')['source'] == 'NONE'
    assert resolve_execution_authority('EURUSD', legacy_symbols=['XAUUSD'])['source'] == 'NONE'
    assert resolve_execution_authority('EURUSD', legacy_symbols=['EURUSD'])['source'] == 'V3B'

def test_inconsistent_live_selection_fails_closed():
    result = resolve_execution_authority('XAUUSD', owner_id='owner', profile={**PROFILE, 'enabled_strategy_id': 'other'}, legacy_symbols=['XAUUSD'])
    assert result['source'] == 'NONE'
    assert result['reason'] == 'LIVE_STRATEGY_SELECTION_MISMATCH'

def test_installed_runtime_never_builds_v3b_for_studio_or_none(monkeypatch):
    from tests.test_strategy_lab_live_v3b_runtime_install import _fake_api
    from services import live_v3b_runtime_install as runtime
    fake, *_ = _fake_api()
    original = fake.run_ctrader_auto_trade_checks
    fake.get_execution_authority = lambda symbol: resolve_execution_authority(symbol, owner_id='owner', profile=PROFILE)
    builder = Mock(side_effect=AssertionError('V3B bypass'))
    monkeypatch.setattr(runtime, 'build_live_v3b_candidate', builder)
    monkeypatch.setattr(runtime, 'live_v3b_enabled', lambda: True)
    runtime.install_live_v3b_runtime(fake)
    fake.run_ctrader_auto_trade_checks({})
    original.assert_called_once()
    builder.assert_not_called()

def test_none_never_reads_legacy_plan_or_evaluates_candidate(monkeypatch):
    import api
    monkeypatch.setattr(api, 'get_execution_authority', lambda symbol: resolve_execution_authority(symbol, owner_id='owner', profile=PROFILE))
    monkeypatch.setattr(api, 'get_panel_trade_plan', Mock(side_effect=AssertionError('legacy plan read')))
    monkeypatch.setattr(api, 'build_studio_candidate', Mock(side_effect=AssertionError('unassigned evaluated')))
    assert api.select_auto_execution_candidate({}, 'EURUSD')['source'] == 'NONE'

def test_submission_rejects_stale_v3b_before_preparing_or_sending(monkeypatch):
    import api
    monkeypatch.setattr(api, 'get_execution_authority', lambda symbol: resolve_execution_authority(symbol, owner_id='owner', profile=PROFILE))
    prepare = Mock(side_effect=AssertionError('stale request prepared'))
    monkeypatch.setattr(api, 'prepare_ctrader_trade', prepare)
    for symbol in ('XAUUSD', 'EURUSD'):
        result = api._execute_live_order_core_impl({'symbol': symbol, 'signal': 'SELL'}, source='auto')
        assert result['ok'] is False
        assert result['order_sent'] is False
    prepare.assert_not_called()

def test_none_blocks_before_news_can_replace_it(monkeypatch):
    import api
    from services.live_v3b_runtime_install import install_live_v3b_runtime
    original = api.run_ctrader_auto_trade_checks
    # Production installer retains its captured original function in closure.
    cycle = next((cell.cell_contents for cell in (original.__closure__ or ()) if callable(cell.cell_contents) and getattr(cell.cell_contents, '__name__', '') == 'run_ctrader_auto_trade_checks'), original)
    monkeypatch.setattr(api, 'refresh_auto_trade_state_from_persistence', lambda *a: None)
    monkeypatch.setattr(api, 'sync_ctrader_account_state', lambda: None)
    monkeypatch.setattr(api, 'LIVE_AUTO_TRADE_ENABLED', {'enabled': True})
    monkeypatch.setattr(api, 'get_execution_authority', lambda symbol: resolve_execution_authority(symbol))
    monkeypatch.setattr(api, 'evaluate_news_entry_state', Mock(side_effect=AssertionError('news replaced NONE')))
    monkeypatch.setattr(api, 'set_auto_trade_status', Mock())
    results = cycle({})
    assert len(results) == 2
    assert all(row['reason'] == 'NO_LIVE_STRATEGY_FOR_SYMBOL' for row in results)
    monkeypatch.setattr(api, 'LIVE_AUTO_TRADE_ENABLED', {'enabled': False})
    monkeypatch.setattr(api, 'select_auto_execution_candidate', Mock(side_effect=AssertionError('Auto OFF evaluated')))
    monkeypatch.setattr(api, 'get_panel_trade_plan', lambda *a: {})
    assert cycle({}) == []


def test_display_none_and_gold_share_entry_authority(monkeypatch):
    import api
    from ctrader_account_context import AccountIdentity
    monkeypatch.setattr(api, 'current_identity', lambda: AccountIdentity('test', 'demo'))
    monkeypatch.setattr(api, 'get_execution_authority', lambda symbol: resolve_execution_authority(symbol, owner_id='owner', profile=PROFILE))
    monkeypatch.setattr(api, 'get_studio_live_display_profile', lambda owner: PROFILE)
    monkeypatch.setattr(api, 'get_ctrader_account_snapshot', lambda: {})
    monkeypatch.setattr(api, 'validate_verified_account_snapshot', lambda value: {'ok': True, 'balance': 10000})
    monkeypatch.setattr(api, 'load_strategy_studio_market_bundle', lambda *a: {})
    monkeypatch.setattr(api, 'build_studio_live_display', lambda *a, **kw: {'strategy_id': 'gold-v18', 'strategy_name': 'Gold 931 v18', 'signal': 'WAIT', 'conditions': [{'key': 'confirmation', 'state': 'WAITING'}]})
    result = api.refresh_live_strategy_display({})
    assert result['EURUSD']['execution_authority']['source'] == 'NONE'
    assert result['EURUSD']['reason'] == 'NO_LIVE_STRATEGY_FOR_SYMBOL'
    assert result['XAUUSD']['execution_authority']['strategy_id'] == 'gold-v18'
    assert result['XAUUSD']['evaluation']['conditions'][0]['key'] == 'confirmation'

import pytest

@pytest.mark.parametrize('broker_sl', [1.112, None])
def test_studio_sync_preserves_actual_broker_stop_without_legacy_repair(monkeypatch, broker_sl):
    import api
    import ctrader_connector as connector
    from services import account_execution_coordination as coordination
    state = {'active_account_id': '47784297', 'active_account_env': 'demo', '_durable_selection_authoritative': True}
    monkeypatch.setattr(connector, 'load_ctrader_account_settings', lambda: dict(state))
    existing = {'symbol': 'EURUSD', 'position_id': '42', 'account_scope': 'CTRADER:DEMO:47784297', 'side': 'BUY', 'entry': 1.1, 'sl': 1.09, 'tp1': 1.11, 'tp2': 1.119, 'result': 'RUNNING'}
    monkeypatch.setattr(api, 'LIVE_ACTIVE_ORDERS', {'EURUSD': existing, 'XAUUSD': None})
    monkeypatch.setattr(api, 'LIVE_TRADE_HISTORY', [])
    monkeypatch.setattr(api, 'LIVE_ACCOUNT_STATE', {'connected': True, 'mode': 'demo', 'broker': 'ctrader'})
    monkeypatch.setattr(api, 'sync_ctrader_account_state', lambda: None)
    monkeypatch.setattr(api, 'get_ctrader_position_fetch_error', lambda: None)
    monkeypatch.setattr(api, 'get_live_prices', lambda: {})
    monkeypatch.setattr(api, 'get_ctrader_symbol_risk_metadata', lambda *a, **kw: {})
    monkeypatch.setattr(connector, 'get_ctrader_symbol_risk_metadata', lambda *a, **kw: {})
    monkeypatch.setattr(api, 'get_signal_trade_plan', lambda symbol: {})
    monkeypatch.setattr(api, 'save_live_backup', lambda: None)
    monkeypatch.setattr(coordination, 'exclude_test_positions', lambda session, account, rows: rows)
    monkeypatch.setattr(api, 'get_enabled_studio_live_owner', lambda: 'owner')
    monkeypatch.setattr(api, 'studio_managed_owner_for_account', lambda *a: 'owner')
    monkeypatch.setattr(api, 'studio_managed_position_ids', lambda *a: {'42'})
    monkeypatch.setattr(api, 'studio_managed_position_states', lambda *a: {'42': {'strategy_id': 'saved-strategy', 'tp1': 1.11, 'tp2': 1.119, 'initial_sl': 1.09, 'broker_sl': broker_sl, 'protection_state': 'PENDING'}})
    repair = Mock(side_effect=AssertionError('generic repair touched Studio position'))
    monkeypatch.setattr(api, 'modify_position_sltp', repair)
    monkeypatch.setattr(api, 'get_open_positions', lambda: [{**existing, 'sl': broker_sl, 'take_profit': 1.12, 'volume': 1000, 'current_price': 1.115, 'profit': 1}])
    api.sync_live_positions()
    mirrored = api.LIVE_ACTIVE_ORDERS['EURUSD']
    assert mirrored['sl'] == broker_sl
    assert mirrored['current_sl'] == broker_sl
    assert mirrored['trade_management']['broker_sl'] == broker_sl
    assert mirrored['protection_confirmed'] is False
    repair.assert_not_called()
