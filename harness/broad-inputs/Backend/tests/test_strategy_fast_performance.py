"""Optimizations must preserve exact fact/evaluator outputs, not approximate metrics."""
import copy
import pandas as pd
from services.strategy_engine import market_facts as facts, evaluator
from services.strategy_engine.types import EvaluationState
from test_strategy_engine_evaluator import definition, FakeTimeline, candle, event, T0

def test_indexed_swing_directions_equal_reference_at_every_timestamp():
    stamps=pd.date_range('2025-01-01',periods=80,freq='5min',tz='UTC')
    swings=[dict(type='HIGH' if i%2 else 'LOW', confirmed_timestamp=stamps[i].isoformat(),price=float((i*7)%13)) for i in range(0,80,3)]
    index=facts.SwingDirectionIndex(swings)
    for stamp in stamps:
        assert index.at(stamp)==facts._swing_structure(swings,stamp)
    assert index.at(stamps[0]-pd.Timedelta(minutes=1)) is None

def test_normalized_evaluator_matches_public_boundary_and_never_mutates_definition():
    raw=definition();value=evaluator.normalize_definition(raw);before=copy.deepcopy(value)
    timeline=FakeTimeline(candles={T0:candle(T0,1.099,1.102,1.098,1.101)},events={T0:event()})
    kwargs=dict(symbol='EURUSD',account_balance=10000)
    expected=evaluator.evaluate_strategy(raw,timeline,T0,EvaluationState(),**kwargs)
    assert evaluator.evaluate_strategy_normalized(value,timeline,T0,EvaluationState(),**kwargs)==expected
    assert value==before

def test_compact_price_index_matches_historical_scan_without_lookahead():
    from services.strategy_engine.market_facts_compact import ConfirmedPriceIndex
    times=pd.date_range('2025-01-01',periods=100,freq='5min',tz='UTC')
    swings=[dict(type='HIGH' if i%2 else 'LOW',confirmed_timestamp=times[i].isoformat(),price=float(i*17%37)) for i in range(100)]
    for kind in ['HIGH','LOW']:
        index=ConfirmedPriceIndex(swings,kind)
        for t in times:
            for price in range(0,40,3):
                available=[s['price'] for s in swings if s['type']==kind and pd.Timestamp(s['confirmed_timestamp'])<=t and (s['price']>price if kind=='HIGH' else s['price']<price)]
                expected=(min(available) if kind=='HIGH' else max(available)) if available else None
                assert index.find(t.value,price,kind=='HIGH')==expected

def test_compact_and_standard_facts_match_at_every_timestamp():
    import numpy as np
    times=pd.date_range('2025-01-01',periods=700,freq='5min',tz='UTC')
    prices=2000+np.sin(np.arange(700)/9)*20
    frame=pd.DataFrame(dict(Open=prices,High=prices+2,Low=prices-2,Close=prices+1,Volume=0),index=times)
    from services.strategy_simulator_static_data import _aggregate
    bundle={'5m':frame,**{tf:_aggregate(frame,tf,times[-1]+pd.Timedelta(minutes=5)) for tf in ['15m','1h','4h']}}
    a=facts.build_market_facts(bundle,'XAUUSD','5m','1h','15m')
    b=facts.build_market_facts(bundle,'XAUUSD','5m','1h','15m',compact=True)
    assert a.timestamps()==b.timestamps()
    for t in times:
        assert a.candle(t)==b.candle(t)
        assert a.structure_candle(t)==b.structure_candle(t)
        assert a.structure_event(t)==b.structure_event(t)
        assert a.trend(t)==b.trend(t)
        assert a.previous_timestamp(t)==b.previous_timestamp(t)
        assert a.next_timestamp(t)==b.next_timestamp(t)
        for side in ['BUY','SELL']:
            assert a.opposite_swing(t,side,2000)==b.opposite_swing(t,side,2000)
