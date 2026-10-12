"""Two pinned test-only inputs; never materialize them in release source."""
from contextlib import contextmanager
import hashlib
from pathlib import Path
import tempfile

SOURCE='4c1c483086f08a8c2596cb400725071d03a1249e'
INPUTS={
 'test_strategy_studio_owner_boundary.py':dict(size=8312,sha256='8ed98937e21329470c29c230bcdf8b23ae2dda495b1261257c07c92a981438d8',blob='32c16f76a43ae0947c8eb6565071570254b5ecc5'),
 'test_strategy_studio_service.py':dict(size=7964,sha256='0960a3adda2314e2af4d951c537cf35dc9882645df6ac60ed8c0b237935e15a9',blob='6b8b76649f46dcd8e69552abd822518e84989a81')}

def required_expand(root,*patterns):
    found=set()
    for pattern in patterns:
        matches=sorted(root.glob(pattern))
        if not matches or any(not p.is_file() or p.is_symlink() for p in matches):
            raise ValueError('REQUIRED_TEST_INPUT_MISSING:'+pattern)
        found.update(str(p.relative_to(root)) for p in matches)
    return sorted(found)

@contextmanager
def auth_overlay(root,harness):
    root=Path(root).resolve(); harness=Path(harness).resolve()
    with tempfile.TemporaryDirectory(prefix='auth-test-overlay-',dir=root.parent) as name:
        directory=Path(name)
        for filename,pin in INPUTS.items():
            original=harness/'broad-inputs/Backend/tests'/filename
            if original.is_symlink():raise ValueError('TEST_INPUT_SYMLINK')
            raw=original.read_bytes()
            if len(raw)!=pin['size'] or hashlib.sha256(raw).hexdigest()!=pin['sha256']:
                raise ValueError('TEST_INPUT_PIN_MISMATCH:'+filename)
            if (root/'Backend/tests'/filename).exists():raise ValueError('OVERLAY_SOURCE_COLLISION')
            (directory/filename).write_bytes(raw)
        yield dict(directory=directory,test=directory/'test_strategy_studio_owner_boundary.py')
