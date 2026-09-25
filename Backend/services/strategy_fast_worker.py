"""Broker-free subprocess entry point. One chronological FAST job with legacy fact reset semantics."""
import gc
import math
import sys
import time
import resource
from pathlib import Path
import pandas as pd
from services.strategy_fast_jobs import read_json, write_json
from services.strategy_fast_history import load_fast_history, HistoryFingerprint
from services.strategy_fast_aggregation import build_fast_market_bundle
from services.strategy_fast_window_facts import iter_window_facts
from services.strategy_fast_results import aggregate_results, DiskResults
from services.strategy_simulator import run_simulation
from services.strategy_studio_schema import normalize_definition


def warmup_days(value):
    minutes={'5m':5,'15m':15,'1h':60,'4h':240};trend=value['trend'];methods=trend['methods']
    trend_bars=max(100 if methods else 0,70 if 'EMA_50' in methods else 0,220 if 'EMA_200' in methods else 0)
    required=max(100*max(minutes[value['trading_timeframe']],minutes[value['structure_timeframe']]),trend_bars*minutes[trend['timeframe'] or value['trading_timeframe']])
    return max(7,math.ceil(required*7/5/1440+2))


def execute(payload, *, progress=lambda stage,percent:None, cancelled=lambda:False, history_dir=None, cache_dir=None, scratch_parent=None):
    with DiskResults(scratch_parent) as results:
        return _execute(payload, progress=progress, cancelled=cancelled, history_dir=history_dir, results=results)


def _execute(payload, *, progress, cancelled, history_dir, results):
    begin=time.perf_counter();timings={};memory_stages={}
    definition=normalize_definition(payload['strategy_definition']);symbol=payload['symbol']
    def stage(label,percent):
        if cancelled():raise InterruptedError('Backtest cancelled')
        memory_stages[label]=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*(1024 if sys.platform!='darwin' else 1)
        progress(label,percent)
    metadata = dict(market_facts_cache_hit=False, market_facts_cache_enabled=False,
                    memory_mode='BOUNDED_WINDOWS', chunk_days=31, warmup_days=warmup_days(definition),
                    history_rows=0, dataset_memory_bytes=0)
    required = [definition['trading_timeframe'], definition['structure_timeframe']]
    if definition['trend']['timeframe']:
        required.append(definition['trend']['timeframe'])
    timings['history_seconds'] = timings['timeframe_seconds'] = 0.0
    windows = []
    cursor, end = pd.Timestamp(payload['start']), pd.Timestamp(payload['end'])
    if end <= cursor or end - cursor > pd.Timedelta(days=5 * 366):
        raise ValueError('SIMULATION_RANGE_INVALID')
    while cursor < end:
        finish = min(cursor + pd.Timedelta(days=31), end)
        windows.append((cursor, finish, cursor - pd.Timedelta(days=warmup_days(definition))))
        cursor = finish
    t=time.perf_counter();last_update=0
    facts_seconds = 0.0
    def on_progress(done,total):
        nonlocal last_update
        now=time.monotonic()
        if now-last_update>=.5:
            stage('Running backtest',5+90*done/max(total,1));last_update=now
    fingerprint = HistoryFingerprint(symbol, payload['end'])
    continuation = None
    evaluation_rows = 0
    for index, window in enumerate(windows):
        start, end, warmup = window
        stage('Loading history window', 5 + 90 * index / len(windows))
        loaded_at = time.perf_counter()
        history = load_fast_history(symbol, start, end, warmup_days(definition), history_dir=history_dir, use_disk_cache=False, fingerprint=fingerprint, require_evaluation=False)
        timings['history_seconds'] += time.perf_counter() - loaded_at
        frame = history.frame_5m
        metadata.update(history_version=history.history_hash, history_revision=history.revision,
                        history_source=history.source)
        evaluation_rows += int(((frame.index >= start) & (frame.index < end)).sum())
        metadata['history_rows'] += len(frame) if index == 0 else int(((frame.index >= start) & (frame.index < end)).sum())
        metadata['dataset_memory_bytes'] = max(metadata['dataset_memory_bytes'], int(frame.memory_usage(deep=True).sum()))
        built_at = time.perf_counter()
        bundle = build_fast_market_bundle(frame, required, end)
        timings['timeframe_seconds'] += time.perf_counter() - built_at
        metadata['required_timeframes'] = list(bundle)
        timeline = iter_window_facts(bundle, symbol, definition['trading_timeframe'], definition['trend']['timeframe'], definition['structure_timeframe'], [window])
        try:
            stage('Building structure and trend facts', 5 + 90 * index / len(windows))
            facts_started = time.perf_counter()
            _, _, facts = next(timeline)
            facts_seconds += time.perf_counter() - facts_started
            def window_progress(done, total):
                on_progress(index + done / max(total, 1), len(windows))
            result = run_simulation(
                definition, {}, symbol, payload['starting_balance'],
                risk_override=payload.get('risk_override'), include_replay=False,
                evaluation_start=start, evaluation_end=end, timeline=facts,
                progress=window_progress, is_cancelled=cancelled,
                continuation=continuation, finalize_open_trade=index == len(windows) - 1,
            )
            continuation = result['continuation']
            results.append(result)
            del facts, result
        finally:
            timeline.close()
            del timeline, bundle, frame, history
        gc.collect()
    timings['evaluation_and_resolution_seconds']=time.perf_counter()-t-facts_seconds-timings['history_seconds']-timings['timeframe_seconds']
    timings['market_facts_seconds']=facts_seconds
    if not evaluation_rows:
        raise ValueError('STATIC_HISTORY_RANGE_UNAVAILABLE')
    metadata['history_version'] = fingerprint.hexdigest()
    stage('Finalizing results',98)
    result = aggregate_results(results, payload['starting_balance'])
    del results, continuation
    gc.collect()
    stage('Working memory released',99)

    result.update(ok=True,strategy_id=payload['strategy_id'],strategy_name=payload.get('strategy_name'),symbol=symbol,mode='FAST',history_source='STATIC_REPLAY_JSON',account_scope=payload['account_scope'],starting_balance=payload['starting_balance'],neon_candle_reads=False,assumptions=dict(closed_candles_only=True,spread=False,commission=False,slippage=False,ambiguous_intrabar_excluded=True,live_trading_enabled=False))
    result['performance']={**metadata,**timings,'stage_peak_rss_bytes':memory_stages,'total_seconds':time.perf_counter()-begin,'peak_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*(1024 if sys.platform!='darwin' else 1)}
    return result


def main(directory):
    directory=Path(directory);payload=read_json(directory/'input.json')
    def cancelled():return (directory/'cancel').exists() or read_json(directory/'state.json')['status']!='RUNNING'
    def progress(stage,percent):write_json(directory/'progress.json',dict(current_stage=stage,progress=round(percent,1)))
    try:
        result=execute(payload,progress=progress,cancelled=cancelled,scratch_parent=directory)
        if cancelled():raise InterruptedError('Backtest cancelled')
        progress('Serializing results', 99)
        write_json(directory/'result.json',result)
    except Exception as exc:write_json(directory/'error.json',{'error':str(exc) or type(exc).__name__})

if __name__=='__main__':main(sys.argv[1])
