"""Download one pinned Actions artifact; authenticate only via GH_TOKEN."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

ZIP_SHA='cf28f8e3f6eb2fc86d0a3b96b926df541019d0197d5da1a72e60b3dfe48a1e51'
API='repos/renathaux/nathauxfx-backend/actions/artifacts/11697286441'

def validate_metadata(value):
    assert value['id']==11697286441
    assert value['name']=='tested-oci-1-38124754867'
    assert value['digest']=='sha256:'+ZIP_SHA
    assert value['expired'] is False
    assert value['workflow_run']['id']==38124754867
    assert value['workflow_run']['head_sha']=='0864b3b41e88e7c3ca03e8cf54ed265ff0edeab4'

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
        certification_run=38124754867,artifact_zip_digest='sha256:'+ZIP_SHA,
        downloaded_zip_verified=True),sort_keys=True))

if __name__=='__main__':
    main(*sys.argv[1:])
