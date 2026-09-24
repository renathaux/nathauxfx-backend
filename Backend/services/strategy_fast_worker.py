"""Broker-free subprocess entry point. One chronological FAST job with legacy fact reset semantics."""
import gc
import math
import sys
import time
import resource
from pathlib import Path
import pandas as pd
from services.strategy_fast_jobs import read_json, write_json
from services.strategy_fast_history import load_fast_history
from services.strategy_fast_cache import FactsCache, facts_key
from services.strategy_simulator_static_data import _aggregate
from services.strategy_fast_window_facts import build_window_facts
from services.strategy_fast_results import aggregate_results
from services.strategy_simulator import run_simulation
from services.strategy_studio_schema import normalize_definition


def warmup_days(value):
    minutes={'5m':5,'15m':15,'1h':60,'4h':240};trend=value['trend'];methods=trend['methods']
    trend_bars=max(100 if methods else 0,70 if 'EMA_50' in methods else 0,220 if 'EMA_200' in methods else 0)
    required=max(100*max(minutes[value['trading_timeframe']],minutes[value['structure_timeframe']]),trend_bars*minutes[trend['timeframe'] or value['trading_timeframe']])
    return max(7,math.ceil(required*7/5/1440+2))


def execute(payload, *, progress=lambda stage,percent:None, cancelled=lambda:False, history_dir=None, cache_dir=None):
    begin=time.perf_counter();timings={};memory_stages={}
    definition=normalize_definition(payload['strategy_definition']);symbol=payload['symbol']
    def stage(label,percent):
        if cancelled():raise InterruptedError('Backtest cancelled')
        memory_stages[label]=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*(1024 if sys.platform!='darwin' else 1)
        progress(label,percent)
    stage('Loading history',5);t=time.perf_counter()
    history=load_fast_history(symbol,payload['start'],payload['end'],warmup_days(definition),history_dir=history_dir)
    timings['history_seconds']=time.perf_counter()-t
    frame=history.frame_5m
    key=facts_key(history.history_hash,symbol,payload['start'],payload['end'],definition['trading_timeframe'],definition['structure_timeframe'],definition['trend']['timeframe'])
    cache=FactsCache(cache_dir) if cache_dir else None
    t=time.perf_counter();timeline=cache.get(key) if cache else None;cache_hit=timeline is not None
    timings['cache_read_seconds']=time.perf_counter()-t
    if timeline is None:
        stage('Building timeframes',10);t=time.perf_counter()
        bundle={'5m':frame}
        for tf in ['15m','1h','4h']:bundle[tf]=_aggregate(frame,tf,payload['end'])
        timings['timeframe_seconds']=time.perf_counter()-t
        stage('Building structure and trend facts',25);t=time.perf_counter()
        windows = []
        cursor, end = pd.Timestamp(payload['start']), pd.Timestamp(payload['end'])
        while cursor < end:
            finish = min(cursor + pd.Timedelta(days=31), end)
            windows.append((cursor, finish, cursor - pd.Timedelta(days=warmup_days(definition))))
            cursor = finish
        timeline=build_window_facts(bundle, symbol, definition['trading_timeframe'], definition['trend']['timeframe'], definition['structure_timeframe'], windows, progress=lambda fraction: stage('Building structure and trend facts', 25 + 10 * fraction))
        timings['market_facts_seconds']=time.perf_counter()-t
        stage('Caching immutable market facts',35)
        if cache:cache.put(key,timeline)
        del bundle
    metadata=dict(history_version=history.history_hash,history_revision=history.revision,history_source=history.source,history_rows=len(frame),dataset_memory_bytes=int(frame.memory_usage(deep=True).sum()),market_facts_cache_hit=cache_hit)
    del frame,history;gc.collect()
    stage('Running backtest',40);t=time.perf_counter();last_update=0
    def on_progress(done,total):
        nonlocal last_update
        now=time.monotonic()
        if now-last_update>=.5:
            stage('Running backtest',40+55*done/max(total,1));last_update=now
    results = []
    continuation = None
    for index, (start, end, facts) in enumerate(timeline):
        def window_progress(done, total):
            on_progress(index + done / max(total, 1), len(timeline))
        result = run_simulation(
            definition, {}, symbol, payload['starting_balance'],
            risk_override=payload.get('risk_override'), include_replay=False,
            evaluation_start=start, evaluation_end=end, timeline=facts,
            progress=window_progress, is_cancelled=cancelled,
            continuation=continuation, finalize_open_trade=index == len(timeline) - 1,
        )
        continuation = result['continuation']
        results.append(result)
    timings['evaluation_and_resolution_seconds']=time.perf_counter()-t
    stage('Finalizing results',98)
    result = aggregate_results(results, payload['starting_balance'])

    result.update(ok=True,strategy_id=payload['strategy_id'],strategy_name=payload.get('strategy_name'),symbol=symbol,mode='FAST',history_source='STATIC_REPLAY_JSON',account_scope=payload['account_scope'],starting_balance=payload['starting_balance'],neon_candle_reads=False,assumptions=dict(closed_candles_only=True,spread=False,commission=False,slippage=False,ambiguous_intrabar_excluded=True,live_trading_enabled=False))
    result['performance']={**metadata,**timings,'stage_peak_rss_bytes':memory_stages,'total_seconds':time.perf_counter()-begin,'peak_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*(1024 if sys.platform!='darwin' else 1)}
    return result


def main(directory):
    directory=Path(directory);payload=read_json(directory/'input.json')
    def cancelled():return (directory/'cancel').exists() or read_json(directory/'state.json')['status']!='RUNNING'
    def progress(stage,percent):write_json(directory/'progress.json',dict(current_stage=stage,progress=round(percent,1)))
    try:
        result=execute(payload,progress=progress,cancelled=cancelled,cache_dir=directory.parent/'facts-cache')
        if cancelled():raise InterruptedError('Backtest cancelled')
        write_json(directory/'result.json',result)
    except Exception as exc:write_json(directory/'error.json',{'error':str(exc) or type(exc).__name__})

if __name__=='__main__':main(sys.argv[1])
