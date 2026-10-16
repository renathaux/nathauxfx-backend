"""Preloaded, single-use isolated children; request work stays bounded to 10s."""
import atexit
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import time
from threading import Lock, Timer

_LOCAL_FLIGHT = Lock()
TIMEOUT_SECONDS = 10
_SLOT_LOCK = Lock()
_idle = None
_ready = False
_started = 0.0
_enabled = False
_timer = None
PRELOAD_TIMEOUT_SECONDS = 60


def _discard(process):
    if process is not None:
        if process.poll() is None:
            process.kill()
        process.communicate()


def _spawn():
    global _idle, _ready, _started, _timer
    _idle = subprocess.Popen([sys.executable, '-I', '-B',
        str(Path(__file__).with_name('worker.py')), '--preload'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    _ready = False
    _started = time.monotonic()
    if _timer is not None:
        _timer.cancel()
    _timer = Timer(PRELOAD_TIMEOUT_SECONDS, _expire, args=(_idle,))
    _timer.daemon = True
    _timer.start()


def _expire(process):
    with _SLOT_LOCK:
        if _idle is process:
            _check_ready()  # Also reaps failed/hung replacement imports.


def _check_ready(wait=0):
    global _idle, _ready
    if _idle is None:
        return False
    if _idle.poll() is not None:
        _discard(_idle)
        _idle = None
        return False
    if not _ready:
        with selectors.DefaultSelector() as selector:
            selector.register(_idle.stdout, selectors.EVENT_READ)
            if not selector.select(wait):
                if time.monotonic()-_started >= PRELOAD_TIMEOUT_SECONDS:
                    _discard(_idle)
                    _idle = None
                return False
        # A fixed-size handshake, never arbitrary child text or credentials.
        _ready = os.read(_idle.stdout.fileno(), 6) == b'READY\n'
        if not _ready:
            _discard(_idle)
            _idle = None
    return _ready


def preload():
    """Startup only: imports in a clean child, no request data or I/O work."""
    global _enabled
    with _SLOT_LOCK:
        _enabled = os.getenv('STRATEGY_LIVE_INTEGRITY_VERIFY_ENABLED') == '1'
        if not _enabled:
            return False
        try:
            if _idle is None:
                _spawn()
            return _check_ready(max(0, PRELOAD_TIMEOUT_SECONDS-(time.monotonic()-_started)))
        except Exception:
            return False


def shutdown():
    """Reap idle code-only child; active requests retain their kill deadline."""
    global _enabled, _idle, _ready
    with _SLOT_LOCK:
        _enabled = False
        if _timer is not None:
            _timer.cancel()
        _discard(_idle)
        _idle, _ready = None, False


def _take():
    global _idle, _ready
    with _SLOT_LOCK:
        if not _enabled or not _check_ready():
            return None
        process = _idle
        if _timer is not None:
            _timer.cancel()
        _idle, _ready = None, False
        return process


def _replenish():
    with _SLOT_LOCK:
        if _enabled and _idle is None:
            try:
                _spawn()  # No wait; imports are outside the completed request.
            except Exception:
                pass


atexit.register(shutdown)


def run(payload):
    if not _LOCAL_FLIGHT.acquire(blocking=False):
        return dict(status=409,body=dict(decision='WOULD_BLOCK',REAL_ORDER_DISPATCH_AVAILABLE=False,block_reasons=['DIAGNOSTIC_BUSY']))
    process = None
    try:
        deadline=min(float(payload.get('deadline',time.monotonic()+TIMEOUT_SECONDS)),time.monotonic()+TIMEOUT_SECONDS)
        payload = dict(payload,deadline=deadline)
        if time.monotonic()>=deadline:
            return dict(status=504,body=dict(decision='WOULD_BLOCK',REAL_ORDER_DISPATCH_AVAILABLE=False,block_reasons=['DIAGNOSTIC_TIMEOUT']))
        process = _take()
        if process is None:
            raise ValueError()
        try:
            stdout,_ = process.communicate(json.dumps(payload).encode()+b'\n',timeout=max(0,deadline-time.monotonic()))
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            return dict(status=504,body=dict(decision='WOULD_BLOCK',REAL_ORDER_DISPATCH_AVAILABLE=False,block_reasons=['DIAGNOSTIC_TIMEOUT']))
        if process.returncode or len(stdout)>65536:
            raise ValueError()
        result=json.loads(stdout)
        if result['body']['REAL_ORDER_DISPATCH_AVAILABLE'] is not False:
            raise ValueError()
        return result
    except Exception:
        return dict(status=503,body=dict(decision='WOULD_BLOCK',REAL_ORDER_DISPATCH_AVAILABLE=False,block_reasons=['DIAGNOSTIC_UNAVAILABLE']))
    finally:
        if process is not None and process.poll() is None:
            process.kill(); process.communicate()
        _replenish()
        _LOCAL_FLIGHT.release()
