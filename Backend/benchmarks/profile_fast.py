"""Read-only benchmark against a chosen source tree and canonical static history.
No API/auth/broker imports. Fixture strategy is explicitly NOT a saved Gold strategy.
Run with PYTHONPATH=<reference>/Backend python profile_fast.py --history PATH --output PATH.
"""
import argparse, json, time, resource, hashlib, math, inspect
from collections import defaultdict
from datetime import datetime
from pathlib import Path
import pandas as pd
from pydantic import BaseModel, TypeAdapter
from services import strategy_simulator as sim, strategy_simulator_static_data as data
from services.strategy_engine import market_facts as facts

class Candle(BaseModel):
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0

def fixture(symbol):
    return dict(schema_version=1,symbols=[symbol],trading_timeframe='5m',structure_timeframe='15m',
        trend=dict(timeframe='1h',methods=['EMA_50','SWING_STRUCTURE']),
        structure=dict(trigger='BOS_CHOCH',break_validation=['CLOSE_BEYOND']),
        confirmation=dict(rules=['NEXT_SAME_DIRECTION'],max_setup_age_bars=20),
        entry=dict(method='CONFIRMATION_CLOSE',remember_bos_on_confirmation_failure=True),
        stop_loss=dict(method='LAST_SWING',buffer_pips=0),tp1=dict(enabled=False),
        tp2=dict(method='FIXED_R',value=2),risk=dict(method='PERCENT_BALANCE',value=1))

def main():
    p=argparse.ArgumentParser();p.add_argument('--history',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--start',default='2025-01-01');p.add_argument('--end',default='2025-02-01');p.add_argument('--symbol',default='XAUUSD')
    p.add_argument('--sections',action='store_true',help='Instrument frozen reference EMA/trend/diagnostic sections without editing source files');p.add_argument('--definition',type=Path);p.add_argument('--full',action='store_true');args=p.parse_args()
    args.output.mkdir(parents=True,exist_ok=True)
    definition=json.loads(args.definition.read_text()) if args.definition else fixture(args.symbol)
    (args.output/'definition.json').write_text(json.dumps(definition,indent=2))
    timings=defaultdict(float);counts=defaultdict(int)
    if args.sections:
        # In-memory timing only; fail on unknown source rather than silently
        # measuring another block. No strategy expressions are changed.
        def instrument(module, name, sections):
            source = inspect.getsource(getattr(module, name))
            for label, begin_marker, end_marker in sections:
                assert source.count(begin_marker) == 1 and source.count(end_marker) == 1
                source = source.replace(begin_marker, "    __section_" + label + " = __profile_clock()\n" + begin_marker)
                source = source.replace(end_marker, "    __profile_timings[" + repr(label) + "] += __profile_clock() - __section_" + label + "\n" + end_marker)
            module.__dict__.update(__profile_clock=time.perf_counter, __profile_timings=timings)
            exec(compile(source, '<benchmark section instrumentation>', 'exec'), module.__dict__)
        instrument(facts, 'build_market_facts', [
            ('ema_calculations', '    ema50 = ', '    trend_at_source:'),
            ('trend_fact_generation', '    trend_at_source:', '    return MarketFactsTimeline('),
        ])
        sim.build_market_facts = facts.build_market_facts
        instrument(sim, 'run_simulation', [
            ('diagnostics_generation', '    stage_pass_counts = {', '    output = {'),
        ])
    def wrap(module,name,label=None):
        original=getattr(module,name)
        def measured(*a,**kw):
            tag=label(a,kw) if callable(label) else label or name;t=time.perf_counter()
            try:return original(*a,**kw)
            finally:timings[tag]+=time.perf_counter()-t;counts[tag]+=1
        setattr(module,name,measured)
    for m,n,label in [(data,'_aggregate',lambda a,k:'aggregate_'+a[1]),(data,'_canonical_frame','canonical_frame'),
        (sim,'build_market_facts','build_market_facts'),(facts,'analyze_structure',lambda a,k:'analyze_structure_'+k['timeframe']),
        (facts,'detect_confirmed_swings','detect_confirmed_swings'),(facts,'_swing_structure','swing_structure'),
        (sim,'evaluate_strategy','evaluate_strategy'),(sim,'resolve_virtual_trade','resolve_virtual_trade')]:wrap(m,n,label)
    start=pd.Timestamp(args.start,tz='UTC');end=pd.Timestamp(args.end,tz='UTC');cursor=start
    minutes={'5m':5,'15m':15,'1h':60,'4h':240}
    trend=definition.get('trend',{});methods=trend.get('methods',[])
    trend_bars=max(100 if methods else 0,70 if 'EMA_50' in methods else 0,220 if 'EMA_200' in methods else 0)
    required=max(100*max(minutes[definition['trading_timeframe']],minutes[definition.get('structure_timeframe',definition['trading_timeframe'])]),trend_bars*minutes[trend.get('timeframe') or definition['trading_timeframe']])
    warmup=max(7,math.ceil(required*7/5/1440+2))
    month_cache={};continuation=None;chunks=[];total=time.perf_counter();uploads=0;dataset_bytes=0
    while cursor<end:
        finish=end if args.full else min(end,cursor+pd.Timedelta(days=31));history_start=cursor-pd.Timedelta(days=warmup)
        files=[]
        for month in pd.period_range(history_start.tz_localize(None), (finish-pd.Timedelta(milliseconds=1)).tz_localize(None),freq='M'):
            path=args.history/args.symbol/(str(month)+'.json')
            if not path.exists():continue
            if str(month) not in month_cache:
                t=time.perf_counter();raw=path.read_bytes();timings['static_file_read']+=time.perf_counter()-t;dataset_bytes+=len(raw)
                t=time.perf_counter();month_cache[str(month)]=json.loads(raw)['candles'];timings['static_json_parse']+=time.perf_counter()-t
            files.extend(month_cache[str(month)])
        t=time.perf_counter();lo=history_start.isoformat().replace('+00:00','.000Z');hi=finish.isoformat().replace('+00:00','.000Z')
        rows=sorted({r['timestamp']:r for r in files if lo<=r['timestamp']<hi}.values(),key=lambda r:r['timestamp'])
        timings['merge_filter_sort']+=time.perf_counter()-t
        t=time.perf_counter();encoded=json.dumps(rows);uploads+=len(encoded.encode());parsed=TypeAdapter(list[Candle]).validate_json(encoded);timings['pydantic_candles']+=time.perf_counter()-t
        t=time.perf_counter();bundle=data.build_static_market_bundle(parsed,cursor,finish);timings['build_static_market_bundle']+=time.perf_counter()-t
        t=time.perf_counter();result=sim.run_simulation(definition,bundle,args.symbol,10000,evaluation_start=cursor,evaluation_end=finish,continuation=continuation,finalize_open_trade=finish==end);elapsed=time.perf_counter()-t;timings['run_simulation']+=elapsed
        continuation=result['continuation'];t=time.perf_counter();serialized=json.dumps(result);timings['result_json_serialization']+=time.perf_counter()-t
        (args.output/f'chunk-{len(chunks):02}.json').write_text(serialized)
        chunks.append(dict(start=cursor.isoformat(),end=finish.isoformat(),candles=len(rows),simulation_seconds=elapsed,result_bytes=len(serialized)))
        report=dict(strategy_source=str(args.definition) if args.definition else 'synthetic representative fixture; NOT Gold 831/832',symbol=args.symbol,start=str(start),end=str(end),full=args.full,wall_seconds=time.perf_counter()-total,timings=dict(timings),calls=dict(counts),chunks=chunks,uploaded_candle_json_bytes=uploads,dataset_json_bytes=dataset_bytes,peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,manifest_sha256=hashlib.sha256((args.history/'manifest.json').read_bytes()).hexdigest())
        (args.output/'timing.json').write_text(json.dumps(report,indent=2));print(json.dumps(dict(chunk=len(chunks),simulation_seconds=elapsed,wall_seconds=report['wall_seconds'])),flush=True);cursor=finish
    print(json.dumps(report,indent=2),flush=True)
if __name__=='__main__':main()
