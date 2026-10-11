"""Certification-only, invoked with --network none and a read-only filesystem."""
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0,'/app/Backend')
assert os.getuid()==501
from live_integrity.build_identity import capture
before=capture()
for path in ('/app/forbidden-write','/app/Backend/forbidden-write','/opt/flowsignal-release/forbidden-write'):
    try:Path(path).write_text('must fail')
    except (PermissionError,OSError):pass
    else:raise AssertionError('SOURCE_OR_ANCHOR_WRITABLE')
import paths
paths.ensure_runtime_dirs()
assert paths.DATA_DIR==Path('/state') and paths.CACHE_DIR==Path('/cache')
assert list(paths.DATA_DIR.iterdir())==[]
from closed_market_bootstrap import create_app
app=create_app()
assert app is not None
from startup_recovery.checkpoint_store import write_runtime
from startup_recovery.types import RecoveryError
try:write_runtime(paths.DATA_DIR/'paper_backup.json','paper_backup',{})
except RecoveryError as error:assert str(error)=='CHECKPOINT_PRODUCER_NOT_ADMITTED'
else:raise AssertionError('UNADMITTED_PUBLICATION')
assert list(paths.DATA_DIR.iterdir())==[]
from startup_recovery.bootstrap import start
engine=Mock();engine.connect.side_effect=RuntimeError('NO_EXTERNAL_DATABASE')
factory=Mock(side_effect=AssertionError('NO_ADMISSION_ALLOWED'))
server=SimpleNamespace(ENGINE_RUNTIME_STATE={})
outcome=start(server,engine=engine,session_factory=Mock(),dependencies_factory=factory)
assert not outcome.ready and not outcome.entries_ready and not outcome.management_ready
factory.assert_not_called()
from startup_recovery.cutover_validation import validate_generation
from startup_recovery.cutover_manifest import CutoverError
try:validate_generation(b'{}',{},'0'*64,None)
except CutoverError:pass
else:raise AssertionError('UNVERIFIED_CUTOVER_GENERATION_ACCEPTED')
assert capture()==before
print(json.dumps(dict(application_import=True,source_readonly=True,entries_ready=False,
    management_ready=False,unadmitted_checkpoint_blocked=True,cutover_invalid_generation_blocked=True,
    build_identity=before['build_identity'],broker_calls=0)))
