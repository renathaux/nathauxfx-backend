"""Memory-first must release each raw window before loading the next."""
import weakref
from types import SimpleNamespace
import pandas as pd
from services import strategy_fast_worker as worker


def test_worker_loads_bounded_windows_and_releases_previous_frames(monkeypatch):
    calls, refs = [], []
    definition = {'trading_timeframe':'5m', 'structure_timeframe':'5m', 'trend':{'timeframe':None,'methods':[]}}
    monkeypatch.setattr(worker, 'normalize_definition', lambda value: definition)
    def load(symbol, start, end, warmup, **kwargs):
        assert all(ref() is None for ref in refs)
        assert pd.Timestamp(end)-pd.Timestamp(start) <= pd.Timedelta(days=31)
        calls.append((start,end,warmup))
        frame = pd.DataFrame({'Close':[1.]}, index=pd.DatetimeIndex([start]))
        refs.append(weakref.ref(frame))
        return SimpleNamespace(frame_5m=frame, history_hash='hash', revision='rev', source='fixture')
    monkeypatch.setattr(worker, 'load_fast_history', load)
    monkeypatch.setattr(worker, 'build_fast_market_bundle', lambda frame,required,end: {'5m':frame})
    def facts(bundle,symbol,trading,trend,structure,windows):
        for start,end,_ in windows: yield start,end,object()
    monkeypatch.setattr(worker, 'iter_window_facts', facts)
    continuations=[]
    def run(*args, continuation, finalize_open_trade, **kwargs):
        continuations.append((continuation,finalize_open_trade))
        return {'continuation':len(continuations)}
    monkeypatch.setattr(worker, 'run_simulation', run)
    monkeypatch.setattr(worker, 'aggregate_results', lambda results,balance: {})
    result=worker.execute(dict(strategy_definition={},symbol='XAUUSD',start='2024-12-15T00:00:00Z',end='2025-03-01T00:00:00Z',starting_balance=10000,strategy_id='test',account_scope='test'))
    assert len(calls)==3
    assert continuations==[(None,False),(1,False),(2,True)]
    assert all(ref() is None for ref in refs)


def test_final_weekend_only_window_matches_global_history_oracle(tmp_path):
    """A 31-day chunk boundary can leave only closed-market dates in the tail."""
    import json
    import numpy as np
    from benchmarks.profile_fast import fixture
    from services.strategy_fast_history import load_fast_history
    from services.strategy_fast_results import aggregate_results
    start = pd.Timestamp('2025-01-01T00:00:00Z')
    boundary = start + pd.Timedelta(days=31)  # Saturday, February 1.
    end = pd.Timestamp('2025-02-02T00:00:00Z')
    times = pd.date_range(start-pd.Timedelta(days=7), end+pd.Timedelta(days=2), freq='5min', inclusive='left')
    times = times[times.dayofweek < 5]
    prices = 2000 + np.random.default_rng(831).normal(0, 1, len(times)).cumsum()
    months = ['2024-12', '2025-01', '2025-02']
    symbol_dir = tmp_path/'XAUUSD'
    symbol_dir.mkdir()
    available = {'months': months, '2024-12': {'first_timestamp': times[0].isoformat()}}
    (tmp_path/'manifest.json').write_text(json.dumps({'version':1, 'base_timeframe':'5m', 'symbols':{'XAUUSD':available}}))
    for month in months:
        candles = [dict(timestamp=t.isoformat(),open=float(p),high=float(p+2),low=float(p-2),close=float(p+1),volume=1)
                   for t,p in zip(times,prices) if t.strftime('%Y-%m') == month]
        (symbol_dir/f'{month}.json').write_text(json.dumps({'symbol':'XAUUSD','timeframe':'5m','candles':candles}))
    definition = fixture('XAUUSD')
    definition.update(structure_timeframe='5m', trend={'timeframe':None,'methods':[]})
    definition = worker.normalize_definition(definition)
    warmup = worker.warmup_days(definition)
    history = load_fast_history('XAUUSD',start,end,warmup,history_dir=tmp_path)
    bundle = worker.build_fast_market_bundle(history.frame_5m,['5m'],end)
    windows = [(start,boundary,start-pd.Timedelta(days=warmup)),
               (boundary,end,boundary-pd.Timedelta(days=warmup))]
    chunks = []
    continuation = None
    for index,(left,right,facts) in enumerate(worker.iter_window_facts(bundle,'XAUUSD','5m',None,'5m',windows)):
        result = worker.run_simulation(definition,bundle,'XAUUSD',10000,evaluation_start=left,evaluation_end=right,
                                      continuation=continuation,finalize_open_trade=index==1,timeline=facts)
        chunks.append(result)
        continuation = result['continuation']
    assert chunks[-1]['diagnostics']['candles_analyzed'] == 0
    expected = aggregate_results(chunks,10000)
    actual = worker.execute(dict(strategy_definition=definition,symbol='XAUUSD',start=start.isoformat(),end=end.isoformat(),
                                 starting_balance=10000,strategy_id='weekend-tail',account_scope='offline'),history_dir=tmp_path)
    for key in ('trades','metrics','diagnostics','equity_curve','continuation'):
        assert actual[key] == expected[key], key
