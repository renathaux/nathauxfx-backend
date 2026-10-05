"""Fail closed unless the running release has verifiable clean source identity."""
import hashlib
import os
from pathlib import Path
import subprocess
import sys


def capture():
    root = Path(__file__).resolve().parents[2]
    def git(*args):
        return subprocess.check_output(['git','-C',str(root),*args],stderr=subprocess.DEVNULL,timeout=1,text=True).strip()
    try:
        sha = git('rev-parse','HEAD')
        tree = git('rev-parse','HEAD^{tree}')
        if len(sha)!=40 or git('status','--porcelain','--untracked-files=all'):
            raise ValueError()
        release = os.getenv('RENDER_GIT_COMMIT')
        if release and release != sha:
            raise ValueError()
        build = hashlib.sha256((sha+':'+tree+':'+sys.version).encode()).hexdigest()
        return dict(backend_git_sha=sha,source_tree=tree,build_identity=build,clean_source=True)
    except Exception:
        raise ValueError('BUILD_IDENTITY_UNVERIFIED') from None
