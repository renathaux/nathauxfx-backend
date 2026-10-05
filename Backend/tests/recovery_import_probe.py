"""Fresh-process import audit. Reports labels only, never configuration values."""
import importlib
import io
import json
import os
from pathlib import Path
import sys
import threading

import dotenv
import sqlalchemy.engine

root = Path(__file__).resolve().parents[1]
violations = []
stdout = sys.stdout
sys.stdout = io.StringIO()
sys.stderr = io.StringIO()


def block(label):
    frame = sys._getframe(1)
    origin = None
    while frame:
        filename = Path(frame.f_code.co_filename)
        if filename.is_absolute() and filename.is_relative_to(root) and filename != Path(__file__):
            origin = str(filename.relative_to(root)) + ':' + str(frame.f_lineno)
            break
        frame = frame.f_back
    violations.append(label + (':' + origin if origin else ''))
    raise RuntimeError('IMPORT_SIDE_EFFECT_BLOCKED')


def audit(event, args):
    if event in {'socket.connect', 'socket.connect_ex', 'subprocess.Popen', 'os.system'}:
        block(event)
    if event in {'os.mkdir', 'os.remove', 'os.rename', 'os.chmod', 'os.rmdir'}:
        block(event)
    if event == 'open':
        path, mode, flags = args
        if isinstance(path, (str, bytes)):
            path = os.fsdecode(path)
            if os.path.basename(path).startswith('.env'):
                block('dotenv-read')
            absolute = Path(path).absolute()
            if any(absolute.is_relative_to(root / name) for name in ('data', 'database', 'cache')):
                block('runtime-file-read:' + absolute.name)
        if (isinstance(mode, str) and any(c in mode for c in 'wax+')) or (isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT)):
            block('file-write')


dotenv.load_dotenv = lambda *a, **k: block('dotenv-hydration')
threading.Thread.start = lambda *a, **k: block('thread-start')
sqlalchemy.engine.Engine.connect = lambda *a, **k: block('database-connect')
sys.addaudithook(audit)
def trace_assembly(frame, event, arg):
    if event == 'call' and frame.f_code.co_name in {
        '_install_ctrader_sparse_trendbar_policy', '_install_paper_entry_shape_guard',
        'install_neon_lifecycle_observer_throttle', '_install_v2_shadow_observer_policy',
        'install_account_scoped_indicator_stream', 'install_monthly_history_window',
        'install_v3b_dashboard_state_middleware', 'assemble_runtime',
        '_register_v3b_runtime_startup_install',
    }:
        block('implicit-runtime-assembly')
sys.setprofile(trace_assembly)
error = None
try:
    factory = '--factory' in sys.argv
    for name in [name for name in sys.argv[1:] if name != '--factory']:
        importlib.import_module(name)
    if factory:
        sys.setprofile(None)  # Explicit assembly is allowed; IO traps remain.
        os.environ['PYTHON_DOTENV_DISABLED'] = '1'
        module = importlib.import_module('closed_market_bootstrap')
        app = module.create_app()
        assert module.create_app() is app
        assert sum(bool(getattr(handler, '_recovery_startup', False))
                   for handler in app.router.on_startup) == 1
except BaseException as exc:
    error = type(exc).__name__
stdout.write(json.dumps({'violations': violations, 'error_type': error}) + '\n')
raise SystemExit(1 if violations or error else 0)
