"""Harness-only tests: semantic loss or hidden exclusions must fail."""
import json
from pathlib import Path
import sqlite3
import os
import subprocess
import sys
import tempfile
import unittest


class DiagnosticContractTests(unittest.TestCase):
    def module(self):
        try:
            import diagnostic_contract
        except ImportError:
            self.fail('diagnostic_contract is not implemented')
        return diagnostic_contract

    def test_full_failure_and_passing_nodes_retained(self):
        m = self.module()
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/'test.xml'
            p.write_text('<testsuites><testsuite><testcase classname="a" name="b"><failure message="first&#10;second">full body</failure></testcase><testcase classname="a" name="c"/></testsuite></testsuites>')
            rows = m.read_cases(p)
            self.assertEqual(rows[0]['message'], 'first\nsecond')
            self.assertEqual(rows[0]['body'], 'full body')
            self.assertEqual(rows[1]['status'], 'passed')

    def test_semantic_or_uuid_or_path_changes_change_fingerprint(self):
        m = self.module()
        for x, y in [('price=1', 'price=2'), ('/tmp/a', '/tmp/b'),
                     ('12345678-1234-1234-1234-123456789012', '22345678-1234-1234-1234-123456789012')]:
            self.assertNotEqual(m.digest([{'message': x}]), m.digest([{'message': y}]))

    def test_only_exact_bls_node_has_outside_scope_label(self):
        m = self.module()
        self.assertEqual(m.classify(m.BLS_NODE), 'OUTSIDE_RELEASE_SURFACE')
        self.assertEqual(m.classify(m.BLS_NODE+'x'), 'SUPPORTED_OR_UNEXPLAINED_PENDING_REVIEW')
        self.assertEqual(m.classify('tests.test_official_backfill::other'), 'SUPPORTED_OR_UNEXPLAINED_PENDING_REVIEW')

    def test_sqlite_is_new_valid_empty_and_never_overwrites(self):
        m = self.module()
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/'db.sqlite'
            v = m.create_empty_sqlite(p)
            self.assertEqual(v['schema'], [])
            self.assertEqual(v['integrity'], 'ok')
            with sqlite3.connect(f'file:{p}?mode=ro', uri=True) as c:
                self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0], 0)
            before = p.read_bytes()
            with self.assertRaises(FileExistsError):
                m.create_empty_sqlite(p)
            self.assertEqual(p.read_bytes(), before)

    def test_json_safe_diagnostic_preserves_all_fields_and_rejects_repr(self):
        m = self.module()
        value = {'b': [1, True, None], 'a': {'price': 1.25}}
        self.assertEqual(json.loads(m.canonical(value)), value)
        with self.assertRaises(TypeError):
            m.canonical({'unknown': object()})
        with self.assertRaises(ValueError):
            m.canonical({'price': float('nan')})
        class CustomDict(dict):
            pass
        for bad in ({1: 'a', '1': 'b'}, (1, 2), CustomDict(a=1)):
            with self.assertRaises(TypeError):
                m.canonical(bad)

    def test_observer_captures_full_synthetic_values_without_changing_failure(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            evidence = root/'evidence'
            evidence.mkdir()
            (root/'test_synthetic.py').write_text('''
from types import ModuleType
def test_actual_position_sync_a_b_a_preserves_frozen_management():
    trade = {"nested": {"price": 1.25}, "identity": "synthetic"}
    api = ModuleType('api')
    api.LIVE_ACTIVE_ORDERS = {"EURUSD": trade}
    assert api.LIVE_ACTIVE_ORDERS["EURUSD"] is None
''')
            env = dict(os.environ, PYTHONPATH=str(Path(__file__).parent.resolve())+os.pathsep+os.environ.get('PYTHONPATH', ''),
                       DIAGNOSTIC_EVIDENCE=str(evidence), PYTHONDONTWRITEBYTECODE='1')
            p = subprocess.run([sys.executable, '-B', '-m', 'pytest', '-q', '-p', 'no:cacheprovider',
                                '-p', 'diagnostic_observer'], cwd=root, env=env, capture_output=True)
            self.assertEqual(p.returncode, 1, p.stdout.decode()+p.stderr.decode())
            data = json.loads(next(evidence.glob('semantic-*.json')).read_text())
            self.assertEqual(data['current'], {'nested': {'price': 1.25}, 'identity': 'synthetic'})
            self.assertEqual(data['supplied'], data['current'])
            self.assertTrue(data['equal_at_failure'])
            self.assertEqual(data['outcome'], 'failed')

    def test_missing_observation_is_not_claimed_equal(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            evidence = root/'evidence'
            evidence.mkdir()
            (root/'test_synthetic.py').write_text('def test_actual_position_sync_a_b_a_preserves_frozen_management():\n    assert False\n')
            env = dict(os.environ, PYTHONPATH=str(Path(__file__).parent.resolve())+os.pathsep+os.environ.get('PYTHONPATH', ''),
                       DIAGNOSTIC_EVIDENCE=str(evidence), PYTHONDONTWRITEBYTECODE='1')
            p = subprocess.run([sys.executable, '-B', '-m', 'pytest', '-q', '-p', 'no:cacheprovider',
                                '-p', 'diagnostic_observer'], cwd=root, env=env, capture_output=True)
            self.assertEqual(p.returncode, 1)
            data = json.loads(next(evidence.glob('semantic-*.json')).read_text())
            self.assertEqual(data['observation'], 'UNAVAILABLE')
            self.assertNotIn('equal_at_failure', data)

if __name__ == '__main__':
    unittest.main()
