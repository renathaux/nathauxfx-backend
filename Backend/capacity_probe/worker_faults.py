import json,os
from pathlib import Path

def install():
    from services import strategy_fast_worker as worker
    path=Path(os.environ['CAPACITY_STATE_ROOT'])/'fault.json'
    fault=json.loads(path.read_text()).get('fault') if path.exists() else None
    if fault=='calculation':
        def fail(*args,**kwargs):raise RuntimeError('injected calculation failure')
        worker.run_simulation=fail
    original=worker.write_json
    def write(path,value):
        if path.name=='result.json':
            original(path.with_name('progress.json'),{'current_stage':'Serializing results','progress':99})
            if fault=='serialization':raise ValueError('injected result serialization failure')
        return original(path,value)
    worker.write_json=write
