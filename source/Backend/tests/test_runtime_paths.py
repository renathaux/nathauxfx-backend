"""Path-only tests: no broker transport, real application imports in children."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

BACKEND = Path(__file__).resolve().parents[1]


def child(code, roots=None, *, source=BACKEND):
    env = {k: v for k, v in os.environ.items() if k not in {
        'FLOWSIGNAL_STATE_ROOT', 'FLOWSIGNAL_CACHE_ROOT', 'NEWS_TRADING_STATE_FILE',
        'NEWS_TRADING_AUDIT_FILE', 'SIMULATOR_HISTORY_CACHE_DIR', 'SIMULATOR_FAST_JOB_DIR',
        'HEAVY_REPLAY_LOCK_PATH'}}
    env.update(PYTHONDONTWRITEBYTECODE='1', PYTHON_DOTENV_DISABLED='1', DATABASE_URL='sqlite:///:memory:')
    env.update(roots or {})
    return subprocess.run([sys.executable, '-B', '-c', code], cwd=source,
        env=env, text=True, capture_output=True, timeout=30)


def roots(tmp_path):
    return dict(FLOWSIGNAL_STATE_ROOT=str(tmp_path / 'state'),
                FLOWSIGNAL_CACHE_ROOT=str(tmp_path / 'cache'))


def test_defaults_preserve_native_path_names():
    result = child("import paths; assert paths.DATA_DIR == paths.BASE_DIR/'data'; assert paths.CACHE_DIR == paths.BASE_DIR/'cache'; assert paths.DATABASE_DIR == paths.BASE_DIR/'database'")
    assert result.returncode == 0, result.stderr


@pytest.fixture
def readonly_source(tmp_path):
    """Real permission denial, not a mocked writer. Never chmod the working tree."""
    import shutil
    source = tmp_path/'application'/'Backend'
    names = subprocess.check_output(['git', 'ls-files', '*.py'], cwd=BACKEND, text=True).splitlines()
    for name in names:
        target = source/name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(BACKEND/name, target)
    for path in source.rglob('*'):
        path.chmod(0o555 if path.is_dir() else 0o444)
    source.chmod(0o555)
    try:
        yield source
    finally:
        source.chmod(0o755)
        for path in source.rglob('*'):
            path.chmod(0o755 if path.is_dir() else 0o644)


def test_readonly_source_initialization_and_external_cache_writes(tmp_path, readonly_source):
    result = child("""
from pathlib import Path
import os, paths
assert os.getuid() != 0, 'Read-only proof must run without root bypass'
try:
    (paths.BASE_DIR/'write-must-fail').write_text('denied')
except PermissionError:
    pass
else:
    raise AssertionError('Application source is writable')
# Record even failed/swallowed source writes after the explicit denial probe.
import sys
source_writes = []
def located(value, dir_fd=None):
    path = Path(os.fsdecode(value))
    if path.is_absolute() or dir_fd is None or dir_fd == -1:
        return path.absolute()
    if sys.platform == 'darwin':
        import fcntl
        directory = os.fsdecode(fcntl.fcntl(dir_fd, fcntl.F_GETPATH, bytes(1024)).split(b'\\0',1)[0])
    else:
        directory = os.readlink('/proc/self/fd/' + str(dir_fd))
    return Path(directory)/path
def audit(event, args):
    candidates = []
    if event == 'open' and isinstance(args[0], (str, bytes)):
        mode, flags = args[1], args[2]
        if (mode and any(c in mode for c in 'wax+')) or flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC):
            candidates = [(args[0], None)]
    elif event in {'os.mkdir','os.remove','os.rmdir','os.chmod','os.rename'}:
        if event == 'os.rename':
            candidates = [(args[0], args[2]), (args[1], args[3])]
        else:
            candidates = [(args[0], args[2] if event in {'os.mkdir','os.chmod'} else args[1])]
    for value, dir_fd in candidates:
        if isinstance(value, (str, bytes)) and located(value, dir_fd).is_relative_to(paths.BASE_DIR):
            source_writes.append(event)
sys.addaudithook(audit)
from closed_market_bootstrap import create_app
assert create_app() is not None
paths.ensure_runtime_dirs()
assert not list(paths.DATA_DIR.iterdir())
from startup_recovery.checkpoint_store import write_runtime
from startup_recovery.types import RecoveryError
try:
    write_runtime(paths.DATA_DIR/'paper_backup.json', 'paper_backup', {})
except RecoveryError as exc:
    assert str(exc) == 'CHECKPOINT_PRODUCER_NOT_ADMITTED'
else:
    raise AssertionError('Empty external root granted authority')
assert not list(paths.DATA_DIR.iterdir())
from services.news_trading import _audit, AUDIT_FILE
_audit({'decision':'path-test'})
assert AUDIT_FILE.parent == paths.DATA_DIR and AUDIT_FILE.is_file()
from services.strategy_fast_results import DiskResults
with DiskResults() as results:
    results.append({'test':'scratch-only'})
    assert results[0] == {'test':'scratch-only'}
    assert results.directory.is_relative_to(paths.CACHE_DIR)
from services.heavy_replay_admission import heavy_replay_lease
with heavy_replay_lease():
    assert (paths.CACHE_DIR/'locks/nathauxfx-heavy-replay.lock').is_file()
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from services.account_execution_coordination import account_lock, ExecutionFenced
factory = sessionmaker(create_engine('sqlite:///:memory:'))
with account_lock(factory, '123'):
    try:
        with account_lock(factory, '123'):
            raise AssertionError('Same account lock admitted twice')
    except ExecutionFenced:
        pass
assert list((paths.CACHE_DIR/'locks').glob('flowsignal-broker-test-*.lock'))
assert not list(paths.BASE_DIR.rglob('__pycache__'))
assert source_writes == [], source_writes
""", roots(tmp_path), source=readonly_source)
    assert result.returncode == 0, result.stderr


def test_external_mode_cannot_rewrite_legacy_source_env(tmp_path):
    result = child("""
import paths
try:
    paths.assert_legacy_source_write_allowed()
except ValueError as exc:
    assert str(exc) == 'RUNTIME_LEGACY_SOURCE_WRITE_FORBIDDEN'
else:
    raise AssertionError('Source credential mirror must remain read-only')
""", roots(tmp_path))
    assert result.returncode == 0, result.stderr
    target = tmp_path/'legacy.env'
    target.write_text('TEST_PLACEHOLDER=original\n')
    result = child(f'''
import ctrader_connector as connector
from pathlib import Path
connector.ENV_PATH = Path({str(target)!r})
try:
    connector.update_env_file_values({{'TEST_PLACEHOLDER':'changed'}})
except ValueError as exc:
    assert str(exc) == 'RUNTIME_LEGACY_SOURCE_WRITE_FORBIDDEN'
else:
    raise AssertionError('Legacy writer was not rejected')
''', roots(tmp_path))
    assert result.returncode == 0, result.stderr
    assert target.read_text() == 'TEST_PLACEHOLDER=original\n'


def test_nested_roots_and_scratch_escape_rejected(tmp_path):
    result = child('import paths', roots(tmp_path) | {'FLOWSIGNAL_CACHE_ROOT': str(tmp_path/'state'/'cache')})
    assert result.returncode != 0 and 'RUNTIME_PATH_UNSAFE' in result.stderr
    result = child("import paths; paths.scratch_parent(paths.BASE_DIR/'scratch')", roots(tmp_path))
    assert result.returncode != 0 and 'RUNTIME_PATH_UNSAFE' in result.stderr


@pytest.mark.parametrize('module,constructor', [('strategy_fast_cache', 'FactsCache'), ('strategy_fast_jobs', 'FastJobs')])
def test_explicit_cache_constructor_cannot_escape_configured_root(tmp_path, module, constructor):
    target = tmp_path/'outside-cache'
    result = child(f'from services.{module} import {constructor}; {constructor}({str(target)!r})', roots(tmp_path))
    assert result.returncode != 0 and 'RUNTIME_PATH_UNSAFE' in result.stderr
    assert not target.exists()


def test_configured_roots_provision_directories_not_state(tmp_path):
    result = child("""
import os, paths
from pathlib import Path
assert paths.DATA_DIR == Path(os.environ['FLOWSIGNAL_STATE_ROOT'])
assert paths.CACHE_DIR == Path(os.environ['FLOWSIGNAL_CACHE_ROOT'])
assert paths.DATABASE_DIR == paths.CACHE_DIR/'database'
paths.ensure_runtime_dirs()
assert list(paths.DATA_DIR.iterdir()) == []
assert all(p.is_dir() for p in paths.CACHE_DIR.rglob('*'))
""", roots(tmp_path))
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('value', ['relative/state', '', '/private/tmp/../escape'])
def test_explicit_invalid_root_fails_closed(value):
    result = child('import paths', dict(FLOWSIGNAL_STATE_ROOT=value))
    assert result.returncode != 0
    assert 'RUNTIME_PATH_UNSAFE' in result.stderr


def test_source_root_rejected():
    result = child('import paths', dict(FLOWSIGNAL_STATE_ROOT=str(BACKEND / 'new-state')))
    assert result.returncode != 0
    assert 'RUNTIME_PATH_UNSAFE' in result.stderr


def test_flattened_application_layout_allows_external_roots(tmp_path):
    import shutil
    source = tmp_path/'app'
    source.mkdir()
    shutil.copyfile(BACKEND/'paths.py', source/'paths.py')
    result = child('import paths; assert paths.DATA_DIR != paths.BASE_DIR', roots(tmp_path), source=source)
    assert result.returncode == 0, result.stderr
    result = child('import paths', roots(tmp_path) | {'FLOWSIGNAL_STATE_ROOT':str(source/'state')}, source=source)
    assert result.returncode != 0 and 'RUNTIME_PATH_UNSAFE' in result.stderr


def test_symlink_parent_rejected_without_resolution(tmp_path):
    actual = tmp_path/'actual'; actual.mkdir()
    link = tmp_path/'link'; link.symlink_to(actual, target_is_directory=True)
    result = child('import paths', dict(FLOWSIGNAL_STATE_ROOT=str(link/'state')))
    assert result.returncode != 0
    assert 'RUNTIME_PATH_UNSAFE' in result.stderr
    assert not (actual/'state').exists()


@pytest.mark.parametrize('override', ['NEWS_TRADING_STATE_FILE', 'NEWS_TRADING_AUDIT_FILE',
    'SIMULATOR_HISTORY_CACHE_DIR', 'SIMULATOR_FAST_JOB_DIR', 'HEAVY_REPLAY_LOCK_PATH'])
def test_legacy_override_cannot_escape_configured_roots(tmp_path, override):
    config = roots(tmp_path) | {override: str(BACKEND/'escaped.json')}
    result = child("""
from services import news_trading, strategy_fast_history, strategy_fast_jobs, heavy_replay_admission
strategy_fast_history._disk_lock().__enter__()
strategy_fast_jobs.manager()
heavy_replay_admission.heavy_replay_lease().__enter__()
""", config)
    assert result.returncode != 0
    assert 'RUNTIME_PATH_UNSAFE' in result.stderr
    assert not (BACKEND/'escaped.json').exists()


def test_all_consumer_bindings_and_scratch_use_external_roots(tmp_path):
    result = child("""
import paths
paths.ensure_runtime_dirs()
import api
from startup_recovery.server_adapter import state_paths
from services import news_trading, strategy_fast_history, strategy_fast_jobs
from services.strategy_fast_results import DiskResults
assert len(state_paths(api)) == 11
assert all(p.parent == paths.DATA_DIR for p in state_paths(api).values())
assert news_trading.AUDIT_FILE.parent == paths.DATA_DIR
with strategy_fast_history._disk_lock() as folder:
    assert folder.is_relative_to(paths.CACHE_DIR)
assert strategy_fast_jobs.manager().root.is_relative_to(paths.CACHE_DIR)
with DiskResults() as results:
    assert results.directory.is_relative_to(paths.CACHE_DIR)
assert not list(paths.DATA_DIR.iterdir())
""", roots(tmp_path))
    assert result.returncode == 0, result.stderr
