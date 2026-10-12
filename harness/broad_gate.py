"""Disposable broad-run comparison. Never change release source to repair tests."""
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from verify_source import verify, SOURCE


def normalize(message):
    # Only nondeterministic addresses, UUIDs and disposable paths are normalized.
    message = re.sub(r'0x[0-9a-fA-F]+', '<address>', message)
    message = re.sub(r'\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b', '<uuid>', message)
    return re.sub(r'/(?:var/tmp|tmp)/[^\s\'"\]]+', '<temporary-path>', message)


def cases(path):
    result=[]
    for case in ET.parse(path).iter('testcase'):
        for status in ('failure', 'error', 'skipped'):
            item=case.find(status)
            if item is not None:
                result.append(dict(node=case.get('classname')+'::'+case.get('name'),
                    status=status, message=normalize((item.get('message') or '').splitlines()[0]),
                    subsystem=case.get('classname').split('.')[1]))
    return sorted(result, key=lambda x:x['node'])


def run(root, evidence):
    assert platform.system()=='Linux' and platform.machine()=='x86_64'
    assert os.getuid()==501 and os.getgid()==1000
    harness=Path(__file__).resolve().parent
    before=verify(root)
    overlay=json.loads((harness/'broad-inputs.json').read_bytes())
    assert overlay['source']==SOURCE
    baseline=json.loads((harness/'broad-baseline.json').read_bytes())['cases']
    for row in baseline:row['message']=normalize(row['message'])
    baseline=sorted(baseline,key=lambda x:x['node'])
    with tempfile.TemporaryDirectory(prefix='broad-only-',dir=root.parent) as temporary:
        test_root=Path(temporary)/'source'
        shutil.copytree(root,test_root)
        for record in overlay['files']:
            path=Path(record['path'])
            assert not path.is_absolute() and '..' not in path.parts
            assert str(path).startswith(('Backend/tests/','Backend/scripts/','Backend/benchmarks/'))
            raw=(harness/'broad-inputs'/path).read_bytes()
            assert len(raw)==record['size'] and hashlib.sha256(raw).hexdigest()==record['sha256']
            dest=test_root/path
            assert not dest.exists()
            dest.parent.mkdir(parents=True,exist_ok=True)
            dest.write_bytes(raw)
        # Same disposable-only Git listing setup as the documented local broad
        # run. This is NOT the sanitized source or an image build context.
        subprocess.run(['/usr/bin/git','init','-q',str(test_root)],check=True)
        subprocess.run(['/usr/bin/git','-C',str(test_root),'add','--','*.py'],check=True)
        env=dict(os.environ)
        env.pop('DATABASE_URL',None)
        env.pop('MIGRATION_DATABASE_URL',None)
        command=[sys.executable,'-B','-m','pytest','-q','-ra','-p','no:cacheprovider',
            '--continue-on-collection-errors','--tb=short','--junitxml='+str(evidence/'broad.xml')]
        with (evidence/'broad.log').open('w') as log:
            result=subprocess.run(command,cwd=test_root/'Backend',env=env,stdout=log,stderr=subprocess.STDOUT)
        actual=cases(evidence/'broad.xml')
        expected_by={r['node']:r for r in baseline}
        actual_by={r['node']:r for r in actual}
        comparison=dict(expected=baseline,actual=actual,
            new=[n for n in actual_by if n not in expected_by],
            missing=[n for n in expected_by if n not in actual_by],
            changed=[n for n in actual_by.keys() & expected_by.keys() if actual_by[n]!=expected_by[n]],
            process_returncode=result.returncode,source_before=before)
        comparison['pass']=actual==baseline and result.returncode==1
        (evidence/'broad-comparison.json').write_text(json.dumps(comparison,sort_keys=True,indent=2))
    assert verify(root)==before
    (evidence/'source-after.json').write_text(json.dumps(verify(root),sort_keys=True))
    assert comparison['pass'], 'BROAD_FINGERPRINT_CHANGED_STOP'


if __name__=='__main__':
    run(Path(sys.argv[1]).resolve(),Path(sys.argv[2]).resolve())
