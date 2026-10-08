import os,json,hashlib,base64,platform,subprocess,sys,stat
from pathlib import Path
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
os.umask(0o077)
r=Path(os.environ['PRIVATE_ROOT']); evidence=Path(os.environ['EVIDENCE_ROOT'])
try:
 assert platform.system()=='Linux' and platform.machine()=='x86_64'
 assert [x.split(':')[0].strip() for x in Path('/proc/net/dev').read_text().splitlines()[2:]]==['lo']
 key=base64.b64decode(os.environ.pop('PROJECTION_KEY')); encrypted=(r/'input.enc').read_bytes()
 bundle=json.loads(AESGCM(key).decrypt(encrypted[:12],encrypted[12:],b'flowsignal-projection-20261008-v1'));del key,encrypted
 assert bundle['source_sha']=='0dbbef69bb293833c299c1fe3e728454b3fa1c45'
 pins=json.loads(Path('/cert/expected-inputs.json').read_bytes())
 assert bundle['code_hashes']==pins['code_hashes']
 for p,h in bundle['code_hashes'].items():assert hashlib.sha256((Path('/app/Backend')/p).read_bytes()).hexdigest()==h
 src=r/'source';dst=r/'destination';ev=r/'inventory-evidence'
 for p in (src,dst,ev):p.mkdir(mode=0o700)
 files=bundle['files'];assert len(files)==7
 assert {n:v['sha256'] for n,v in files.items()}==pins['file_hashes']
 for name,item in files.items():
  assert '/' not in name and name.endswith('.json')
  raw=base64.b64decode(item['bytes']);assert hashlib.sha256(raw).hexdigest()==item['sha256']
  with (src/name).open('xb') as f:f.write(raw)
  (src/name).chmod(0o400)
 src.chmod(0o500)
 scope=bundle['scope'];assert len(scope['family_locations'])==27
 for paths in scope['family_locations'].values():
  for i,p in enumerate(paths):paths[i]=str(src/Path(p).name)
 scopefile=r/'scope.json';scopefile.write_text(json.dumps(scope,sort_keys=True,separators=(',',':'))+'\n');scopefile.chmod(0o400)
 def snapshot():
  out={};fd=os.open(src,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_NOATIME)
  try:names=os.listdir(fd)
  finally:os.close(fd)
  for name in ['.',*sorted(names)]:
   p=src/name;s=p.lstat();row=[getattr(s,'st_'+k) for k in ('mode','uid','gid','ino','nlink','size','mtime_ns','ctime_ns','atime_ns')]
   if name!='.':
    fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW|os.O_NOATIME)
    with os.fdopen(fd,'rb') as f:row.append(hashlib.sha256(f.read()).hexdigest())
   out[name]=row
  return out
 before=snapshot()
 import psycopg2
 c=psycopg2.connect(os.environ['CUTOVER_DATABASE_URL']);c.set_session(readonly=True)
 with c.cursor() as q:
  q.execute('SHOW transaction_read_only');assert q.fetchone()[0]=='on'
  q.execute('SELECT version_num FROM alembic_version');revision=q.fetchone()[0];assert revision=='20261001_0029'
  for table in ('recovery_accounts','recovery_attempts','recovery_mutations','recovery_checkpoint_heads'):
   q.execute('SELECT count(*) FROM '+table);assert q.fetchone()[0]==0
 c.rollback();c.close()
 cmd=[sys.executable,'-B','-m','startup_recovery.cutover','inventory','--source-root',str(src),'--state-root',str(dst),'--scope',str(scopefile),'--evidence-root',str(ev)]
 run=subprocess.run(cmd,cwd='/app/Backend',capture_output=True,text=True)
 result=json.loads(run.stdout)
 assert run.returncode==2 and not run.stderr
 assert not result['blockers'] and not result['remaining_unknowns']
 assert set(result['operational_blockers'])=={'COPY_NOT_VERIFIED','LEGACY_RECONCILIATION_REQUIRED'}
 assert before==snapshot() and not list(dst.iterdir())
 manifest=Path(result['manifest_path']).read_bytes();mh=hashlib.sha256(manifest).hexdigest();assert mh==result['manifest_sha256']
 m=json.loads(manifest);assert m['db_references']==[]
 present=[x for x in m['records'] if x['presence']=='PRESENT'];assert len(present)==7 and all(x['classification']=='COPY_CANDIDATE' for x in present)
 report=dict(status='PASS',platform=platform.platform(),architecture=platform.machine(),uid=os.geteuid(),source_sha=bundle['source_sha'],source_files_verified=len(bundle['code_hashes']),revision=revision,hashes={n:v['sha256'] for n,v in files.items()},scope_families=27,manifest_sha256=mh,independent_manifest_hash=True,blockers=result['blockers'],operational_blockers=result['operational_blockers'],source_unchanged=True,destination_empty=True,copy_executed=False)
 (evidence/'result.json').write_text(json.dumps(report,sort_keys=True,indent=2))
 print('CERTIFIED_INVENTORY_PASS')
except BaseException as exc:
 print('CERTIFICATION_BLOCKED_'+type(exc).__name__);sys.exit(1)
