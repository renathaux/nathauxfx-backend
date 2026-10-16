"""Sanitized evidence only; OCI archives are retained separately, never pushed."""
import hashlib,json
from pathlib import Path
import tarfile
import xml.etree.ElementTree as ET
import package

e=Path('evidence')
raw=(e/'maintenance-owner.log').read_text()
xml=ET.fromstring(raw[raw.index('<?xml'):])
totals={k:sum(int(s.get(k,0)) for s in xml.iter('testsuite')) for k in ['tests','failures','errors','skipped']}
assert totals==dict(tests=270,failures=0,errors=0,skipped=0)
auth_raw=(e/'production-auth.log').read_text()
auth_xml=ET.fromstring(auth_raw[auth_raw.index('<?xml'):])
auth_totals={k:sum(int(s.get(k,0)) for s in auth_xml.iter('testsuite')) for k in ['tests','failures','errors','skipped']}
assert auth_totals==dict(tests=73,failures=0,errors=0,skipped=0)
preload_raw=(e/'production-preload-canonical.log').read_text()
preload_xml=ET.fromstring(preload_raw[preload_raw.index('<?xml'):])
preload_totals={k:sum(int(s.get(k,0)) for s in preload_xml.iter('testsuite')) for k in ['tests','failures','errors','skipped']}
assert preload_totals==dict(tests=29,failures=0,errors=0,skipped=0)
result={'source_sha':package.SOURCE,'manifest_sha256':package.MANIFEST,'source_files':391,
 'dependency_lock':package.LOCK,'native_amd64':True,'owner_container_tests':totals,
 'production_auth_tests':auth_totals,'preload_canonical_container_tests':preload_totals,
 'registry_push':False,'production_access':False,'broker_writes':0}
for kind in ('production','maintenance'):
 p=Path('images')/(kind+'.oci.tar')
 info=package.oci_identity(p)
 with tarfile.open(p) as t:
  config=json.load(t.extractfile('blobs/sha256/'+info['config_digest'][7:]))
 info['diff_ids']=config['rootfs']['diff_ids']
 digest=hashlib.sha256()
 with p.open('rb') as f:
  while block:=f.read(1024*1024):digest.update(block)
 info.update(artifact_sha256=digest.hexdigest(),architecture='linux/amd64')
 result[kind]=info
prod=json.loads((e/'production-runtime.json').read_text())
maint=json.loads((e/'maintenance-runtime.json').read_text())
assert prod==maint,'PROTECTED_RUNTIME_PROFILE_CHANGED'
result['build_identity']=prod['build']['build_identity']
result['maintenance_tests']=json.loads((e/'maintenance-tests.json').read_text())['maintenance_tests']
assert result['maintenance_tests']==11
(e/'image-result.json').write_text(json.dumps(result,sort_keys=True,indent=2))
print(json.dumps(result,sort_keys=True))
