"""Start the read-only spot feed only for the recovery-admitted fixed account.

Feed startup remains separate from strategy signal readiness, but must never
restore or switch accounts before recovery establishes account authority.
"""
from __future__ import annotations


_HANDLER_MARKER = "__flowsignal_ctrader_live_stream_startup__"


def start_ctrader_live_stream(api_module, restore_account):
    """Start only for the admitted, already-reconciled account.

    ``restore_account`` remains in the compatibility signature, but must not
    execute: recovery owns selection and never switches or refreshes accounts.
    """
    from startup_recovery.runtime import require_worker_admission
    from startup_recovery.types import RecoveryError
    try:
        require_worker_admission()
    except RecoveryError as exc:
        return {'ok': False, 'status': 'not_started', 'reason': exc.code}

    try:
        result = api_module.start_ctrader_live_price_stream()
    except Exception as exc:
        result = {"ok": False, "status": "not_started", "reason": str(exc)}
        print("CTRADER_LIVE_STREAM_START_ERROR =", str(exc))
    else:
        print("CTRADER_LIVE_STREAM_START =", result)
    return result


def install_ctrader_live_stream_startup(
    app,
    api_module,
    strategy_startup,
    restore_account,
):
    """Insert the read-only feed startup immediately before strategy startup."""
    handlers = app.router.on_startup
    for existing in handlers:
        if getattr(existing, _HANDLER_MARKER, False):
            return existing

    def _start_live_stream():
        return start_ctrader_live_stream(api_module, restore_account)

    setattr(_start_live_stream, _HANDLER_MARKER, True)
    try:
        index = handlers.index(strategy_startup)
    except ValueError:
        index = len(handlers)
    handlers.insert(index, _start_live_stream)
    return _start_live_stream
