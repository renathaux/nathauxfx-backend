"""Synthetic legacy inputs: validation never promotes candidate news to execution."""
import importlib
from copy import deepcopy

import pytest
from startup_recovery.checkpoints import _encoded, _parse
from startup_recovery.reconcile import DiscoverySnapshot
from startup_recovery.types import RecoveryError
from test_recovery_reconciliation import empty, TOKEN


def inputs():
    d, b = empty()
    d['runtime_owners'] = ['owner']
    d['settings'] = [dict(setting_name='live_auto_trade_enabled', setting_value='false')]
    payloads = dict(
        live_backup=dict(account_close_times={}, live_active_orders={}, live_trade_history=[],
                         live_last_execution_time={'EURUSD': 0, 'XAUUSD': 0},
                         live_last_position_closed_at={'EURUSD': 0, 'XAUUSD': 0}, last_live_reset=None),
        final_signal_hold={}, fifteen_m_swing_watch={},
        ctrader_accounts=dict(active_account_id=7, active_account_env='demo', accounts=[],
                              forgotten_account_ids=[], last_refresh=None, active_account_snapshot=None,
                              active_account_snapshot_refreshed_at=None),
        news_trading_state=dict(version=1, opportunities={
            'synthetic-event|EURUSD': dict(event_id='synthetic-event', event_name='Synthetic release',
                symbol='EURUSD', currency='USD', release_time='2020-01-01T12:30:00Z',
                status='PENDING', reason='WAITING_FOR_5M_CONFIRMATION', consumed=False,
                updated_at='2020-01-01T12:31:00Z',
                details={'ok': False, 'reason': 'WAITING_FOR_5M_CONFIRMATION'})},
            normal_fresh_after={}, audit=[], mode_enabled_at=None))
    return payloads, d, b


def validate(payloads, d, b):
    try:
        api = importlib.import_module('startup_recovery.legacy_semantics')
    except ModuleNotFoundError:
        pytest.fail('Pure legacy semantic validator is missing')
    raw = {k: _encoded(v) if not isinstance(v, bytes) else v for k, v in payloads.items()}
    return api.validate_legacy_payloads(raw, DiscoverySnapshot.create(TOKEN, d, b))


def test_valid_pending_and_expired_window_remain_byte_semantically_unchanged():
    p, d, b = inputs()
    before = deepcopy(p)
    result = validate(p, d, b)
    assert result.payloads == before == p
    assert set(result.dependency_hashes) == set(p)
    assert all(len(v) == 64 for v in result.dependency_hashes.values())
    result.payloads['live_backup']['live_active_orders']['bad'] = {}
    assert result.payloads == before
    for kind, raw in result.canonical_payloads:
        assert raw == _encoded(before[kind])


@pytest.mark.parametrize('field,value', [
    ('status', 'READY'), ('consumed', True), ('consumed', 0),
    ('symbol', 'eurusd'), ('symbol', 'GBPUSD'), ('event_id', 'other'),
    ('updated_at', '2020-01-01'), ('release_time', None),
    ('reason', 'EXECUTED'), ('details', {'ok': True, 'reason': 'WAITING_FOR_5M_CONFIRMATION'}),
    ('details', {'ok': False, 'reason': 'WAIT_NEWS_ATR_UNAVAILABLE'}),
    ('position_id', 'position'), ('execution_id', 'execution'), ('owner_id', 'owner'),
])
def test_news_contradictions_block(field, value):
    p, d, b = inputs()
    p['news_trading_state']['opportunities']['synthetic-event|EURUSD'][field] = value
    with pytest.raises(RecoveryError):
        validate(p, d, b)


@pytest.mark.parametrize('raw', [b'{"x":1,"x":2}', b'{"x":NaN}', b'[]', b'null'])
def test_ambiguous_or_wrong_json_blocks(raw):
    p, d, b = inputs()
    p['final_signal_hold'] = raw
    with pytest.raises(RecoveryError):
        validate(p, d, b)


@pytest.mark.parametrize('kind', ['live_backup', 'final_signal_hold', 'fifteen_m_swing_watch', 'news_trading_state', 'ctrader_accounts'])
def test_exact_five_required(kind):
    p, d, b = inputs()
    del p[kind]
    with pytest.raises(RecoveryError):
        validate(p, d, b)


def test_quarantined_kind_cannot_enter_adoption_set():
    p, d, b = inputs()
    p['paper_backup'] = {}
    with pytest.raises(RecoveryError):
        validate(p, d, b)


@pytest.mark.parametrize('target,field,value', [
    ('broker', 'account_id', '8'), ('broker', 'environment', 'live'),
    ('broker', 'positions', [{'position_id': 'x'}]), ('broker', 'orders', [{'order_id': 'x'}]),
    ('broker', 'complete', False), ('broker', 'authenticated', False),
    ('db', 'runtime_owners', []), ('db', 'runtime_owners', ['a', 'b']),
    ('db', 'submissions', [{'attempt_status': 'SUBMITTING'}]),
])
def test_unsafe_snapshot_blocks(target, field, value):
    p, d, b = inputs()
    (d if target == 'db' else b)[field] = value
    with pytest.raises(RecoveryError):
        validate(p, d, b)


@pytest.mark.parametrize('value', [True, -1, '1', None])
def test_watermark_strict_numeric_types(value):
    p, d, b = inputs()
    p['live_backup']['live_last_execution_time']['EURUSD'] = value
    with pytest.raises(RecoveryError):
        validate(p, d, b)


@pytest.mark.parametrize('kind', ['final_signal_hold', 'fifteen_m_swing_watch'])
def test_nonempty_unproven_signal_is_not_bound_to_current_strategy(kind):
    p, d, b = inputs()
    p[kind] = {'EURUSD': {'event_id': 'unproven', 'symbol': 'EURUSD'}}
    with pytest.raises(RecoveryError):
        validate(p, d, b)


def test_account_selection_must_match_not_cached_observation():
    p, d, b = inputs()
    p['ctrader_accounts']['active_account_id'] = 8
    with pytest.raises(RecoveryError):
        validate(p, d, b)


def test_live_history_cannot_recreate_active_execution():
    p, d, b = inputs()
    p['live_backup']['live_active_orders'] = {'EURUSD': {'status': 'OPEN'}}
    with pytest.raises(RecoveryError):
        validate(p, d, b)


def test_unknown_authority_fields_block():
    p, d, b = inputs()
    p['live_backup']['accepted_execution'] = {'valid': True}
    with pytest.raises(RecoveryError):
        validate(p, d, b)
