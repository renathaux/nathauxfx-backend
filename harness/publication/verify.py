"""Validate existing OCI bytes only. No image execution or build capability."""
import hashlib,json,sys,tarfile
from pathlib import Path

PINS={
 'production':('5de4c90c287e5b26120fb30277114322b52d94d3ca656c35ab2e9669ee23e5fe','1e91557dfcb4d0235cd2cfc6052c095934557284d08a19e1fd45bdec2b051ecc','db984be7b3f84ad024e628074a4da95afe875883bcc69c184cc63e8031fc45f8'),
 'maintenance':('ab568197789e266a71760ed90d4054946e296abe6ff00ac26dc5f10ec800797d','f9a21e46bbfebc77e5745de16c2a69b27c7a40c03d66e342ffba393ffcdac2e4','a99e16ba9c184f7ae88d73608d8d7db5759bd2980a9ff90ad10a29dc1dd628b7')}
def sha(path):
 h=hashlib.sha256()
 with path.open('rb') as f:
  while b:=f.read(1024*1024):h.update(b)
 return h.hexdigest()
def verify(path,kind,original):
 archive_pin,image_pin,config_pin=PINS[kind]
 archive_hash=sha(path)
 if original:assert archive_hash==archive_pin,'ARCHIVE_HASH_MISMATCH'
 with tarfile.open(path) as t:
  def blob(digest):
   raw=t.extractfile('blobs/sha256/'+digest.removeprefix('sha256:')).read()
   assert 'sha256:'+hashlib.sha256(raw).hexdigest()==digest,'BLOB_HASH_MISMATCH'
   return raw
  index=json.load(t.extractfile('index.json'))
  assert len(index['manifests'])==1
  digest=index['manifests'][0]['digest'];assert digest=='sha256:'+image_pin
  manifest=json.loads(blob(digest));assert manifest['config']['digest']=='sha256:'+config_pin
  config=json.loads(blob(manifest['config']['digest']))
  assert (config['os'],config['architecture'])==('linux','amd64')
  for desc in [manifest['config']]+manifest['layers']:assert len(blob(desc['digest']))==desc['size']
  return dict(manifest_digest=digest,config_digest=manifest['config']['digest'],platform='linux/amd64',
   layers=[x['digest'] for x in manifest['layers']],diff_ids=config['rootfs']['diff_ids'])
if __name__=='__main__':
 mode,root,out=sys.argv[1:];assert mode in ('pre','post')
 result={kind:verify(Path(root)/(kind+'.oci.tar'),kind,mode=='pre') for kind in PINS}
 if mode=='post':
  before=json.loads(Path('evidence/pre.json').read_text());assert result==before,'REGISTRY_TRANSFORMATION'
  result={'certification_run':38310701782,'artifact_id':11776309299,'artifact_name':'tested-oci-1-38310701782',
   'registry':'ghcr.io/renathaux/nathauxfx-backend','images':result,'registry_verification':'PASS',
   'archive_hashes':{kind:pin[0] for kind,pin in PINS.items()},'rebuild':False,'deployment':False,'production_access':False}
 Path(out).write_text(json.dumps(result,sort_keys=True,indent=2));print(json.dumps(result,sort_keys=True))
