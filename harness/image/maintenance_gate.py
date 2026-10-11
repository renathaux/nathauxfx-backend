"""Use frozen maintenance checks against untagged, locally built image IDs."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

source=Path(sys.argv[1]).resolve()
spec=importlib.util.spec_from_file_location('reviewed_maintenance_certify',source/'certify.py')
mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
mod.BASE=sys.argv[2];mod.IMAGE=sys.argv[3];mod.E=Path(sys.argv[4]).resolve()
assert mod.BASE.startswith('sha256:') and mod.IMAGE.startswith('sha256:')
assert mod.filesystem(mod.BASE,'production')==mod.filesystem(mod.IMAGE,'maintenance'), 'PROTECTED_BYTES_CHANGED'
result=mod.tests()
# The reviewed eight mount tests report their actual unittest count.
log=(mod.E/'mounted-launcher-tests.log').read_text()
assert 'Ran 8 tests' in log and '\nOK' in log
unit=subprocess.run(['docker','run','--rm','--network','none','--user','501:1000',
    '-v',str(Path('maintenance-unit').resolve())+':/maintenance-unit:ro',
    '--entrypoint','/opt/venv/bin/python',mod.IMAGE,'-B','/maintenance-unit/tests/test_launcher.py'],
    capture_output=True,text=True)
(mod.E/'maintenance-unit-tests.log').write_text(unit.stdout+unit.stderr)
assert unit.returncode==0 and 'Ran 3 tests' in unit.stderr and '\nOK' in unit.stderr
result.update(maintenance_tests=11,protected_paths='IDENTICAL')
(mod.E/'maintenance-tests.json').write_text(json.dumps(result,sort_keys=True,indent=2))
