"""Independent jobs must agree on full manifests/config/layers/diffIDs and identity."""
import json
from pathlib import Path
import sys
import os

def validate_backend(backend):
    expected={'database-config':7,'owner':71,'focused':281,'safe-recovery':439,'cutover':321,'bounded':787}
    assert set(backend)==set(expected)
    for name,n in expected.items():
        assert backend[name]==dict(tests=n,failures=0,errors=0,skipped=0)
    return backend

def compare(a,b):
    for key in ('source_sha','manifest_sha256','source_files','dependency_lock','build_identity'):
        assert a[key]==b[key], 'RELEASE_IDENTITY_NONDETERMINISM'
    for kind in ('production','maintenance'):
        for key in ('image_digest','manifest_digest','config_digest','layers','diff_ids','architecture'):
            assert a[kind][key]==b[kind][key], kind.upper()+'_DIGEST_NONDETERMINISM_REQUIRES_EXPLANATION'
    return dict(certification='PASS',independent_builds=[a,b],production_reproducible=True,
        maintenance_reproducible=True,image_published=False,production_access=False,
        broker_writes=0,real_order_dispatch=0)

if __name__=='__main__':
    root=Path(sys.argv[1]);paths=sorted(root.glob('image-evidence-*/image-result.json'))
    assert len(paths)==2
    result=compare(*(json.loads(p.read_text()) for p in paths))
    backend=root/'backend'
    result['backend_suites']=validate_backend(json.loads((backend/'backend-results.json').read_text()))
    migration=json.loads((backend/'migration.json').read_text())
    assert migration['before']['revision']=='20261001_0030'
    assert migration['after']['revision']=='20261010_0031' and migration['noop'] is True
    assert all(n==0 for phase in ('before','after') for n in migration[phase]['rows'].values())
    result['backend_migration']=migration
    result['source_after_cleanup']=json.loads((backend/'source-after.json').read_text())
    assert result['source_after_cleanup']['source_files']==369
    assert result['source_after_cleanup']['manifest_sha256']=='693da082978200be0c6c21bfc3a7ca52a6b0e4b018dcdf682dfd8a91bf6d7ce8'
    result['origins']={'application':'c8c82ece5856c2e040b75070bad7888beedd2f60',
        'maintenance':'d223082822bd209858ce4c699e5fa907e654dc29',
        'composite':'63b42795ca26bdecb1be6d7758c8ae757eee90d4'}
    result['base_digest']='sha256:c6f0b5b3a167963de3cc7cd97fe1a5d07105c8d27f47507ab31d648b533b05c7'
    result['oci_artifacts']=['tested-oci-'+str(n)+'-'+os.environ['GITHUB_RUN_ID'] for n in (1,2)]
    result['protected_paths']=[]
    for p in paths:
        protected=json.loads((p.parent/'maintenance-tests.json').read_text())
        assert protected['protected_paths']=='IDENTICAL' and protected['maintenance_tests']==11
        result['protected_paths'].append(protected)
    (root/'certification.json').write_text(json.dumps(result,sort_keys=True,indent=2))
    print(json.dumps(result,sort_keys=True))
