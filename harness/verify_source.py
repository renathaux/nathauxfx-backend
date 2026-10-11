"""Independent pins for the frozen export, without importing application code."""
import hashlib
import json
from pathlib import Path
import sys

SOURCE = '1a664174dfcc8b42da12718244c581aebfdb54f2'
MANIFEST = '1550749d73db81400014682c8d3eb5a4cea94964b01174d83907738fc268f4ef'
LOCK = '1d8e368b5645db69cab33479c56d7db0827155898368a419fc80ab9819ee5736'
METADATA = '8e24bda82fa3cc5bdcf21f19c86eec85eae74e24e115a402066bfafe884e5e89'
GENERATED = {'certification-source-manifest.json', 'Backend/requirements.production.lock',
             'Backend/requirements.production.metadata.json'}

def verify(root):
    root = Path(root)
    raw = (root/'certification-source-manifest.json').read_bytes()
    assert hashlib.sha256(raw).hexdigest() == MANIFEST, 'MANIFEST_PIN'
    manifest = json.loads(raw)
    assert manifest['packaging_source_sha'] == SOURCE
    assert manifest['application_input_sha'] == 'c8c82ece5856c2e040b75070bad7888beedd2f60'
    assert manifest['maintenance_input_sha'] == 'd223082822bd209858ce4c699e5fa907e654dc29'
    assert len(manifest['files']) == 370
    expected = set(GENERATED)
    for entry in manifest['files']:
        name = entry['path']
        assert name not in expected
        expected.add(name)
        path = root/name
        assert not path.is_symlink()
        data = path.read_bytes()
        assert len(data) == entry['size'] and hashlib.sha256(data).hexdigest() == entry['sha256']
    paths = list(root.rglob('*'))
    assert not any(p.is_symlink() for p in paths)
    assert {p.relative_to(root).as_posix() for p in paths if p.is_file()} == expected, 'EXPORT_EXTRA_FILES'
    assert not any(p.name in ('.git', '__pycache__') or p.name.startswith('.env') for p in paths)
    assert not (root/'Backend/database/flowsignal.db').exists()
    assert not (root/'Backend/scripts/migrate_sqlite_to_neon.py').exists()
    assert not list((root/'Backend/data').glob('*.json'))
    for name, pin in [('requirements.production.lock', LOCK), ('requirements.production.metadata.json', METADATA)]:
        assert hashlib.sha256((root/'Backend'/name).read_bytes()).hexdigest() == pin
    return {'source_sha': SOURCE, 'source_files': 370, 'manifest_sha256': MANIFEST}

if __name__ == '__main__':
    print(json.dumps(verify(sys.argv[1]), sort_keys=True))
