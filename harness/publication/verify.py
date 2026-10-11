"""Validate existing OCI bytes only. No image execution or build capability."""
import hashlib,json,sys,tarfile
from pathlib import Path

PINS={
 'production':('8a38e73e357be709ece03e7b7d83942ee181df64dae5fa153ce9ee0774457315','963b64859423a3804b1d0c7a9e5b43cf8e4cc74dc33cdb62abdb6cb6a0d2a848','b0cb1d661210a345efc8a1429b7866f47675d4f8f5f1e469fbeec691097a9a17'),
 'maintenance':('3c40d9c629a929f5c8a3a75b40c5e94544cbdbc488d76e9a68fc2ca11ed59d61','9a32b88bd4911dd41ee89bd2a3378a7095e5ff1e2e2b7a78278858ba620bc6dc','344c94c33d656c6ad44f91087eaaddbc1cf07c77a6453de79f782acbcc7759c6')}
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
  result={'certification_run':38124754867,'artifact_id':11697286441,'artifact_name':'tested-oci-1-38124754867',
   'registry':'ghcr.io/renathaux/nathauxfx-backend','images':result,'registry_verification':'PASS',
   'archive_hashes':{kind:pin[0] for kind,pin in PINS.items()},'rebuild':False,'deployment':False,'production_access':False}
 Path(out).write_text(json.dumps(result,sort_keys=True,indent=2));print(json.dumps(result,sort_keys=True))
