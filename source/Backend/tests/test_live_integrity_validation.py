"""Real evaluator and pure dependencies must not load execution or change inputs."""
import copy
import importlib
import subprocess
import sys

import pytest


def test_pure_import_graph_has_no_services_bootstrap_db_or_broker():
    script = '''
import sys
class Deny:
    def find_spec(self, name, *args):
        if name.split('.')[0] in ('services','api','ctrader_connector','db','models','stream_generations'):
            raise RuntimeError('DISPATCH_CAPABILITY_IMPORT:'+name)
sys.meta_path.insert(0,Deny())
from live_integrity import evaluator, market_facts, metadata, binding, schema
from live_integrity.order_intent import project_order_intent
assert 'ctrader_connector' not in sys.modules
'''
    result = subprocess.run([sys.executable,'-c',script],capture_output=True,text=True)
    assert result.returncode == 0, result.stderr[-1500:]


def test_request_local_real_evaluation_leaves_shared_inputs_unchanged():
    from test_strategy_studio_live_candidate import _definition, _pending_state
    from test_strategy_engine_evaluator import FakeTimeline, candle
    import pandas as pd
    try:
        pure = importlib.import_module('live_integrity.validation')
    except ModuleNotFoundError:
        pytest.fail('Pure evaluation boundary missing')
    stamp = pd.Timestamp('2026-09-17T13:05:00Z')
    definition = _definition()
    prior = _pending_state()
    timeline = FakeTimeline(candles={stamp:candle(stamp,1.0998,1.1002,1.0997,1.1001)})
    before = copy.deepcopy((definition,prior,timeline.__dict__))
    pure.evaluate_current(definition,timeline,[stamp],symbol='EURUSD',balance=10000,prior_state=prior)
    assert (definition,prior,timeline.__dict__) == before


def test_real_market_fact_builder_and_evaluator_do_not_mutate_inputs_or_globals():
    import pandas as pd
    from live_integrity import evaluator,market_facts,market_facts_compact
    from live_integrity.market_data import aggregate_closed
    from live_integrity.validation import evaluate_current
    from test_strategy_engine_evaluator import definition
    closes=[1.10,1.12,1.14,1.11,1.09,1.11,1.15,1.13,1.12,1.17,1.18,1.20]*5
    frame=pd.DataFrame([(c-.005,c+.01,c-.01,c,0) for c in closes],columns=['Open','High','Low','Close','Volume'],index=pd.date_range('2026-09-17',periods=len(closes),freq='5min',tz='UTC'))
    bundle={tf:aggregate_closed(frame,tf,end_exclusive=frame.index[-1]+pd.Timedelta(minutes=5)) for tf in ('5m','15m','1h','4h')}
    def globals_snapshot():
        return copy.deepcopy({m.__name__:{k:v for k,v in vars(m).items() if not k.startswith('__') and isinstance(v,(dict,list,set))} for m in (evaluator,market_facts,market_facts_compact)})
    before=copy.deepcopy(bundle); shared=globals_snapshot(); config=definition(); original=copy.deepcopy(config)
    timeline=market_facts.build_market_facts(bundle,'EURUSD','5m',None)
    evaluate_current(config,timeline,timeline.timestamps(),symbol='EURUSD',balance=10000)
    assert config==original and globals_snapshot()==shared
    assert all(bundle[k].equals(before[k]) and bundle[k].attrs==before[k].attrs for k in bundle)
