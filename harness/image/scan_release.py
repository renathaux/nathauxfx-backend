"""Static scan with exact frozen synthetic-fixture exceptions, never live checks."""
import json
from pathlib import Path
import sys
import credential_scan
import package


def scan(root):
    root=Path(root)
    package.validate_source(root,built=(root/'build-record.json').exists())
    result=credential_scan.scan(root)
    # These two non-credential sentinel strings are reviewed, immutable bytes in
    # the pinned mounted-launcher test, not a path-only exception for new inputs.
    known={'path':'mounted_launcher_test.py','line':74,'kind':'CREDENTIAL_LITERAL'}
    accepted=[f for f in result['findings'] if f==known]
    assert len(accepted)==2, 'SYNTHETIC_FIXTURE_REVIEW_MISMATCH'
    result['findings']=[f for f in result['findings'] if f!=known]
    result['blocking_matches']=len(result['findings'])
    result['reviewed_pinned_maintenance_sentinels']=2
    assert not result['blocking_matches'], 'UNREVIEWED_CREDENTIAL_MATCH'
    return result


if __name__=='__main__': print(json.dumps(scan(sys.argv[1]),sort_keys=True))
