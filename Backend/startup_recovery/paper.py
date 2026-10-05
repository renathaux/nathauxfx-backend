"""Paper-only admission around assembled callbacks, independent of LIVE policy.

Unavailable paper state cannot be evaluated, reset or repaired as empty. These
callbacks have no required return value in the panel path, so skipping paper
does not skip unrelated LIVE evaluation. The visible health is not authority.
"""
from functools import wraps
from startup_recovery.checkpoint_store import read_runtime
from startup_recovery.types import RecoveryError


def install(shared):
    if getattr(shared, '_RECOVERY_PAPER_GUARDS_INSTALLED', False):
        return
    shared.PAPER_RECOVERY_STATE = {'ready': False, 'reason': 'PAPER_RECOVERY_NOT_ADMITTED'}

    def guarded(callback):
        @wraps(callback)
        def invoke(*args, **kwargs):
            try:
                # This requires the producer's fixed epoch and current durable
                # owner, not a process-wide ready Boolean or raw legacy file.
                read_runtime(shared.PAPER_BACKUP_FILE, 'paper_backup')
            except RecoveryError as exc:
                shared.PAPER_RECOVERY_STATE = {'ready': False, 'reason': exc.code}
                return None
            shared.PAPER_RECOVERY_STATE = {'ready': True, 'reason': None}
            return callback(*args, **kwargs)
        return invoke

    for name in ('update_paper_trade', 'run_weekly_paper_reset', 'run_monthly_paper_reset'):
        setattr(shared, name, guarded(getattr(shared, name)))
    shared._RECOVERY_PAPER_GUARDS_INSTALLED = True
