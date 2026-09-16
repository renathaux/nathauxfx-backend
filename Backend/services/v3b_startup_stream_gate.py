"""Startup readiness policy for the frozen 5m V3B model.

Failures in chart/legacy timeframes stay visible, but cannot masquerade as a
V3B 5m execution dependency. Neither this policy nor its caller places orders.
"""


def startup_stream_gate(initialize, *, v3b_enabled):
    initialized = []
    failures = []
    for symbol in ("EURUSD", "XAUUSD"):
        for timeframe in ("5m", "15m", "1h"):
            try:
                initialized.append(initialize(symbol, timeframe))
            except Exception as exc:
                failures.append({
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "reason": str(exc),
                })

    authoritative_failures = [item for item in failures if item["timeframe"] == "5m"]
    blocking = authoritative_failures if v3b_enabled else failures
    result = {
        "ready": not blocking,
        "streams_ready": len(initialized),
        "v3b_5m_ready": not authoritative_failures,
        "ancillary_failures": [item for item in failures if item["timeframe"] != "5m"],
    }
    if blocking:
        first = blocking[0]
        result["reason"] = f"{first['symbol']} {first['timeframe']}: {first['reason']}"
    return result
