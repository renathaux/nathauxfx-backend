"""Test-only exact-command adapter; release source and real Git remain untouched."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from verify_source import verify


def main(root):
    root=Path(root).resolve()
    if Path.cwd() != root/'Backend' or sys.argv[1:] != ['ls-files','*.py']:
        os.execv('/usr/bin/git',['/usr/bin/git',*sys.argv[1:]])
    try:
        verify(root)  # Recheck independent pin, 369 entries, bytes and exact file set.
        manifest=json.loads((root/'certification-source-manifest.json').read_bytes())
        names=sorted(e['path'][8:] for e in manifest['files']
                     if e['path'].startswith('Backend/') and e['path'].endswith('.py'))
    except Exception:
        print('CERTIFICATION_GIT_MANIFEST_REJECTED',file=sys.stderr)
        raise SystemExit(1)
    print('\n'.join(names))


@contextmanager
def temporary_git_shim(root):
    root=Path(root).resolve()
    verify(root)  # Must succeed before materializing the adapter.
    with tempfile.TemporaryDirectory(prefix='test-bin-',dir=root.parent) as directory:
        shim=Path(directory)/'git'
        shim.write_text('#!'+sys.executable+' -B\nimport sys\nsys.path.insert(0,'+
            repr(str(Path(__file__).resolve().parent))+')\nfrom git_shim import main\nmain('+repr(str(root))+')\n')
        shim.chmod(0o700)
        env=dict(os.environ,PATH=directory+os.pathsep+os.environ['PATH'])
        yield env
    assert not Path(directory).exists(), 'GIT_SHIM_CLEANUP_FAILED'


def prove(root,env):
    """Native pre-suite proof using real commands and independently read entries."""
    root=Path(root)
    before=verify(root)
    manifest=json.loads((root/'certification-source-manifest.json').read_bytes())
    expected=sorted(e['path'][8:] for e in manifest['files']
                     if e['path'].startswith('Backend/') and e['path'].endswith('.py'))
    run=lambda exe,args: subprocess.run([exe,*args],cwd=root/'Backend',env=env,capture_output=True,text=True)
    listed=run('git',['ls-files','*.py'])
    assert listed.returncode==0 and listed.stdout.splitlines()==expected
    actual=run('git',['status']);real=run('/usr/bin/git',['status'])
    assert actual.returncode!=0
    assert (actual.returncode,actual.stdout,actual.stderr)==(real.returncode,real.stdout,real.stderr)
    assert verify(root)==before
    return dict(exact_command=True,file_list_source='PINNED_MANIFEST',python_files=len(expected),
                delegation_verified=True,source=before)
