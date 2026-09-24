import json,os,threading,time
from pathlib import Path

class Recorder:
    def __init__(self, root):
        self.path=root/'samples.jsonl';self.events=[];self.label='idle';self.children=[]
        self.start=time.time();self.maximum=0;self.latest={};self.lock=threading.Lock()
        threading.Thread(target=self.loop,daemon=True,name='capacity-sampler').start()
    def event(self, kind, **fields):
        item={'time':time.time(),'kind':kind,**fields}
        with self.lock:self.events.append(item)
        print('CAPACITY_EVENT',json.dumps(item),flush=True)
    def sample(self):
        processes=[]
        for p in Path('/proc').glob('[0-9]*'):
            try:
                status=p.joinpath('status').read_text();rss=next((int(x.split()[1])*1024 for x in status.splitlines() if x.startswith('VmRSS:')),0)
                ppid=int(next(x.split()[1] for x in status.splitlines() if x.startswith('PPid:')))
                state=next(x.split()[1] for x in status.splitlines() if x.startswith('State:'))
                processes.append({'pid':int(p.name),'ppid':ppid,'rss':rss,'state':state})
            except (OSError,ValueError):pass
        descendants={os.getpid()}
        for _ in range(len(processes)):
            found={p['pid'] for p in processes if p['ppid'] in descendants}
            if found.issubset(descendants):break
            descendants.update(found)
        processes=[p for p in processes if p['pid'] in descendants]
        cg=Path('/sys/fs/cgroup');memory={}
        for name in ['memory.current','memory.peak','memory.max','memory.swap.current','memory.events']:
            try:
                raw=(cg/name).read_text().strip()
                memory[name]=int(raw) if raw.isdigit() else raw
            except OSError:memory[name]=None
        # cgroup v1 fallback for older hosts.
        if memory['memory.current'] is None:
            for key,name in [('memory.current','memory.usage_in_bytes'),('memory.peak','memory.max_usage_in_bytes'),('memory.max','memory.limit_in_bytes'),('memory.events','memory.failcnt')]:
                try:memory[key]=int((cg/'memory'/name).read_text())
                except (OSError,ValueError):pass
        stage='idle'
        for state in Path(os.environ['SIMULATOR_FAST_JOB_DIR']).glob('*/state.json'):
            try:
                v=json.loads(state.read_text())
                if v['status']=='RUNNING':
                    progress=state.with_name('progress.json')
                    stage=json.loads(progress.read_text()).get('current_stage','starting') if progress.exists() else 'starting'
            except (OSError,ValueError):pass
        return {'time':time.time(),'label':self.label,'stage':stage,'parent_pid':os.getpid(),'processes':processes,'tree_rss':sum(p['rss'] for p in processes),**memory}
    def loop(self):
        with self.path.open('a',buffering=1) as stream:
            while True:
                try:
                    value=self.sample();self.latest=value
                    self.maximum=max(self.maximum,value.get('memory.current') or 0)
                    stream.write(json.dumps(value)+'\n')
                except Exception as exc:self.event('sampler_error',error=str(exc))
                time.sleep(.05)
