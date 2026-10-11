"""Exercise actual shim subprocesses against a disposable Git-free export."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from git_shim import temporary_git_shim
from verify_source import verify


class GitShimTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='manifest-git-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)/'source'
        shutil.copytree(Path(__file__).resolve().parents[1]/'source', self.root)
        for path in [self.root,*self.root.rglob('*')]:
            path.chmod(0o700 if path.is_dir() else 0o600)
        self.manifest = json.loads((self.root/'certification-source-manifest.json').read_bytes())

    def run_git(self, args, cwd, env=None, real=False):
        return subprocess.run(['/usr/bin/git' if real else 'git', *args], cwd=cwd,
            env=env, capture_output=True, text=True)

    def test_exact_command_manifest_list_and_all_file_hashes(self):
        before=verify(self.root)
        expected=sorted(e['path'][8:] for e in self.manifest['files']
                        if e['path'].startswith('Backend/') and e['path'].endswith('.py'))
        with temporary_git_shim(self.root) as env:
            result=self.run_git(['ls-files','*.py'], self.root/'Backend', env)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(result.stdout.splitlines(),expected)
            for entry in self.manifest['files']:
                if entry['path'][8:] in expected:
                    self.assertEqual(hashlib.sha256((self.root/entry['path']).read_bytes()).hexdigest(),entry['sha256'])
        self.assertEqual(verify(self.root),before)
        self.assertFalse(list(self.root.rglob('.git')))

    def test_other_commands_delegate_unchanged(self):
        with temporary_git_shim(self.root) as env:
            for args in (['status'],['ls-files','--cached','*.py'],['--version']):
                with self.subTest(args=args):
                    real=self.run_git(args,self.root/'Backend',env,real=True)
                    shim=self.run_git(args,self.root/'Backend',env)
                    self.assertEqual((shim.returncode,shim.stdout,shim.stderr),(real.returncode,real.stdout,real.stderr))
            self.assertNotEqual(self.run_git(['status'],self.root/'Backend',env).returncode,0)

    def test_wrong_directory_delegates(self):
        with temporary_git_shim(self.root) as env:
            real=self.run_git(['ls-files','*.py'],self.root,env,real=True)
            shim=self.run_git(['ls-files','*.py'],self.root,env)
            self.assertEqual((shim.returncode,shim.stdout,shim.stderr),(real.returncode,real.stdout,real.stderr))
            self.assertNotEqual(shim.returncode,0)

    def test_changed_manifest_rejected_after_initial_verification(self):
        with temporary_git_shim(self.root) as env:
            path=self.root/'certification-source-manifest.json'
            path.write_bytes(path.read_bytes()+b'\n')
            result=self.run_git(['ls-files','*.py'],self.root/'Backend',env)
            self.assertNotEqual(result.returncode,0)
            self.assertEqual(result.stdout,'')

    def test_unmanifested_test_input_blocks_instead_of_entering_list(self):
        with temporary_git_shim(self.root) as env:
            path=self.root/'Backend/scripts/migrate_sqlite_to_neon.py'
            path.parent.mkdir(exist_ok=True)
            path.write_text('# synthetic extra\n')
            result=self.run_git(['ls-files','*.py'],self.root/'Backend',env)
            self.assertNotEqual(result.returncode,0)
            self.assertEqual(result.stdout,'')

    def test_exception_cleanup_and_parent_path_unchanged(self):
        original=os.environ['PATH']
        with self.assertRaisesRegex(RuntimeError,'injected'):
            with temporary_git_shim(self.root) as env:
                directory=Path(env['PATH'].split(os.pathsep)[0])
                self.assertTrue((directory/'git').is_file())
                raise RuntimeError('injected')
        self.assertFalse(directory.exists())
        self.assertEqual(os.environ['PATH'],original)


if __name__ == '__main__': unittest.main()
