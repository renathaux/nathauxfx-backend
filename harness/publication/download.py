"""Download one pinned Actions artifact; authenticate only via GH_TOKEN."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

ZIP_SHA='12da2c5e31b18cacd2828c47c43eea8981919d07969aad70288063aeaaf90ea8'
API='repos/renathaux/nathauxfx-backend/actions/artifacts/11717090533'

def validate_metadata(value):
    assert value['id']==11717090533
    assert value['name']=='tested-oci-1-38176383880'
    assert value['digest']=='sha256:'+ZIP_SHA
    assert value['expired'] is False
    assert value['workflow_run']['id']==38176383880
    assert value['workflow_run']['head_sha']=='28ce458f71ce4a13a2e6038cd798872ef47e8e96'

def validate_digest(value):
    assert value==ZIP_SHA,'ARTIFACT_ZIP_HASH_MISMATCH'

def main(root,out):
    root=Path(root)
    assert not root.exists(),'FRESH_DOWNLOAD_REQUIRED'
    os.umask(0o077)
    root.mkdir(mode=0o700)
    metadata=json.loads(subprocess.check_output(['gh','api',API]))
    validate_metadata(metadata)
    archive=root/'artifact.zip'
    with archive.open('xb') as stream:
        subprocess.run(['gh','api',API+'/zip'],stdout=stream,check=True)
    with archive.open('rb') as stream:
        validate_digest(hashlib.file_digest(stream,'sha256').hexdigest())
    with zipfile.ZipFile(archive) as bundle:
        assert sorted(bundle.namelist())==['maintenance.oci.tar','production.oci.tar'],'ARTIFACT_MEMBERS'
        for name in bundle.namelist():
            with bundle.open(name) as source,(root/name).open('xb') as destination:
                shutil.copyfileobj(source,destination)
    Path(out).write_text(json.dumps(dict(artifact_id=metadata['id'],artifact_name=metadata['name'],
        certification_run=38176383880,artifact_zip_digest='sha256:'+ZIP_SHA,
        downloaded_zip_verified=True),sort_keys=True))

if __name__=='__main__':
    main(*sys.argv[1:])
