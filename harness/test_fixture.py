import sqlite3
import tempfile
import unittest
from pathlib import Path
import fixture


class FixtureTests(unittest.TestCase):
    def test_empty_fixed_legacy_contract_and_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / 'flowsignal.db'
            with fixture.temporary_database(path):
                with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as c:
                    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    self.assertEqual(tables, {'users', 'runtime_settings', 'news_trading_mode_audit', 'auto_trade_state_audit'})
                    for name in tables:
                        self.assertEqual(c.execute('SELECT count(*) FROM '+name).fetchone()[0], 0)
                    self.assertEqual(c.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                    self.assertEqual([r[1] for r in c.execute('PRAGMA table_info(users)')],
                                     ['id', 'email', 'hashed_password', 'is_active'])
            self.assertEqual(list(root.iterdir()), [])

    def test_failure_cleans_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'flowsignal.db'
            with self.assertRaisesRegex(RuntimeError, 'test failure'):
                with fixture.temporary_database(path):
                    raise RuntimeError('test failure')
            self.assertFalse(path.exists())

    def test_existing_input_never_overwritten_or_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'flowsignal.db'
            path.write_bytes(b'preserve')
            with self.assertRaises(FileExistsError):
                with fixture.temporary_database(path):
                    pass
            self.assertEqual(path.read_bytes(), b'preserve')

if __name__ == '__main__':
    unittest.main()
