from __future__ import annotations

from fundamentals.gold_insight_service import get_xauusd_fundamental_insight
from fundamentals.insight_cache import get_or_calculate
from fundamentals.insight_service import get_fundamental_insight


SUPPORTED_SYMBOLS = {"EURUSD", "XAUUSD"}
EXECUTION_CACHE_TTL_SECONDS = 120
OPPOSING_BIAS_REASON = "WAIT_FUNDAMENTAL_BIAS_OPPOSES_ENTRY"


def _normalize_symbol(symbol):
    return str(symbol or "").upper().replace("/", "")


def _load_insight(symbol):
    normalized = _normalize_symbol(symbol)

    def calculate():
        if normalized == "XAUUSD":
            return get_xauusd_fundamental_insight()
        return get_fundamental_insight(normalized, persist=False)

    return get_or_calculate(
        normalized,
        calculate,
        ttl_seconds=EXECUTION_CACHE_TTL_SECONDS,
    )


def validate_fundamental_entry(symbol, side, *, insight=None):
    """Use the existing macro engine as a final strategy-entry filter.

    Policy:
    - ACTIVE BUY/SELL aligned with the technical side: pass.
    - ACTIVE BUY/SELL opposite the technical side: block.
    - NEUTRAL or insufficient/unavailable fundamentals: do not block.

    The fundamental engines already require minimum factor coverage and a
    +/-20 directional threshold before they emit BUY or SELL, so this guard
    does not invent a second directional threshold.
    """
    normalized = _normalize_symbol(symbol)
    normalized_side = str(side or "").upper()
    details = {
        "symbol": normalized,
        "side": normalized_side,
        "fundamental_execution_connected": True,
        "fundamental_policy": "ACTIVE_OPPOSITE_BLOCK_NEUTRAL_PASS",
    }

    if normalized not in SUPPORTED_SYMBOLS:
        details["fundamental_gate_state"] = "BYPASS_UNSUPPORTED_SYMBOL"
        return {"ok": True, "reason": None, "details": details}

    if normalized_side not in {"BUY", "SELL"}:
        details["fundamental_gate_state"] = "INVALID_SIDE"
        return {
            "ok": False,
            "reason": "WAIT_FUNDAMENTAL_GATE_INVALID_SIDE",
            "details": details,
        }

    try:
        current = insight if insight is not None else _load_insight(normalized)
    except Exception as exc:
        # Fundamental data is a directional filter, not a kill switch for a
        # temporary database/provider problem. Fail open and make the bypass
        # explicit in execution diagnostics.
        details.update({
            "fundamental_gate_state": "BYPASS_UNAVAILABLE",
            "fundamental_error": str(exc),
        })
        return {"ok": True, "reason": None, "details": details}

    overall = (current or {}).get("overall_bias") or {}
    quality = (current or {}).get("data_quality") or {}
    direction = str(overall.get("direction") or "NEUTRAL").upper()
    status = str(overall.get("status") or quality.get("status") or "").upper()
    score = overall.get("pair_score", overall.get("score"))
    confidence = overall.get("confidence")
    coverage = quality.get("coverage_percent")

    details.update({
        "fundamental_direction": direction,
        "fundamental_status": status,
        "fundamental_score": score,
        "fundamental_confidence": confidence,
        "fundamental_coverage_percent": coverage,
        "fundamental_generated_at": (current or {}).get("generated_at"),
    })

    if status != "ACTIVE":
        details["fundamental_gate_state"] = "BYPASS_INSUFFICIENT_DATA"
        return {"ok": True, "reason": None, "details": details}

    if direction not in {"BUY", "SELL"}:
        details["fundamental_gate_state"] = "PASS_NEUTRAL"
        return {"ok": True, "reason": None, "details": details}

    if direction == normalized_side:
        details["fundamental_gate_state"] = "PASS_ALIGNED"
        return {"ok": True, "reason": None, "details": details}

    details["fundamental_gate_state"] = "BLOCK_OPPOSITE"
    return {
        "ok": False,
        "reason": OPPOSING_BIAS_REASON,
        "details": details,
    }
