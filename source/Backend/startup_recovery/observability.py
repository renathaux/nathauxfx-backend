"""Process-local observations, never capabilities. No database or broker imports.

Staged transaction facts are private until the caller observes commit success.
The recorder is best-effort: its failure must never affect recovery control flow.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime, timezone
import os
from pathlib import Path
import re
from threading import Lock
from uuid import UUID

_current = ContextVar('startup_identity_observer', default=None)
UNKNOWN = 'UNKNOWN'


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def _uuid(value):
    try:
        return str(UUID(value)) if isinstance(value, str) else None
    except ValueError:
        return None


def _epoch(value):
    return value if type(value) is int and value >= 0 else None


def _code(value):
    return value if isinstance(value, str) and re.fullmatch('[A-Z][A-Z0-9_]{0,95}', value) else UNKNOWN


def _time(value):
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(timezone.utc).isoformat()
    return None


def _process():
    # Linux kernel birth evidence, not an approximation from import/startup time.
    # Unsupported OS or unreadable procfs leaves start time explicitly unknown.
    started = None
    try:
        ticks = int(Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()[19])
        boot = next(int(line.split()[1]) for line in Path('/proc/stat').read_text().splitlines()
                    if line.startswith('btime '))
        started = datetime.fromtimestamp(boot + ticks / os.sysconf('SC_CLK_TCK'), timezone.utc).isoformat()
    except (OSError, ValueError, IndexError, StopIteration):
        pass
    instance = os.environ.get('RENDER_INSTANCE_ID')
    if not isinstance(instance, str) or not re.fullmatch('[A-Za-z0-9_-]{1,128}', instance):
        instance = None
    return dict(pid=os.getpid(), started_at=started, render_instance_id=instance)


class StartupRecorder:
    def __init__(self):
        self._lock = Lock()
        self._process = _process()
        self._startup = dict(boot_id=None, build={}, attempt_id=None, allocated_epoch=None,
            attempt_created_at=None, submission_reconciliation=UNKNOWN,
            acquisition_attempted=UNKNOWN, acquisition_result=UNKNOWN,
            token_installed=UNKNOWN, checkpoint_producer_admitted=UNKNOWN,
            ready=UNKNOWN, reason=UNKNOWN, phases=[], finalized=False, finished_at=None)
        self._owner = dict(attempt_id=None, epoch=None, observed_at=None)
        self._staged = None
        self._acquisition_validated = False

    def record(self, event, **fields):
        if self._process['pid'] != os.getpid():
            return
        with self._lock:
            if self._process['pid'] != os.getpid() or self._startup['finalized']:
                return
            s = self._startup
            if event == 'build':
                build = fields.get('build')
                if type(build) is dict:
                    for key, size in (('backend_git_sha', 40), ('source_tree', 40),
                                      ('packaging_source_sha', 40), ('build_identity', 64),
                                      ('source_manifest_sha256', 64)):
                        value = build.get(key)
                        if isinstance(value, str) and re.fullmatch('[0-9a-f]{%d}' % size, value):
                            s['build'][key] = value
            elif event == 'boot':
                s['boot_id'] = _uuid(fields.get('boot_id'))
            elif event == 'attempt_staged':
                self._staged = dict(attempt_id=_uuid(fields.get('attempt_id')),
                    allocated_epoch=_epoch(fields.get('epoch')),
                    attempt_created_at=_time(fields.get('created_at')))
            elif event == 'attempt_committed' and self._staged is not None:
                s.update(self._staged)
                self._staged = None
            elif event == 'owner_observed':
                self._owner = dict(attempt_id=_uuid(fields.get('attempt_id')),
                    epoch=_epoch(fields.get('epoch')), observed_at=timestamp())
            elif event == 'submission_reconciled':
                value = fields.get('ok')
                s['submission_reconciliation'] = value if type(value) is bool else UNKNOWN
            elif event == 'acquisition_started':
                s['acquisition_attempted'] = True
            elif event == 'acquisition_validated':
                self._acquisition_validated = True
            elif event == 'acquisition_committed':
                s['acquisition_result'] = 'COMMITTED'
            elif event == 'acquisition_rejected':
                if s['acquisition_result'] != 'COMMITTED':
                    s['acquisition_result'] = 'REJECTED'
            elif event == 'token_installed':
                s['token_installed'] = True
            elif event == 'producer_admitted':
                s['checkpoint_producer_admitted'] = True
            elif event == 'finished':
                s['ready'] = fields.get('ready') if type(fields.get('ready')) is bool else UNKNOWN
                s['reason'] = None if fields.get('reason') is None else _code(fields.get('reason'))
                s['phases'] = [_code(phase) for phase in fields.get('phases', ())]
                s['finished_at'], s['finalized'] = timestamp(), True
                self._staged = None

    def snapshot(self):
        if self._process['pid'] != os.getpid():
            # Do not touch a lock inherited from another process/thread.
            return StartupRecorder().snapshot()
        with self._lock:
            return deepcopy(dict(process=self._process, startup=self._startup,
                                 observed_durable_owner=self._owner))


@contextmanager
def observing(recorder):
    token = _current.set(recorder)
    try:
        yield
    finally:
        _current.reset(token)


def note(event, **fields):
    try:
        recorder = _current.get()
        if recorder is not None:
            recorder.record(event, **fields)
    except Exception:
        # Deliberately no logging: recorder inputs/errors must not leak secrets.
        pass


def attach(api):
    try:
        recorder = StartupRecorder()
        api._runtime_identity = recorder
        return recorder
    except Exception:
        return None
