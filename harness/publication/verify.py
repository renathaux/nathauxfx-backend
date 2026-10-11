"""Validate existing OCI bytes only. No image execution or build capability."""
import hashlib,json,sys,tarfile
from pathlib import Path

PINS={
 'production':('02823ef02468438d4b97b32daa88ffbaad45e8429a8bafd7312514fdc21ed73c','133647a0c31aeaa6347f0a63d21ed0e2fe18f643196cf38f252ad0ea3aaf35d7','3416c586b4a75eb745f5ea4e54b8e5432238d9aa86b530f86657a019a0832de9'),
 'maintenance':('d71865f8c5f022faf93bbcf8077674abaa5984f01ac45397166f7c38b0158361','b7bfdd86b0c7c5254d6c1f10992666f2abe022356bcdc46215861cb1afe3809d','38ab7745c8812c97f3f042316b6c4b5a55ddeb52ad0c4c6b3d54922d565069a5')}
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
  result={'certification_run':38111585216,'artifact_id':11692810031,'artifact_name':'tested-oci-1-38111585216',
   'registry':'ghcr.io/renathaux/nathauxfx-backend','images':result,'registry_verification':'PASS',
   'archive_hashes':{kind:pin[0] for kind,pin in PINS.items()},'rebuild':False,'deployment':False,'production_access':False}
 Path(out).write_text(json.dumps(result,sort_keys=True,indent=2));print(json.dumps(result,sort_keys=True))
