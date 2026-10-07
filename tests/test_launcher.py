import importlib.util
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]

class LauncherContract(unittest.TestCase):
    def test_arbitrary_helper_command_rejected_without_execution(self):
        p = subprocess.run([sys.executable, '-B', str(ROOT/'maintenance/cutover_helper.py'), 'sh', '-c', 'echo UNAUTHORIZED'], capture_output=True, text=True)
        self.assertEqual(p.returncode, 1)
        self.assertEqual(p.stdout, '')
        self.assertEqual(p.stderr, 'MAINTENANCE_HELPER_REJECTED\n')

    def test_invalid_port_rejected(self):
        path = ROOT/'maintenance/launcher.py'
        self.assertTrue(path.exists(), 'maintenance implementation missing')
        spec = importlib.util.spec_from_file_location('launcher', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for value in ('0','-1','65536','not-a-port'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                module.valid_port(value)

    def test_helper_environment_removes_python_injection(self):
        path = ROOT/'maintenance/cutover_helper.py'
        self.assertTrue(path.exists(), 'helper implementation missing')
        spec = importlib.util.spec_from_file_location('helper', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        env = module.safe_environment({'PYTHONPATH':'/unsafe','PYTHONHOME':'/unsafe','LD_PRELOAD':'/unsafe','CUTOVER_DATABASE_URL':'synthetic-test-only','DATABASE_URL':'do-not-inherit'})
        self.assertNotIn('PYTHONPATH', env)
        self.assertNotIn('PYTHONHOME', env)
        self.assertNotIn('LD_PRELOAD', env)
        self.assertNotIn('DATABASE_URL', env)
        self.assertEqual(env['CUTOVER_DATABASE_URL'], 'synthetic-test-only')

if __name__ == '__main__': unittest.main()
