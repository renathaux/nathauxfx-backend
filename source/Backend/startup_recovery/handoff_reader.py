"""Administrative-only read scheduling; no ownership or broker write capability.

Each observation joins independent DB and broker reads. Two observations remain
sequential at the caller. No evidence is cached and the locked reread remains
synchronous on the caller's transaction. This module never starts recovery.
"""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from threading import Lock
from time import monotonic

from startup_recovery.readers import DatabaseReader, BrokerReader
from startup_recovery.types import RecoveryError
from startup_recovery.unit_of_work import require_network_boundary


class ConcurrentHandoffReader:
    def __init__(self, engine, *, history_from):
        self._database = DatabaseReader(engine, history_from=history_from)
        self._history_from = history_from
        self._flight = Lock()

    def __call__(self, scope):
        require_network_boundary()
        if not self._flight.acquire(blocking=False):
            raise RecoveryError('RECOVERY_ACCOUNT_BUSY')
        try:
            deadline = monotonic() + 20
            credentials = self._database.credentials(scope)
            # Independent connection in the worker, never the caller's Session.
            # Broker remains on the caller thread; do not hide its lock context.
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix='handoff-read') as executor:
                pending = executor.submit(copy_context().run, self._database, scope)
                require_network_boundary()
                with BrokerReader(scope.account_id, scope.environment, credentials, deadline) as broker:
                    observation = broker.snapshot(self._history_from)
                database = pending.result()
            # Executor joins even on failure. Never enter ownership locks while
            # a worker remains active. Slow failure may finish after deadline,
            # but can never return successful stale evidence or mutate authority.
            if monotonic() > deadline:
                raise RecoveryError('RECOVERY_TERMINATION_EVIDENCE_INVALID')
            return dict(database=database, broker=observation)
        except RecoveryError:
            raise
        except Exception:
            # Driver errors may contain credentials; never propagate their text.
            raise RecoveryError('RECOVERY_TERMINATION_EVIDENCE_INVALID') from None
        finally:
            self._flight.release()

    def database(self, session, scope):
        return self._database.read_connection(session.connection(), scope)
