"""Synthetic Linux mount tests. Run as root in isolated mount/network namespaces.

The launcher and its interpreter must be installed at their image paths by the
disposable harness. No application source, database, or broker is imported.
"""
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import time
import unittest
import urllib.request

ROOT = Path('/state')
LAUNCHER = '/usr/local/lib/flowsignal-maintenance/launcher.py'
PYTHON = '/opt/venv/bin/python'


def snapshot(path):
    """No-atime bytes AND metadata, including directory entries and identity."""
    s = path.lstat()
    value = [s.st_mode, s.st_uid, s.st_gid, s.st_size, s.st_ino,
             s.st_mtime_ns, s.st_ctime_ns, s.st_atime_ns, s.st_nlink]
    if stat.S_ISLNK(s.st_mode):
        return value + [os.readlink(path)]
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NOATIME
    if stat.S_ISDIR(s.st_mode):
        flags |= os.O_DIRECTORY
    fd = os.open(path, flags)
    try:
        if stat.S_ISDIR(s.st_mode):
            return value + [{n: snapshot(path / n) for n in sorted(os.listdir(fd))}]
        with os.fdopen(os.dup(fd), 'rb') as stream:
            return value + [hashlib.sha256(stream.read()).hexdigest()]
    finally:
        os.close(fd)


class MountedLauncher(unittest.TestCase):
    def setUp(self):
        self.assertEqual(os.geteuid(), 0, 'Disposable Linux root namespace required')
        self.assertTrue(hasattr(os, 'O_NOATIME'))
        ROOT.mkdir(exist_ok=True)
        subprocess.run(['mount', '-t', 'tmpfs', '-o', 'mode=0700,strictatime',
                        'maintenance-test', str(ROOT)], check=True)
        self.mounted = True
        self.process = None

    def tearDown(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
            self.process.communicate(timeout=5)
        if self.mounted:
            subprocess.run(['umount', str(ROOT)], check=True)

    def populate(self):
        for name in ('live_backup.json', '.cutover/pinned.json',
                     '.cutover/lock', '.recovery-generations/synthetic/manifest.json',
                     '.recovery-generations/synthetic/payload.json'):
            p = ROOT / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b'{"synthetic":true}\n')
        for p in [ROOT, *ROOT.rglob('*')]:
            os.chown(p, 501, 1000)
            p.chmod(0o700 if p.is_dir() else 0o400)
            # Force a read to change atime unless O_NOATIME is genuinely used.
            os.utime(p, ns=(1_000_000_000, 2_000_000_000))

    def launch(self, success):
        self.process = subprocess.Popen([PYTHON, '-I', '-B', LAUNCHER],
            env={'PORT': '18941', 'PYTHONDONTWRITEBYTECODE': '1',
                 'DATABASE_URL': 'must-not-be-used', 'MIGRATION_DATABASE_URL': 'must-not-be-used'},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if not success:
            out, err = self.process.communicate(timeout=5)
            self.assertEqual(self.process.returncode, 1)
            self.assertEqual(out, b'')
            self.assertEqual(err, b'MAINTENANCE_START_REJECTED\n')
            return
        for _ in range(100):
            if self.process.poll() is not None:
                out, err = self.process.communicate()
                self.fail('Launcher rejected safe fixture: ' + err.decode())
            try:
                with urllib.request.urlopen('http://127.0.0.1:18941/', timeout=.1) as r:
                    body = json.load(r)
                break
            except (OSError, ValueError):
                time.sleep(.02)
        else:
            self.fail('Maintenance health timeout')
        self.assertEqual(body, dict(maintenance=True, runtime_uid=501, runtime_gid=1000,
            state_uid=501, state_gid=1000, state_mode='0700'))
        status = dict(line.split(':', 1) for line in Path(f'/proc/{self.process.pid}/status')
                      .read_text().splitlines() if ':' in line)
        self.assertEqual(status['Uid'].split(), ['501'] * 4)
        self.assertEqual(status['Gid'].split(), ['1000'] * 4)
        self.assertEqual(status['Groups'].strip(), '')
        self.assertEqual(int(status['CapEff'], 16), 0)
        environment = Path(f'/proc/{self.process.pid}/environ').read_bytes()
        self.assertNotIn(b'DATABASE_URL', environment)

    def test_empty_normalization(self):
        ROOT.chmod(0o2775)
        self.launch(True)
        s = ROOT.stat()
        self.assertEqual((s.st_uid, s.st_gid, stat.S_IMODE(s.st_mode)), (501, 1000, 0o700))
        self.assertEqual(list(ROOT.iterdir()), [])

    def test_populated_accepts_without_any_metadata_or_byte_change(self):
        self.populate()
        before = snapshot(ROOT)
        self.launch(True)
        self.assertEqual(snapshot(ROOT), before)

    def test_populated_wrong_owner_blocks_without_repair(self):
        self.populate()
        os.chown(ROOT, 0, 1000)
        before = snapshot(ROOT)
        self.launch(False)
        self.assertEqual(snapshot(ROOT), before)

    def test_populated_wrong_group_blocks_without_repair(self):
        self.populate()
        os.chown(ROOT, 501, 0)
        before = snapshot(ROOT)
        self.launch(False)
        self.assertEqual(snapshot(ROOT), before)

    def test_populated_readonly_mount_starts_without_writes(self):
        self.populate()
        subprocess.run(['mount', '-o', 'remount,ro', str(ROOT)], check=True)
        before = snapshot(ROOT)
        self.launch(True)
        self.assertEqual(snapshot(ROOT), before)

    def test_populated_wrong_mode_blocks_without_repair(self):
        self.populate()
        ROOT.chmod(0o2700)
        before = snapshot(ROOT)
        self.launch(False)
        self.assertEqual(snapshot(ROOT), before)

    def test_ordinary_directory_is_not_mount(self):
        subprocess.run(['umount', str(ROOT)], check=True)
        self.mounted = False
        self.populate()
        try:
            before = snapshot(ROOT)
            self.launch(False)
            self.assertEqual(snapshot(ROOT), before)
        finally:
            # Only synthetic files created above, in the disposable namespace.
            import shutil
            for p in ROOT.iterdir():
                if p.is_dir(): shutil.rmtree(p)
                else: p.unlink()

    def test_state_path_symlink_is_rejected(self):
        subprocess.run(['umount', str(ROOT)], check=True)
        self.mounted = False
        ROOT.rmdir()
        ROOT.symlink_to('/tmp', target_is_directory=True)
        try:
            self.launch(False)
            self.assertTrue(ROOT.is_symlink())
        finally:
            ROOT.unlink()
            ROOT.mkdir()


if __name__ == '__main__':
    unittest.main(verbosity=2)
