"""Executable controls: neutral migration, true gates, and closed structure sources."""
from copy import deepcopy
import pytest
import pandas as pd
from test_strategy_engine_evaluator import definition, candle, event, FakeTimeline, T0, T1, T2, T3
from services.strategy_engine.evaluator import evaluate_strategy
from services.strategy_engine.types import EvaluationState
from services.strategy_studio_schema import normalize_definition, validation_errors


def evaluate(value, timeline, stamps):
    state = EvaluationState()
    results = []
    for stamp in stamps:
        result = evaluate_strategy(value, timeline, stamp, state, symbol=value['symbols'][0], account_balance=10000)
        results.append(result)
        state = result.next_state
    return results


def gold():
    value = definition()
    value['symbols'] = ['XAUUSD']
    timeline = FakeTimeline(candles={T0:candle(T0, 4290, 4310, 4280, 4300)}, events={T0:event(trigger_close=4300, broken=4295, invalidation=4280)})
    return value, timeline


def test_old_definition_neutral_defaults_and_exact_output():
    value, timeline = gold()
    normalized = normalize_definition(value)
    assert normalized['structure_timeframe'] == '5m'
    assert normalized['confirmation']['max_setup_age_bars'] is None
    assert normalized['stop_loss']['distance_filter']['enabled'] is False
    assert evaluate(value, timeline, [T0]) == evaluate(normalized, timeline, [T0])
    result = evaluate(value, timeline, [T0])[0]
    assert (result.signal, result.entry, result.sl, result.tp2) == ('BUY', 4300, 4280, 4340)
    assert result.risk_budget['dollars'] == 100


@pytest.mark.parametrize('minimum,maximum,reason', [(0.4,0.6,None), (0.5,0.6,'SL_DISTANCE_BELOW_MINIMUM'), (0.1,0.4,'SL_DISTANCE_ABOVE_MAXIMUM')])
def test_percent_gold_gate(minimum, maximum, reason):
    value, timeline = gold()
    value['stop_loss']['distance_filter'] = dict(enabled=True, mode='PERCENT_ENTRY', minimum=minimum, maximum=maximum)
    result = evaluate(value, timeline, [T0])[0]
    assert result.steps['stop_loss']['distance'] == pytest.approx(0.465116279)
    assert result.sl == 4280
    if reason:
        assert result.signal == 'WAIT'
        assert result.steps['stop_loss']['reason'] == reason
        assert result.steps['risk']['state'] == 'NOT_APPLICABLE'
        assert result.next_state.pending_setup is None
    else:
        assert result.signal == 'BUY'


def test_pips_and_disabled_filter():
    value, timeline = gold()
    value['stop_loss']['distance_filter'] = dict(enabled=False, mode='PIPS', minimum=1, maximum=2)
    assert evaluate(value, timeline, [T0])[0].signal == 'BUY'
    value['stop_loss']['distance_filter'].update(enabled=True, minimum=2000, maximum=2000)
    assert evaluate(value, timeline, [T0])[0].signal == 'BUY'


@pytest.mark.parametrize('limit,expected', [(2,'BUY'), (1,'WAIT')])
def test_setup_freshness_original_event(limit, expected):
    value = definition()
    value['entry']['method'] = 'RETEST'
    value['confirmation'].update(rules=['RETEST_LEVEL'], max_setup_age_bars=limit)
    timeline = FakeTimeline(candles={c.timestamp:c for c in [
        candle(T0,1.10,1.102,1.099,1.101), candle(T1,1.101,1.103,1.101,1.102),
        candle(T2,1.101,1.103,1.099,1.102), candle(T3,1.101,1.103,1.099,1.102),
    ]}, events={T0:event()})
    results = evaluate(value, timeline, [T0,T1,T2,T3])
    assert results[2].signal == expected
    if limit == 1:
        assert results[2].steps['confirmation']['reason'] == 'SETUP_EXPIRED'
        assert results[3].signal == 'WAIT'
        assert results[3].next_state.pending_setup is None


def test_new_field_validation():
    value = definition()
    value.update(trading_timeframe='15m', structure_timeframe='5m')
    assert 'structure_timeframe' in validation_errors(value)
    value['structure_timeframe'] = '1h'
    assert not validation_errors(value)
    value['confirmation']['max_setup_age_bars'] = 1.5
    assert validation_errors(value)


def market_bundle():
    import numpy as np
    from services.strategy_simulator_data_source import aggregate_closed
    rng = np.random.default_rng(42)
    closes = 4300 + np.cumsum(rng.normal(0, 3, 300))
    opens = np.r_[4299, closes[:-1]]
    frame = pd.DataFrame(dict(Open=opens, High=np.maximum(opens,closes)+1,
        Low=np.minimum(opens,closes)-1, Close=closes),
        index=pd.date_range('2026-01-01', periods=300, freq='5min', tz='UTC'))
    return {'5m':frame, '15m':aggregate_closed(frame,'15m',end_exclusive=frame.index[-1]+pd.Timedelta(minutes=5))}


def test_real_simulation_filter_changes_trade_set_and_funnel():
    from services.strategy_simulator import run_simulation
    value, _ = gold()
    bundle = market_bundle()
    baseline = run_simulation(value,bundle,'XAUUSD',10000)
    assert baseline['trades']
    neutral = normalize_definition(value)
    assert run_simulation(neutral,bundle,'XAUUSD',10000) == baseline
    value['stop_loss']['distance_filter'] = dict(enabled=True,mode='PERCENT_ENTRY',minimum=0,maximum=100)
    permissive = run_simulation(value,bundle,'XAUUSD',10000)
    assert permissive['trades'] == baseline['trades']
    value['stop_loss']['distance_filter'].update(minimum=0.4,maximum=0.6)
    restrictive = run_simulation(value,bundle,'XAUUSD',10000)
    assert restrictive['trades'] != baseline['trades']
    assert 'SL_DISTANCE_BELOW_MINIMUM' in str(restrictive['diagnostics'])
    assert restrictive['diagnostics'] != baseline['diagnostics']


def test_real_structure_selection_closed_bars_and_later_confirmation():
    from services.strategy_engine.market_facts import build_market_facts
    bundle = market_bundle()
    low = build_market_facts(bundle,'XAUUSD','5m',None,'5m')
    high = build_market_facts(bundle,'XAUUSD','5m',None,'15m')
    value,_ = gold()
    five_only = pd.Timestamp('2026-01-01T03:50Z')
    assert low.structure_event(five_only) is not None
    assert high.structure_event(five_only) is None
    assert evaluate(value,low,[five_only])[0].signal == 'BUY'
    value['structure_timeframe'] = '15m'
    assert evaluate(value,high,[five_only])[0].signal == 'WAIT'
    value['entry']['method'] = 'CONFIRMATION_CLOSE'
    value['confirmation']['rules'] = ['NEXT_SAME_DIRECTION']
    event_time = pd.Timestamp('2026-01-01T15:25Z')
    # The 15:15 higher candle becomes available on the trading bar closing 15:30.
    assert high.structure_event(event_time-pd.Timedelta(minutes=10)) is None
    assert high.structure_event(event_time-pd.Timedelta(minutes=5)) is None
    assert high.structure_event(event_time) is not None
    results = evaluate(value,high,[event_time,event_time+pd.Timedelta(minutes=5)])
    assert results[0].signal == 'WAIT'
    assert results[1].signal == 'BUY'
    # Prefix rebuild proves future candles do not manufacture/change this event.
    short = {key:frame.loc[frame.index <= event_time] for key,frame in bundle.items()}
    prefix = build_market_facts(short,'XAUUSD','5m',None,'15m')
    assert prefix.structure_event(event_time) == high.structure_event(event_time)


@pytest.mark.parametrize('restrictive',[False,True])
def test_simulator_and_live_use_same_eligibility(restrictive):
    from services.strategy_engine.market_facts import build_market_facts
    from services.strategy_simulator import run_simulation
    from services.strategy_studio_live_candidate import _evaluate_latest
    value,_ = gold()
    value['structure_timeframe'] = '15m'
    value['stop_loss']['distance_filter'] = dict(enabled=restrictive,mode='PERCENT_ENTRY',minimum=0.4,maximum=0.6)
    bundle = market_bundle()
    stamp = pd.Timestamp('2026-01-01T02:40Z')
    timeline = build_market_facts(bundle,'XAUUSD','5m',None,'15m')
    _,_,live = _evaluate_latest(value,timeline,[stamp],'XAUUSD',10000,EvaluationState())
    sim = run_simulation(value,bundle,'XAUUSD',10000,include_replay=True,evaluation_start=stamp,evaluation_end=stamp+pd.Timedelta(minutes=5))
    assert sim['replay'][0]['signal'] == live.signal
    assert sim['replay'][0]['steps'] == live.steps
    assert (live.signal == 'BUY') is (not restrictive)


def test_remember_bos_freshness_never_resets_and_duplicate_bar_does_not_age():
    value = definition()
    value['entry'].update(method='CONFIRMATION_CLOSE',remember_bos_on_confirmation_failure=True)
    value['confirmation'].update(rules=['NEXT_SAME_DIRECTION'],max_setup_age_bars=2)
    timeline = FakeTimeline(candles={
        T0:candle(T0,1.100,1.102,1.099,1.101),
        T1:candle(T1,1.102,1.103,1.098,1.099),
        T2:candle(T2,1.098,1.100,1.097,1.099),
        T3:candle(T3,1.099,1.103,1.098,1.102),
    },events={T0:event(),T2:event(timestamp=T2)})
    results=evaluate(value,timeline,[T0,T1,T1,T2,T3])
    assert results[2].next_state.pending_setup['age_bars'] == 1
    assert results[3].next_state.pending_setup['event_timestamp'] == T0.isoformat()
    assert results[4].steps['confirmation']['reason'] == 'SETUP_EXPIRED'


@pytest.mark.parametrize('minimum,maximum',[(10,20),(0,10)])
def test_pip_boundaries_are_inclusive_despite_binary_roundoff(minimum,maximum):
    value=definition()
    value['stop_loss']=dict(method='FIXED_DISTANCE',fixed_distance=10,buffer_pips=None,
        distance_filter=dict(enabled=True,mode='PIPS',minimum=minimum,maximum=maximum))
    timeline=FakeTimeline(candles={T0:candle(T0,1.10,1.102,1.099,1.101)},events={T0:event()})
    assert evaluate(value,timeline,[T0])[0].signal == 'BUY'


def test_percent_exact_boundary_and_chunked_freshness():
    value,timeline=gold()
    distance=20/4300*100
    value['stop_loss']['distance_filter']=dict(enabled=True,mode='PERCENT_ENTRY',minimum=distance,maximum=distance)
    assert evaluate(value,timeline,[T0])[0].signal == 'BUY'
    value=definition()
    value['entry']['method']='RETEST'
    value['confirmation'].update(rules=['RETEST_LEVEL'],max_setup_age_bars=2)
    timeline=FakeTimeline(candles={T0:candle(T0,1.100,1.102,1.099,1.101),T1:candle(T1,1.101,1.103,1.101,1.102)},events={T0:event()})
    prior=evaluate(value,timeline,[T0,T1])[-1].next_state
    # No warmup/event bar in the next chunk. The watermark continues the count.
    chunk=FakeTimeline(candles={T2:candle(T2,1.101,1.103,1.101,1.102),T3:candle(T3,1.101,1.103,1.099,1.102)})
    r=evaluate_strategy(value,chunk,T2,prior,symbol='EURUSD',account_balance=10000)
    assert r.next_state.pending_setup['age_bars'] == 2
    r=evaluate_strategy(value,chunk,T3,r.next_state,symbol='EURUSD',account_balance=10000)
    assert r.steps['confirmation']['reason'] == 'SETUP_EXPIRED'
