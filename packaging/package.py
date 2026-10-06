"""Packaging-only verifier/preparer; no application import at preparation time."""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile

SOURCE='0dbbef69bb293833c299c1fe3e728454b3fa1c45'
MANIFEST='e6aa098027a0246d53ca0f2c6fe599b23125b376d5e33c260896efc02ab73fcd'
LOCK='1d8e368b5645db69cab33479c56d7db0827155898368a419fc80ab9819ee5736'
METADATA='8e24bda82fa3cc5bdcf21f19c86eec85eae74e24e115a402066bfafe884e5e89'
BASE='sha256:c6f0b5b3a167963de3cc7cd97fe1a5d07105c8d27f47507ab31d648b533b05c7'
GENERATED={'certification-source-manifest.json','Backend/requirements.production.lock','Backend/requirements.production.metadata.json'}

def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode()
def sha(data):return hashlib.sha256(data).hexdigest()
def build_record():
    return dict(schema='flowsignal-immutable-build/v1',packaging_source_sha=SOURCE,
        source_manifest_sha256=MANIFEST,dependency_lock_sha256=LOCK,dependency_metadata_sha256=METADATA,
        python_version='3.14.3',base_image_digest=BASE,certified_dependency_run_id='37414992580',
        certified_dependency_commit='2a237b3cbc3f066a1b1d19eaf1f5470e72472f31')

def safe_path(name):
    if (not isinstance(name,str) or not name or PurePosixPath(name).is_absolute() or '\\' in name
        or any(p in ('','.','..','.git','__pycache__','data','database','cache','outputs') or p.startswith('.env') for p in name.split('/'))):
        raise ValueError('UNSAFE_PACKAGING_PATH')
    return name

def validate_source(root, *, built=False):
    root=Path(root)
    raw=(root/'certification-source-manifest.json').read_bytes()
    if sha(raw)!=MANIFEST:raise ValueError('MANIFEST_PIN_MISMATCH')
    manifest=json.loads(raw)
    if manifest['packaging_source_sha']!=SOURCE:raise ValueError('SOURCE_MISMATCH')
    allowed=set(GENERATED)
    for entry in manifest['files']:
        name=safe_path(entry['path'])
        if name in allowed:raise ValueError('DUPLICATE_SOURCE')
        allowed.add(name)
        path=root/name
        if path.is_symlink() or not path.is_file():raise ValueError('SOURCE_TYPE')
        data=path.read_bytes()
        if len(data)!=entry['size'] or sha(data)!=entry['sha256']:raise ValueError('SOURCE_BYTES_MISMATCH')
        expected=0o555 if entry['mode']=='100755' else 0o444
        if built and stat.S_IMODE(path.stat().st_mode)!=expected:raise ValueError('SOURCE_MODE')
    if built:allowed.add('build-record.json')
    for path in root.rglob('*'):
        if path.is_symlink():raise ValueError('SOURCE_SYMLINK')
    actual={p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file()}
    if actual!=allowed:raise ValueError('UNEXPECTED_OR_MISSING_PACKAGED_FILE')
    for name,digest in [('requirements.production.lock',LOCK),('requirements.production.metadata.json',METADATA)]:
        if sha((root/'Backend'/name).read_bytes())!=digest:raise ValueError('DEPENDENCY_PIN_MISMATCH')
    return manifest

def compare_profile(expected,actual):
    if expected!=actual:raise ValueError('APPLICATION_PROFILE_MISMATCH')
    return True

def profile(root):
    metadata=json.loads((Path(root)/'Backend/requirements.production.metadata.json').read_bytes())
    norm=lambda n:re.sub('[-_.]+','-',n).lower()
    expected={norm(p['name']):p['version'] for p in metadata['packages']}
    installed={norm(d.metadata['Name']):d.version for d in importlib.metadata.distributions()}
    tooling={k:installed.pop(k) for k in ('pip',) if k in installed}
    compare_profile(expected,installed)
    if len(installed)!=39 or installed.get('greenlet')!='3.5.6':raise ValueError('PROFILE_COUNT')
    return dict(application_packages=installed,installer_tooling=tooling)

def prepare(source,destination):
    source=Path(source); destination=Path(destination)
    manifest=validate_source(source)
    if destination.exists():raise ValueError('CONTEXT_MUST_BE_NEW')
    destination.mkdir(parents=True)
    app=destination/'app';app.mkdir()
    for name in sorted({e['path'] for e in manifest['files']} | GENERATED):
        dst=app/name;dst.parent.mkdir(parents=True,exist_ok=True)
        dst.write_bytes((source/name).read_bytes())
        dst.chmod(0o555 if (source/name).stat().st_mode&0o111 else 0o444)
    release=destination/'release';release.mkdir()
    # Record and independent expectations are provisioned from reviewed constants,
    # never harvested from self-claimed package data or environment variables.
    (release/'build-record.json').write_bytes(canonical(build_record()))
    (release/'expectations.json').write_bytes(canonical(build_record()))
    here=Path(__file__).parent
    for name in ('Dockerfile','.dockerignore','start.sh','package.py'):
        shutil.copyfile(here/name,destination/name)
    print(json.dumps({'context_source_files':len(manifest['files']),'source_manifest_sha256':MANIFEST}))

def installed_check():
    if platform.python_version()!='3.14.3' or platform.machine()!='x86_64' or platform.system()!='Linux':
        raise ValueError('RUNTIME_PLATFORM')
    if os.geteuid()==0:raise ValueError('ROOT_RUNTIME')
    root=Path('/app')
    manifest=validate_source(root,built=True)
    for path in [root,*root.rglob('*'),Path('/opt/flowsignal-release'),Path('/opt/flowsignal-release/expectations.json')]:
        s=path.stat()
        if s.st_uid!=0 or s.st_mode&0o222:raise ValueError('MUTABLE_APPLICATION_OR_EVIDENCE')
    sys.path.insert(0,'/app/Backend')
    from live_integrity.build_identity import capture
    identity=capture()
    if identity['build_identity']!=sha(canonical(build_record())) or not identity['clean_source']:
        raise ValueError('BUILD_IDENTITY')
    result=profile(root)
    # These imports load the native extensions rather than only metadata.
    import bcrypt,cryptography.hazmat.bindings._rust,_cffi_backend,charset_normalizer.md
    import greenlet,numpy,pandas,psycopg2,pydantic_core,sqlalchemy,markupsafe
    import websockets.speedups
    native=[]
    for path in Path('/opt/venv').rglob('*.so'):
        proc=subprocess.run(['ldd',str(path)],capture_output=True,text=True)
        if proc.returncode or 'not found' in proc.stdout+proc.stderr:raise ValueError('NATIVE_LIBRARY_UNRESOLVED')
        native.append(str(path))
    result.update(build=identity,source_files=len(manifest['files']),native_extension_count=len(native),
        application_filesystem_sha256=sha(canonical(manifest['files'])),python=platform.python_version(),uid=os.getuid())
    print(json.dumps(result,sort_keys=True))

def oci_identity(path):
    with tarfile.open(path,'r') as archive:
        def blob(digest):
            raw=archive.extractfile('blobs/sha256/'+digest.removeprefix('sha256:')).read()
            if 'sha256:'+sha(raw)!=digest:raise ValueError('OCI_BLOB_HASH')
            return raw
        index_raw=archive.extractfile('index.json').read()
        index=json.loads(index_raw)
        if len(index['manifests'])!=1:raise ValueError('UNEXPECTED_OCI_INDEX')
        descriptor=index['manifests'][0]
        manifest=json.loads(blob(descriptor['digest']))
        while 'manifests' in manifest:
            if len(manifest['manifests'])!=1:raise ValueError('UNEXPECTED_OCI_NESTED_INDEX')
            descriptor=manifest['manifests'][0];manifest=json.loads(blob(descriptor['digest']))
        config=json.loads(blob(manifest['config']['digest']))
        if config['architecture']!='amd64' or config['os']!='linux':raise ValueError('OCI_PLATFORM')
        for layer in manifest['layers']:blob(layer['digest'])
        return dict(image_digest=descriptor['digest'],manifest_digest=descriptor['digest'],
            config_digest=manifest['config']['digest'],oci_index_sha256=sha(index_raw),
            layers=[v['digest'] for v in manifest['layers']])

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('operation',choices=['prepare','validate','image','oci'])
    p.add_argument('paths',nargs='*');args=p.parse_args()
    if args.operation=='prepare':prepare(*args.paths)
    elif args.operation=='validate':print(json.dumps({'verified_source_files':len(validate_source(args.paths[0])['files'])}))
    elif args.operation=='image':installed_check()
    else:print(json.dumps(oci_identity(args.paths[0]),sort_keys=True))
