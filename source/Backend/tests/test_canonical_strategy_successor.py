"""Narrow explicit legacy-default migration; no relaxation of saved identity."""
import copy
import importlib.util
from types import SimpleNamespace

import pytest

from live_integrity.binding import fingerprint
from live_integrity.schema import normalize_definition
from test_strategy_engine_evaluator import definition, FakeTimeline, T0, candle, event


def planner():
    assert importlib.util.find_spec('live_integrity.canonical_successor'), 'Missing explicit successor planner'
    from live_integrity.canonical_successor import plan
    return plan


def pair():
    value = definition()
    value['symbols'] = ['XAUUSD']
    value['tp1'] = dict(enabled=True, target_r=.75, close_percent=80,
                        protection_r=.2, protection_mode='FIXED')
    canonical = normalize_definition(value)
    legacy = copy.deepcopy(canonical)
    del legacy['risk']['max_concurrent_positions']
    del legacy['risk']['max_combined_open_risk_percent']
    del legacy['tp1']['protection_trigger_method']
    return legacy, canonical


def test_only_explicit_historical_defaults_with_both_independent_pins():
    raw, expected = pair()
    before = copy.deepcopy(raw)
    actual = planner()(raw, source_sha256=fingerprint(raw), successor_sha256=fingerprint(expected))
    assert actual == expected
    assert actual['risk']['max_concurrent_positions'] == 1
    assert actual['risk']['max_combined_open_risk_percent'] is None
    assert actual['tp1']['protection_trigger_method'] == 'CANDLE_CLOSE'
    assert raw == before


@pytest.mark.parametrize('fault', ['source_pin','successor_pin','extra_missing','present_value','unknown_field'])
def test_no_generic_normalization_or_silent_behavior_change(fault):
    raw, expected = pair()
    if fault == 'extra_missing': del raw['fundamentals']
    if fault == 'present_value': raw['risk']['max_concurrent_positions'] = 2
    if fault == 'unknown_field': raw['unexpected'] = True
    with pytest.raises(ValueError):
        planner()(raw, source_sha256='0'*64 if fault == 'source_pin' else fingerprint(raw),
                  successor_sha256='0'*64 if fault == 'successor_pin' else fingerprint(expected))


@pytest.mark.parametrize('direction', ['BUY','SELL'])
@pytest.mark.parametrize('balance', [1000, 10000, 12500.75])
@pytest.mark.parametrize('protection', ['FIXED','TP2_STEPS'])
def test_gold_entry_risk_sl_tp_tp1_and_protection_equivalence(direction,balance,protection):
    from live_integrity.evaluator import evaluate_strategy
    from live_integrity.types import EvaluationState
    from services.strategy_studio_position_manager import _levels
    raw, expected = pair()
    if protection == 'TP2_STEPS':
        expected['tp1'].update(protection_mode='TP2_STEPS',protection_r=None,target_basis='TP2_DISTANCE',target_r=.2,
            protection_steps=[{'trigger_percent':25.0,'secure_percent':10.0},
                              {'trigger_percent':50.0,'secure_percent':30.0}])
        expected = normalize_definition(expected)
        raw = copy.deepcopy(expected)
        del raw['risk']['max_concurrent_positions']
        del raw['risk']['max_combined_open_risk_percent']
        del raw['tp1']['protection_trigger_method']
    canonical = planner()(raw, source_sha256=fingerprint(raw), successor_sha256=fingerprint(expected))
    buy = direction == 'BUY'
    timeline = FakeTimeline(candles={T0:candle(T0,2000,2012,1988,2010 if buy else 1990)},
        events={T0:event(direction=direction,broken=2000,invalidation=1980 if buy else 2020,trigger_close=2010 if buy else 1990)})
    results = [evaluate_strategy(d,copy.deepcopy(timeline),T0,EvaluationState(),
        symbol='XAUUSD',account_balance=balance,exact_prices=True) for d in (raw,canonical)]
    assert results[0] == results[1]
    assert results[0].signal == direction
    assert results[0].risk_budget['dollars'] == pytest.approx(balance/100)
    levels = [_levels(SimpleNamespace(definition_snapshot=d,direction=direction,
        execution_snapshot={'entry':2010 if buy else 1990,'initial_sl':1980 if buy else 2020,'tp2':2070 if buy else 1930}),{}) for d in (raw,canonical)]
    assert levels[0] == levels[1]
    assert levels[0]['protection_trigger_method'] == 'CANDLE_CLOSE'


def test_normal_clone_preserves_original_and_leaves_successor_inactive():
    from datetime import datetime, timezone
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from db import Base
    from services.strategy_studio_models import SavedStrategy,StrategyStudioSelection
    from services.strategy_studio_service import clone_strategy
    raw, expected = pair()
    canonical = planner()(raw, source_sha256=fingerprint(raw),successor_sha256=fingerprint(expected))
    NOW = datetime(2026, 9, 17, 10, 0, tzinfo=timezone.utc)
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    try:
        with sessions() as session:
            session.add(SavedStrategy(strategy_id='historical',owner_id='user:fixture',name='Original',
                schema_version=1,definition_json=raw,created_at=NOW,updated_at=NOW))
            session.flush()
            session.add(StrategyStudioSelection(owner_id='user:fixture',strategy_id='historical',activated_at=NOW,updated_at=NOW))
            session.commit()
        successor = clone_strategy('user:fixture','historical','Explicit canonical successor',sessions)
        assert successor['strategy_id'] != 'historical'
        assert successor['state'] == 'INACTIVE'
        assert successor['definition'] == canonical
        with sessions() as session:
            assert session.get(SavedStrategy,'historical').definition_json == raw
            assert session.get(StrategyStudioSelection,'user:fixture').strategy_id == 'historical'
    finally:
        engine.dispose()
