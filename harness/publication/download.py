"""Download one pinned Actions artifact; authenticate only via GH_TOKEN."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

ZIP_SHA='f6bb7557ff6c23782fcac3ac435827594b2954e1a5acc6c3bb121b20d4aa8a26'
API='repos/renathaux/nathauxfx-backend/actions/artifacts/11907941907'

def validate_metadata(value):
    assert value['id']==11907941907
    assert value['name']=='tested-oci-1-38584096907'
    assert value['digest']=='sha256:'+ZIP_SHA
    assert value['expired'] is False
    assert value['workflow_run']['id']==38584096907
    assert value['workflow_run']['head_sha']=='8328d8025da84a47b56a4d6548126a496b1aa19a'

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
        certification_run=38584096907,artifact_zip_digest='sha256:'+ZIP_SHA,
        downloaded_zip_verified=True),sort_keys=True))

if __name__=='__main__':
    main(*sys.argv[1:])
