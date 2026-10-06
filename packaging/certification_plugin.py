"""Replace ONLY Git enumeration in the native read-only-source fixture.

The original test assertion body is unchanged. Every copied input is manifested
and hash-verified; this plugin is mounted tooling and never included in image.
"""
import hashlib
import json
from pathlib import Path
import shutil

def pytest_fixture_setup(fixturedef,request):
    if fixturedef.argname!='readonly_source' or request.module.__name__.rsplit('.',1)[-1]!='test_runtime_paths':
        return None
    tmp=request.getfixturevalue('tmp_path')
    source=tmp/'application'/'Backend'
    manifest=json.loads(Path('/app/certification-source-manifest.json').read_bytes())
    for entry in manifest['files']:
        name=entry['path']
        if not name.startswith('Backend/') or not name.endswith('.py'):continue
        raw=(Path('/app')/name).read_bytes()
        assert hashlib.sha256(raw).hexdigest()==entry['sha256']
        target=source/name.removeprefix('Backend/')
        target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(raw)
    for path in source.rglob('*'):path.chmod(0o555 if path.is_dir() else 0o444)
    source.chmod(0o555)
    def cleanup():
        source.chmod(0o755)
        for path in source.rglob('*'):path.chmod(0o755 if path.is_dir() else 0o644)
    request.addfinalizer(cleanup)
    fixturedef.cached_result=(source,fixturedef.cache_key(request),None)
    return source
