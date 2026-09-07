from services.fundamental_execution_guard import (
    OPPOSING_BIAS_REASON,
    validate_fundamental_entry,
)


def _insight(direction="NEUTRAL", status="ACTIVE", score=0.0, confidence=70.0):
    return {
        "generated_at": "2026-09-07T15:00:00Z",
        "overall_bias": {
            "direction": direction,
            "status": status,
            "pair_score": score,
            "confidence": confidence,
        },
        "data_quality": {
            "coverage_percent": 75.0,
            "status": status,
        },
    }


def test_neutral_fundamentals_do_not_block_entry():
    result = validate_fundamental_entry(
        "EURUSD",
        "BUY",
        insight=_insight(direction="NEUTRAL", score=5.24),
    )

    assert result["ok"] is True
    assert result["reason"] is None
    assert result["details"]["fundamental_gate_state"] == "PASS_NEUTRAL"


def test_aligned_fundamentals_allow_entry():
    result = validate_fundamental_entry(
        "EURUSD",
        "BUY",
        insight=_insight(direction="BUY", score=31.0),
    )

    assert result["ok"] is True
    assert result["details"]["fundamental_gate_state"] == "PASS_ALIGNED"


def test_opposing_fundamentals_block_entry():
    result = validate_fundamental_entry(
        "EURUSD",
        "SELL",
        insight=_insight(direction="BUY", score=31.0),
    )

    assert result["ok"] is False
    assert result["reason"] == OPPOSING_BIAS_REASON
    assert result["details"]["fundamental_gate_state"] == "BLOCK_OPPOSITE"


def test_insufficient_fundamental_data_does_not_block_entry():
    result = validate_fundamental_entry(
        "EURUSD",
        "SELL",
        insight=_insight(direction="BUY", status="INSUFFICIENT_DATA", score=31.0),
    )

    assert result["ok"] is True
    assert result["details"]["fundamental_gate_state"] == "BYPASS_INSUFFICIENT_DATA"


def test_xauusd_uses_same_direction_filter():
    result = validate_fundamental_entry(
        "XAUUSD",
        "BUY",
        insight=_insight(direction="SELL", score=-28.0),
    )

    assert result["ok"] is False
    assert result["reason"] == OPPOSING_BIAS_REASON
