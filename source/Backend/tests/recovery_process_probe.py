"""Separate interpreter for disposable PostgreSQL ownership tests only."""
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_recovery_integration import _worker


class Channel:
    def recv(self):
        line = sys.stdin.readline()
        return json.loads(line) if line else 'stop'
    def send(self, value): print(json.dumps(value), flush=True)
    def close(self): pass


if __name__ == '__main__':
    _worker(os.environ['VERIFIER_TEST_PG_DSN'], sys.argv[1], Channel(), sys.argv[2])
