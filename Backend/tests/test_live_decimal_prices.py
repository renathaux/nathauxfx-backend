"""Catch float arithmetic before freezing, without rounding invalid prices."""
from decimal import Decimal
from types import SimpleNamespace

import pytest

from services.strategy_engine.evaluator import evaluate_strategy
from services.strategy_engine.types import EvaluationState
from services.strategy_studio_schema import normalize_definition
from services.strategy_live_binding import freeze_plan
from services.strategy_studio_execution_adapter import normalize_studio_trade_levels
from test_strategy_engine_evaluator import FakeTimeline, candle, event, definition, T0
from test_broker_execution_metadata import fixture_metadata


def evaluated(entry=1.1001, stop=1.0951, *, changes=None):
    config = definition()
    if changes:
        for key, values in changes.items(): config[key].update(values)
    config = normalize_definition(config)
    timeline = FakeTimeline(candles={T0:candle(T0,entry-.001,entry+.001,entry-.002,entry)},
        events={T0:event(invalidation=stop,trigger_close=entry)})
    result = evaluate_strategy(config,timeline,T0,EvaluationState(),symbol='EURUSD',
        account_balance=10000,exact_prices=True)
    return config,result


@pytest.mark.parametrize('entry,stop,expected',[(1.1001,1.0951,'1.1101'),(1.1537,1.1487,'1.1637')])
def test_arithmetic_targets_are_exact_before_broker_validation(entry,stop,expected):
    config,result = evaluated(entry,stop)
    assert result.signal == 'BUY'
    assert Decimal(str(result.tp2)) == Decimal(expected)
    metadata = fixture_metadata('EURUSD',account_id='7',environment='demo')
    frozen = freeze_plan(config,result,symbol='EURUSD',account_balance=10000,
        account_scope='CTRADER:DEMO:7',broker_metadata=metadata)
    assert Decimal(str(frozen['tp2'])) == Decimal(expected)
    assert normalize_studio_trade_levels('EURUSD','BUY',result.entry,result.sl,result.tp1,result.tp2,
        tp1_enabled=False,broker_metadata=metadata)['ok']


def test_fixed_distance_stop_and_tp1_use_decimal_operands():
    _,r = evaluated(changes={'stop_loss':{'method':'FIXED_DISTANCE','fixed_distance':50,'buffer_pips':None},
        'tp1':{'enabled':True,'target_r':1,'close_percent':50,'protection_r':.5}})
    assert [str(r.entry),str(r.sl),str(r.tp1),str(r.tp2)] == ['1.1001','1.0951','1.1051','1.1101']


@pytest.mark.parametrize('changes',[
    {'tp2':{'value':2.0001}},
    {'stop_loss':{'method':'FIXED_DISTANCE','fixed_distance':50.001,'buffer_pips':None}},
    {'tp1':{'enabled':True,'target_r':.3333,'close_percent':50,'protection_r':.5}},
])
def test_genuinely_off_grid_arithmetic_is_not_rounded(changes):
    config,r = evaluated(changes=changes)
    with pytest.raises(ValueError,match='BROKER_PRICE_PRECISION_INVALID'):
        freeze_plan(config,r,symbol='EURUSD',account_balance=10000,account_scope='CTRADER:DEMO:7',
            broker_metadata=fixture_metadata('EURUSD',account_id='7',environment='demo'))


def test_noisy_input_without_arithmetic_provenance_is_not_repaired():
    with pytest.raises(ValueError,match='EXECUTION_PRICE_DECIMAL_LOSS|BROKER_PRICE_PRECISION_INVALID'):
        config,r = evaluated(entry=1.1101000000000003)
        freeze_plan(config,r,symbol='EURUSD',account_balance=10000,account_scope='CTRADER:DEMO:7',
            broker_metadata=fixture_metadata('EURUSD',account_id='7',environment='demo'))


@pytest.mark.parametrize('mode',['FIXED','TP2_STEPS'])
def test_protected_prices_are_frozen_exactly_and_reused_by_management(mode):
    config,r = evaluated(changes={'tp1':{'enabled':True,'target_r':1,'close_percent':50,
        'protection_r':.5 if mode=='FIXED' else None,'protection_mode':mode,
        'target_basis':'SL_DISTANCE' if mode=='FIXED' else 'TP2_DISTANCE',
        'protection_steps':[] if mode=='FIXED' else [{'trigger_percent':50,'secure_percent':25}]}})
    frozen = freeze_plan(config,r,symbol='EURUSD',account_balance=10000,account_scope='CTRADER:DEMO:7',
        broker_metadata=fixture_metadata('EURUSD',account_id='7',environment='demo'))
    from services.strategy_studio_position_manager import _levels
    row = SimpleNamespace(definition_snapshot=config,direction='BUY',entry_binding={'frozen_plan':frozen},
        execution_snapshot={'entry':r.entry,'initial_sl':r.sl,'tp2':r.tp2})
    levels = _levels(row,{})
    protected = levels['protected'] if mode=='FIXED' else levels['step_levels'][0]['protected']
    assert str(protected) == '1.1026'
    assert frozen['price_arithmetic_version'] == 'exact-decimal-v1'


def test_off_grid_protected_stop_blocks_before_entry():
    config,r = evaluated(changes={'tp1':{'enabled':True,'target_r':1,'close_percent':50,'protection_r':.3333}})
    with pytest.raises(ValueError,match='BROKER_PRICE_PRECISION_INVALID'):
        freeze_plan(config,r,symbol='EURUSD',account_balance=10000,account_scope='CTRADER:DEMO:7',
            broker_metadata=fixture_metadata('EURUSD',account_id='7',environment='demo'))


@pytest.mark.parametrize('method',['BOS_CHOCH_CLOSE','CONFIRMATION_CLOSE'])
def test_entry_source_decimal_is_not_lossily_coerced_to_float(method):
    config = definition()
    config['entry']['method'] = method
    if method == 'CONFIRMATION_CLOSE':
        config['confirmation']['rules'] = ['NEXT_SAME_DIRECTION']
    raw = Decimal('1.1001000000000000001')
    bar = SimpleNamespace(open=1.099,high=1.101,low=1.098,close=raw,body_percent=70)
    timeline = FakeTimeline(candles={T0:bar},events={T0:event(trigger_close=raw)})
    with pytest.raises(ValueError,match='EXECUTION_PRICE_DECIMAL_LOSS'):
        evaluate_strategy(config,timeline,T0,EvaluationState(),symbol='EURUSD',account_balance=10000,exact_prices=True)


@pytest.mark.parametrize('side,entry,stop,target',[
    ('BUY',1.1001,1.0951,1.1101),('SELL',1.1009,1.1059,1.0909),
])
def test_decimal_arithmetic_preserves_real_opposite_swing_selection(side,entry,stop,target):
    from services.strategy_engine.market_facts import MarketFactsTimeline
    from services.strategy_engine.evaluator import _target_prices
    kind = 'HIGH' if side == 'BUY' else 'LOW'
    timeline = MarketFactsTimeline(candles={},events={},trends={},timestamps=[T0],
        trading_swings=[{'type':kind,'price':v,'confirmed_timestamp':T0} for v in (entry,target)])
    config = {'tp2':{'method':'OPPOSITE_SWING'},
              'tp1':{'enabled':True,'target_basis':'TP2_DISTANCE','target_r':.5}}
    tp1,tp2 = _target_prices(config,timeline,T0,entry,stop,side,.0001,exact_prices=True)
    assert Decimal(str(tp2)) == Decimal(str(target))
    assert Decimal(str(tp1)) == Decimal('1.1051' if side=='BUY' else '1.0959')
