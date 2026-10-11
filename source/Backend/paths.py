"""Storage locations only; a path never grants recovery/checkpoint authority.

Roots are startup configuration. With neither set, native paths are unchanged.
Configure both for read-only application source. No files are copied/migrated.
"""
import os
from pathlib import Path
import tempfile


BASE_DIR = Path(__file__).resolve().parent
# Preserve the reviewed repository layout, but do not mistake '/' for source
# when application modules are installed directly at /app.
SOURCE_DIR = BASE_DIR.parent if BASE_DIR.name == 'Backend' else BASE_DIR


def _checked(path):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('RUNTIME_PATH_UNSAFE')
    for component in (*reversed(path.parents), path):
        if component.is_symlink():
            raise ValueError('RUNTIME_PATH_UNSAFE')
    return path


def _root(name, default):
    if name not in os.environ:
        return default
    path = _checked(os.environ[name])
    source = SOURCE_DIR
    if path.is_relative_to(source) or source.is_relative_to(path):
        raise ValueError('RUNTIME_PATH_UNSAFE')
    if path.exists() and (not path.is_dir() or path.stat().st_uid != os.getuid()):
        raise ValueError('RUNTIME_PATH_UNSAFE')
    return path


STATE_ROOT_CONFIGURED = 'FLOWSIGNAL_STATE_ROOT' in os.environ
CACHE_ROOT_CONFIGURED = 'FLOWSIGNAL_CACHE_ROOT' in os.environ
DATA_DIR = _root('FLOWSIGNAL_STATE_ROOT', BASE_DIR / 'data')
CACHE_DIR = _root('FLOWSIGNAL_CACHE_ROOT', BASE_DIR / 'cache')
if (STATE_ROOT_CONFIGURED or CACHE_ROOT_CONFIGURED) and (
        DATA_DIR.is_relative_to(CACHE_DIR) or CACHE_DIR.is_relative_to(DATA_DIR)):
    raise ValueError('RUNTIME_PATH_UNSAFE')
# SQLite remains a development fallback, never a replacement for hosted PostgreSQL.
DATABASE_DIR = CACHE_DIR / 'database' if CACHE_ROOT_CONFIGURED else BASE_DIR / 'database'
CANDLE_CACHE_DIR = CACHE_DIR / "candle_cache"


def _location(root, configured, name, override=None, legacy=None):
    value = os.environ.get(override) if override else None
    path = Path(value) if value is not None else (root / name if configured or legacy is None else Path(legacy))
    if configured:
        path = _checked(path)
        if not path.is_relative_to(root):
            raise ValueError('RUNTIME_PATH_UNSAFE')
    return path


def state_file(name, *, override=None):
    return _location(DATA_DIR, STATE_ROOT_CONFIGURED, name, override)


def cache_path(name, *, override=None, legacy=None):
    return _location(CACHE_DIR, CACHE_ROOT_CONFIGURED, name, override, legacy)


def scratch_parent(parent=None):
    """Preserve native tempfile selection; constrain explicit scratch in image mode."""
    if not CACHE_ROOT_CONFIGURED:
        return parent
    path = _checked(parent if parent is not None else CACHE_DIR / 'scratch')
    if not path.is_relative_to(CACHE_DIR):
        raise ValueError('RUNTIME_PATH_UNSAFE')
    provision_runtime_directory(path)
    return path


def temporary_lock(name):
    path = cache_path('locks/' + name, legacy=Path(tempfile.gettempdir()) / name)
    if CACHE_ROOT_CONFIGURED:
        provision_runtime_directory(path.parent)
    return path


def provision_runtime_directory(path):
    from startup_recovery.checkpoints import provision_directory
    provision_directory(path)


def assert_legacy_source_write_allowed():
    # The legacy .env credential mirror is not migrated or copied. Hosted
    # credentials remain server/DB managed; an existing source .env is read-only.
    if STATE_ROOT_CONFIGURED or CACHE_ROOT_CONFIGURED:
        raise ValueError('RUNTIME_LEGACY_SOURCE_WRITE_FORBIDDEN')


def ensure_runtime_dirs():
    for path in [DATA_DIR, DATABASE_DIR, CACHE_DIR, CANDLE_CACHE_DIR]:
        provision_runtime_directory(path)
