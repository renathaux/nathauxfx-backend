"""Lossless diagnostic records, not an approving certification comparator."""
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import xml.etree.ElementTree as ET

BLS_NODE = 'tests.test_official_backfill.OfficialBackfillTests::test_verified_manifest_includes_new_bls_releases'

def canonical(value):
    def validate(v):
        if v is None or type(v) in (str, bool, int):
            return
        if type(v) is float:
            if not math.isfinite(v):
                raise ValueError('NONFINITE_DIAGNOSTIC')
            return
        if type(v) is list:
            for item in v:
                validate(item)
            return
        if type(v) is dict:
            for key, item in v.items():
                if type(key) is not str:
                    raise TypeError('NONSTRING_DIAGNOSTIC_KEY')
                validate(item)
            return
        raise TypeError('NONPRIMITIVE_DIAGNOSTIC')
    validate(value)
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                      allow_nan=False).encode()

def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()

def classify(node):
    return 'OUTSIDE_RELEASE_SURFACE' if node == BLS_NODE else 'SUPPORTED_OR_UNEXPLAINED_PENDING_REVIEW'

def read_cases(path):
    rows = []
    for case in ET.parse(path).iter('testcase'):
        node = case.get('classname', '')+'::'+case.get('name', '')
        outcomes = [(kind, child) for kind in ('failure', 'error', 'skipped') for child in case.findall(kind)]
        if not outcomes:
            outcomes = [('passed', None)]
        for kind, child in outcomes:
            rows.append(dict(node=node, status=kind, boundary=classify(node),
                             message='' if child is None else child.get('message', ''),
                             body='' if child is None else ''.join(child.itertext())))
    return sorted(rows, key=lambda row: (row['node'], row['status'], row['message'], row['body']))

def sqlite_snapshot(path):
    path = Path(path)
    with sqlite3.connect(path.as_uri()+'?mode=ro', uri=True) as c:
        schema = [list(row) for row in c.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name')]
        integrity = c.execute('PRAGMA integrity_check').fetchone()[0]
    return dict(schema=schema, integrity=integrity, sqlite_version=sqlite3.sqlite_version,
                sha256=hashlib.sha256(path.read_bytes()).hexdigest())

def create_empty_sqlite(path):
    path = Path(path).resolve()
    with path.open('xb'):
        pass
    with sqlite3.connect(path) as c:
        c.execute('PRAGMA user_version=0')
    result = sqlite_snapshot(path)
    assert result['schema'] == [] and result['integrity'] == 'ok'
    return result
