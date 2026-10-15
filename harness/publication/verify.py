"""Validate existing OCI bytes only. No image execution or build capability."""
import hashlib,json,sys,tarfile
from pathlib import Path

PINS={
 'production':('8ae072036667dd8a515d13009991ef23f91d0d4f04f618f2909b55d68d315088','7214c2136df7304daad71a945c8b5b684effcd7507a693c463aaefb5b2a8ddfc','bef6ff3f51ddf76fc960532b1365e9a4dfefe22d7ea8a249dc34fbe96af9d4b0'),
 'maintenance':('8bc08529d26d7f0dc8d09ba75ec559b2897165eacc1845811493dbaf00710d26','34b1bdb5d3bd0cc6a226ce1df2a32013d299dd15d51480f56e86e010cbaad981','2abaebc2ecbef07c612d61639c9a2493d04c280196d73616f37c85ae89c5f578')}
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
  result={'certification_run':38756405521,'artifact_id':11987665377,'artifact_name':'tested-oci-1-38756405521',
   'registry':'ghcr.io/renathaux/nathauxfx-backend','images':result,'registry_verification':'PASS',
   'archive_hashes':{kind:pin[0] for kind,pin in PINS.items()},'rebuild':False,'deployment':False,'production_access':False}
 Path(out).write_text(json.dumps(result,sort_keys=True,indent=2));print(json.dumps(result,sort_keys=True))
