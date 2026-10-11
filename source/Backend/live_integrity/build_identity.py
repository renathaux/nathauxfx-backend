"""Git-free, read-only release evidence verification; see docs/immutable-runtime-identity.md."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import stat

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
# Separately provisioned release anchor, never read from environment or the record.
EXPECTATIONS_PATH = Path('/opt/flowsignal-release/expectations.json')
SCHEMA = 'flowsignal-immutable-build/v1'
FIELDS = frozenset(('schema', 'packaging_source_sha', 'source_manifest_sha256',
    'dependency_lock_sha256', 'dependency_metadata_sha256', 'python_version',
    'base_image_digest', 'certified_dependency_run_id', 'certified_dependency_commit'))
MANIFEST = 'certification-source-manifest.json'
LOCK = 'Backend/requirements.production.lock'
METADATA = 'Backend/requirements.production.metadata.json'
LIMIT = 32 * 1024 * 1024


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True, allow_nan=False).encode('utf-8')


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError()
            result[key] = value
        return result
    def invalid(value):
        raise ValueError()
    result = json.loads(data.decode('utf-8'), object_pairs_hook=pairs, parse_constant=invalid)
    if type(result) is not dict:
        raise ValueError()
    return result


def _read(path):
    """No-follow descriptor walk, bounded regular-file read; no writes or chmod."""
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError()
    directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=directory)
            os.close(directory)
            directory = child
        if os.fstat(directory).st_mode & 0o222:
            raise ValueError()
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=directory)
        try:
            before = os.fstat(fd)
            if (not stat.S_ISREG(before.st_mode) or before.st_mode & 0o222
                    or before.st_nlink != 1 or before.st_size > LIMIT):
                raise ValueError()
            with os.fdopen(fd, 'rb', closefd=False) as stream:
                data = stream.read(LIMIT + 1)
            after = os.fstat(fd)
            identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mode,
                                  s.st_mtime_ns, s.st_ctime_ns, s.st_nlink)
            if identity(before) != identity(after) or len(data) != before.st_size:
                raise ValueError()
            return data, stat.S_IMODE(before.st_mode)
        finally:
            os.close(fd)
    finally:
        os.close(directory)


def _identity(record, *, timestamp=False):
    if type(record) is not dict or set(record) - FIELDS - ({'build_timestamp'} if timestamp else set()):
        raise ValueError()
    if not FIELDS <= record.keys() or any(type(record[k]) is not str or not record[k] for k in FIELDS):
        raise ValueError()
    if 'build_timestamp' in record and type(record['build_timestamp']) is not str:
        raise ValueError()
    if record['schema'] != SCHEMA:
        raise ValueError()
    for key in ('packaging_source_sha', 'certified_dependency_commit'):
        if not re.fullmatch('[0-9a-f]{40}', record[key]):
            raise ValueError()
    for key in ('source_manifest_sha256','dependency_lock_sha256','dependency_metadata_sha256'):
        if not re.fullmatch('[0-9a-f]{64}', record[key]):
            raise ValueError()
    if (not re.fullmatch(r'\d+\.\d+\.\d+', record['python_version'])
            or not re.fullmatch('sha256:[0-9a-f]{64}', record['base_image_digest'])
            or not re.fullmatch('[1-9][0-9]*', record['certified_dependency_run_id'])):
        raise ValueError()
    return {key: record[key] for key in FIELDS}


def _sources(root, manifest, expected):
    if (manifest.get('schema') != 'flowsignal-certification-source/v1'
            or manifest.get('packaging_source_sha') != expected['packaging_source_sha']
            or type(manifest.get('files')) is not list or not manifest['files']):
        raise ValueError()
    seen = set()
    for item in manifest['files']:
        if type(item) is not dict or set(item) != {'path','size','sha256','mode'}:
            raise ValueError()
        name = item['path']
        if (type(name) is not str or not name or '\\' in name
                or PurePosixPath(name).is_absolute() or any(p in ('', '.', '..', '.git') for p in name.split('/'))
                or name in seen or name in (MANIFEST,'build-record.json',LOCK,METADATA)):
            raise ValueError()
        if (type(item['size']) is not int or item['size'] < 0
                or item['mode'] not in ('100644','100755')
                or type(item['sha256']) is not str or not re.fullmatch('[0-9a-f]{64}', item['sha256'])):
            raise ValueError()
        data, mode = _read(root / name)
        if (len(data) != item['size'] or _sha(data) != item['sha256']
                or mode != (0o555 if item['mode'] == '100755' else 0o444)):
            raise ValueError()
        seen.add(name)
        parent = (root / name).parent
        while parent != root:
            if parent.stat().st_mode & 0o222:
                raise ValueError()
            parent = parent.parent
    # Do not permit undeclared importable code to shadow verified inputs.
    def walk_error(error):
        raise ValueError() from None
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
        if Path(directory) == root and '.git' in dirs:
            dirs.remove('.git')
        for name in dirs + files:
            if name == '.git' and Path(directory) == root:
                continue
            path = Path(directory) / name
            if path.is_symlink():
                raise ValueError()
            if path.is_file() and path.suffix.lower() in ('.py','.pyc','.pyo','.so','.pyd','.pth','.zip'):
                if path.relative_to(root).as_posix() not in seen:
                    raise ValueError()


def capture():
    """Preserve the verifier contract, without claiming runtime Git cleanliness."""
    try:
        expected = _identity(_json(_read(EXPECTATIONS_PATH)[0]))
        root = PACKAGE_ROOT
        record = _identity(_json(_read(root / 'build-record.json')[0]), timestamp=True)
        if record != expected or platform.python_version() != expected['python_version']:
            raise ValueError()
        manifest_bytes = _read(root / MANIFEST)[0]
        if (_sha(manifest_bytes) != expected['source_manifest_sha256']
                or _sha(_read(root / LOCK)[0]) != expected['dependency_lock_sha256']
                or _sha(_read(root / METADATA)[0]) != expected['dependency_metadata_sha256']):
            raise ValueError()
        _sources(root, _json(manifest_bytes), expected)
        return dict(backend_git_sha=expected['packaging_source_sha'],
                    build_identity=_sha(_canonical(record)), clean_source=True,
                    identity_schema=SCHEMA, source_manifest_sha256=expected['source_manifest_sha256'])
    except Exception:
        raise ValueError('BUILD_IDENTITY_UNVERIFIED') from None
