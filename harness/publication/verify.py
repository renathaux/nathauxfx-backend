"""Validate existing OCI bytes only. No image execution or build capability."""
import hashlib,json,sys,tarfile
from pathlib import Path

PINS={
 'production':('62ccad99d85a63cc7e27e4bc2b7a0137150a89b44d5c8a99933d15c4e47bcfbc','000b35a4c1783f26b7870ae9ca4b831ec865a341c030678462e421dbab74ec65','dcf09cda4166b87cf1706acebc2d3985988fb9eb3745f5b95989523eff5b6168'),
 'maintenance':('b23141e72bcceae169956ba694c242ea6eeae15e6bc8110b9b04d3e2119db41c','8a1b5faeb57e8541582b25446b410bd944eb1dac384033fecd54d9120be05a82','0f983d00b59b9e6c304b3f9dedcabbd1ebb4b8573f9e880a5ec7181044c20379')}
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
  result={'certification_run':38584096907,'artifact_id':11907941907,'artifact_name':'tested-oci-1-38584096907',
   'registry':'ghcr.io/renathaux/nathauxfx-backend','images':result,'registry_verification':'PASS',
   'archive_hashes':{kind:pin[0] for kind,pin in PINS.items()},'rebuild':False,'deployment':False,'production_access':False}
 Path(out).write_text(json.dumps(result,sort_keys=True,indent=2));print(json.dumps(result,sort_keys=True))
