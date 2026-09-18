from __future__ import annotations

from fundamentals.gold_insight_service import get_xauusd_fundamental_insight
from fundamentals.insight_cache import get_or_calculate
from fundamentals.insight_service import get_fundamental_insight


SUPPORTED_SYMBOLS = {"EURUSD", "XAUUSD"}
EXECUTION_CACHE_TTL_SECONDS = 120

DEFAULT_FUNDAMENTAL_POLICY = "BLOCK_OPPOSITE"
FUNDAMENTAL_POLICIES = {"BLOCK_OPPOSITE", "REQUIRE_ALIGNMENT"}

OPPOSING_BIAS_REASON = "WAIT_FUNDAMENTAL_BIAS_OPPOSES_ENTRY"
ALIGNMENT_REQUIRED_REASON = "WAIT_FUNDAMENTAL_ALIGNMENT_REQUIRED"
ALIGNMENT_UNAVAILABLE_REASON = "WAIT_FUNDAMENTAL_ALIGNMENT_UNAVAILABLE"
INVALID_POLICY_REASON = "WAIT_FUNDAMENTAL_POLICY_INVALID"


def _normalize_symbol(symbol):
    return str(symbol or "").upper().replace("/", "")


def normalize_fundamental_policy(policy):
    value = str(policy or DEFAULT_FUNDAMENTAL_POLICY).strip().upper()
    return value if value in FUNDAMENTAL_POLICIES else None


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


def validate_fundamental_entry(symbol, side, *, insight=None, policy=DEFAULT_FUNDAMENTAL_POLICY):
    """Apply the shared LIVE fundamental-entry policy.

    BLOCK_OPPOSITE:
    - ACTIVE BUY/SELL aligned with the technical side: pass.
    - ACTIVE BUY/SELL opposite the technical side: block.
    - NEUTRAL, insufficient, or temporarily unavailable fundamentals: pass.

    REQUIRE_ALIGNMENT:
    - Only an ACTIVE BUY/SELL matching the technical side passes.
    - Opposite, NEUTRAL, insufficient, or unavailable data blocks.

    Both policies use the existing macro engine's own ACTIVE/coverage threshold;
    this guard does not invent a second score threshold.
    """
    normalized = _normalize_symbol(symbol)
    normalized_side = str(side or "").upper()
    normalized_policy = normalize_fundamental_policy(policy)
    details = {
        "symbol": normalized,
        "side": normalized_side,
        "fundamental_execution_connected": True,
        "fundamental_policy": normalized_policy or str(policy or ""),
    }

    if normalized_policy is None:
        details["fundamental_gate_state"] = "INVALID_POLICY"
        return {
            "ok": False,
            "reason": INVALID_POLICY_REASON,
            "details": details,
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
        details.update({
            "fundamental_gate_state": (
                "BLOCK_UNAVAILABLE"
                if normalized_policy == "REQUIRE_ALIGNMENT"
                else "BYPASS_UNAVAILABLE"
            ),
            "fundamental_error": str(exc),
        })
        if normalized_policy == "REQUIRE_ALIGNMENT":
            return {
                "ok": False,
                "reason": ALIGNMENT_UNAVAILABLE_REASON,
                "details": details,
            }
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
        if normalized_policy == "REQUIRE_ALIGNMENT":
            details["fundamental_gate_state"] = "BLOCK_ALIGNMENT_NOT_ACTIVE"
            return {
                "ok": False,
                "reason": ALIGNMENT_REQUIRED_REASON,
                "details": details,
            }
        details["fundamental_gate_state"] = "BYPASS_INSUFFICIENT_DATA"
        return {"ok": True, "reason": None, "details": details}

    if direction not in {"BUY", "SELL"}:
        if normalized_policy == "REQUIRE_ALIGNMENT":
            details["fundamental_gate_state"] = "BLOCK_ALIGNMENT_NEUTRAL"
            return {
                "ok": False,
                "reason": ALIGNMENT_REQUIRED_REASON,
                "details": details,
            }
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
