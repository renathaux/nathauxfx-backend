"""Start the read-only cTrader spot feed independently of strategy readiness.

The strategy bootstrap intentionally fails closed when authoritative indicator
streams need reconciliation. Live spot prices are read-only market data and
must start before that fence so broker/feed status can recover while analysis
and execution remain blocked.
"""
from __future__ import annotations


_HANDLER_MARKER = "__flowsignal_ctrader_live_stream_startup__"


def start_ctrader_live_stream(api_module, restore_account):
    """Restore the selected account, then start the existing read-only feed."""
    try:
        restore_account()
    except Exception as exc:
        print("CTRADER_LIVE_STREAM_ACCOUNT_RESTORE_ERROR =", str(exc))

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
