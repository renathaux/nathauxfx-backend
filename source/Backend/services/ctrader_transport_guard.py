"""Runtime fail-safe for cTrader read/auth requests.

This module is deliberately isolated from trading strategy and order execution.
It prevents a cTrader WebSocket read loop from blocking the FlowSignal backend
forever when the broker keeps sending frames that do not match the requested
response.
"""

import threading

import ctrader_connector as ctrader

_INSTALLED = False
_ORIGINAL_SEND = None

# Only read/auth/stream-setup requests are guarded here. Order placement,
# amendment and close payloads are intentionally excluded so this safety patch
# cannot create ambiguous trade execution by timing out after a broker action.
_GUARDED_PAYLOAD_TYPES = {
    2112,  # ProtoOAAssetListReq: read-only execution-metadata currency binding
    ctrader.PAYLOAD_APPLICATION_AUTH_REQ,
    ctrader.PAYLOAD_ACCOUNT_AUTH_REQ,
    ctrader.PAYLOAD_SYMBOLS_LIST_REQ,
    ctrader.PAYLOAD_SYMBOL_BY_ID_REQ,
    ctrader.PAYLOAD_TRADER_REQ,
    ctrader.PAYLOAD_RECONCILE_REQ,
    ctrader.PAYLOAD_SUBSCRIBE_SPOTS_REQ,
    ctrader.PAYLOAD_DEAL_LIST_REQ,
    ctrader.PAYLOAD_GET_TRENDBARS_REQ,
    ctrader.PAYLOAD_GET_ACCOUNT_LIST_BY_ACCESS_TOKEN_REQ,
    ctrader.PAYLOAD_GET_POSITION_UNREALIZED_PNL_REQ,
}


def _bounded_send(sock, payload_type, payload, expected_payload_type, timeout_seconds=12):
    if payload_type not in _GUARDED_PAYLOAD_TYPES:
        return _ORIGINAL_SEND(sock, payload_type, payload, expected_payload_type)

    result = {}
    done = threading.Event()

    def worker():
        try:
            result["value"] = _ORIGINAL_SEND(
                sock,
                payload_type,
                payload,
                expected_payload_type,
            )
        except BaseException as exc:  # preserve original transport exception
            result["error"] = exc
        finally:
            done.set()

    thread = threading.Thread(
        target=worker,
        name=f"ctrader-read-{payload_type}",
        daemon=True,
    )
    thread.start()

    if not done.wait(timeout_seconds):
        try:
            sock.shutdown(2)
        except Exception:
            pass
        try:
            sock.close()
        except Exception:
            pass
        print("CTRADER_REQUEST_DEADLINE_EXCEEDED =", {
            "payload_type": payload_type,
            "expected_payload_type": expected_payload_type,
            "timeout_seconds": timeout_seconds,
        })
        raise ctrader.CTraderApiError(
            f"cTrader request timed out after {timeout_seconds}s",
            {
                "payload_type": payload_type,
                "expected_payload_type": expected_payload_type,
                "timeout_seconds": timeout_seconds,
            },
        )

    if "error" in result:
        raise result["error"]
    return result.get("value")


def install_ctrader_transport_guard():
    global _INSTALLED, _ORIGINAL_SEND
    if _INSTALLED:
        return False

    current = ctrader.send_ctrader_request
    if getattr(current, "__flowsignal_deadline_guard__", False):
        _INSTALLED = True
        return False

    _ORIGINAL_SEND = current
    _bounded_send.__flowsignal_deadline_guard__ = True
    ctrader.send_ctrader_request = _bounded_send
    _INSTALLED = True
    print("CTRADER_TRANSPORT_GUARD_INSTALLED =", {
        "read_timeout_seconds": 12,
        "guarded_payload_types": sorted(_GUARDED_PAYLOAD_TYPES),
        "trade_payloads_untouched": True,
    })
    return True
