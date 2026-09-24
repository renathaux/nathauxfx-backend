"""One OS lease for heavy replay work across routes and API processes.

Linux/macOS flock ownership survives in a child inheriting the descriptor.
Close descriptors (do not explicitly LOCK_UN) so parent death cannot release
admission while a worker still owns its inherited copy. Single host only.
"""
from contextlib import contextmanager
import fcntl
import os


class HeavyReplayBusy(Exception):
    pass


@contextmanager
def heavy_replay_lease():
    path = os.environ.get('HEAVY_REPLAY_LOCK_PATH', '/tmp/nathauxfx-heavy-replay.lock')
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise HeavyReplayBusy('A heavy backtest is already running.') from exc
        yield fd
    finally:
        os.close(fd)


class HeavyReplayAdmission:
    """Hold the same lease through the legacy response's serialization/send."""
    PATHS = {'/strategy-simulator/run', '/strategy-lab/replay', '/strategy-simulator/manual-history'}

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope.get('method') != 'POST' or scope.get('path', '').rstrip('/') not in self.PATHS:
            return await self.app(scope, receive, send)
        lease = heavy_replay_lease()
        try:
            lease.__enter__()
        except HeavyReplayBusy:
            body = b'{"detail":"A heavy backtest is already running. Try again after it finishes."}'
            await send({'type':'http.response.start','status':429,'headers':[(b'content-type',b'application/json')]})
            await send({'type':'http.response.body','body':body})
            return
        try:
            await self.app(scope, receive, send)
        finally:
            lease.__exit__(None, None, None)
