from services.v3b_dashboard_state import enrich_dashboard_payload


def test_enrich_dashboard_payload_prefers_live_v3b_runtime_state():
    payload = {
        "EURUSD": {
            "signal": "WAIT",
            "block_reason": "WAIT_NO_FRESH_15M_SMC_BREAK",
            "blocked_reason": "WAIT_NO_FRESH_15M_SMC_BREAK",
        },
        "XAUUSD": {
            "signal": "WAIT",
            "block_reason": "WAIT_NO_FRESH_15M_SMC_BREAK",
            "blocked_reason": "WAIT_NO_FRESH_15M_SMC_BREAK",
        },
        "_meta": {
            "live_strategy_identity": "LIVE — V3B",
        },
    }
    statuses = {
        "EURUSD": {
            "status": "WAIT",
            "reason": "WAIT_V3B_PAPER_5M_BOS",
            "checked_at": 123.0,
            "details": {
                "source_candidate": {
                    "symbol": "EURUSD",
                    "live_strategy_model": "LIVE_V3B_M5_FROZEN",
                    "paper_entry_reason": "WAIT_V3B_PAPER_5M_BOS",
                }
            },
        },
        "XAUUSD": {
            "status": "WAIT",
            "reason": "WAIT_V3B_PAPER_5M_BOS",
            "checked_at": 124.0,
            "details": {
                "source_candidate": {
                    "symbol": "XAUUSD",
                    "live_strategy_model": "LIVE_V3B_M5_FROZEN",
                    "paper_entry_reason": "WAIT_V3B_PAPER_5M_BOS",
                }
            },
        },
    }

    result = enrich_dashboard_payload(payload, statuses)

    for symbol in ("EURUSD", "XAUUSD"):
        plan = result[symbol]
        assert plan["live_strategy_model"] == "LIVE_V3B_M5_FROZEN"
        assert plan["live_v3b_reason"] == "WAIT_V3B_PAPER_5M_BOS"
        assert plan["live_v3b_status"] == "WAIT"
        assert plan["live_v3b_details"]["source_candidate"]["symbol"] == symbol
        # Preserve compatibility fields; the V3B frontend renderer will prefer
        # the explicit live_v3b_* fields instead of rewriting legacy payloads.
        assert plan["block_reason"] == "WAIT_NO_FRESH_15M_SMC_BREAK"
        assert plan["blocked_reason"] == "WAIT_NO_FRESH_15M_SMC_BREAK"


def test_enrich_dashboard_payload_is_noop_for_non_v3b_payload():
    payload = {
        "EURUSD": {"signal": "WAIT", "block_reason": "WAIT_LEGACY"},
        "XAUUSD": {"signal": "WAIT", "block_reason": "WAIT_LEGACY"},
        "_meta": {"live_strategy_identity": "LIVE — V1"},
    }
    statuses = {
        "EURUSD": {"status": "WAIT", "reason": "WAIT_LEGACY", "details": {}},
        "XAUUSD": {"status": "WAIT", "reason": "WAIT_LEGACY", "details": {}},
    }

    result = enrich_dashboard_payload(payload, statuses)

    assert result == payload
    assert "live_v3b_reason" not in result["EURUSD"]
    assert "live_v3b_reason" not in result["XAUUSD"]


def test_v3b_runtime_reason_can_activate_bridge_without_meta_identity():
    payload = {
        "EURUSD": {"signal": "WAIT"},
        "XAUUSD": {"signal": "WAIT"},
    }
    statuses = {
        "EURUSD": {
            "status": "WAIT",
            "reason": "WAIT_V3B_RUNTIME_EVALUATION",
            "details": {"error": "example"},
        },
        "XAUUSD": {"status": "WAIT", "reason": "WAIT_LEGACY", "details": {}},
    }

    result = enrich_dashboard_payload(payload, statuses)

    assert result["EURUSD"]["live_v3b_reason"] == "WAIT_V3B_RUNTIME_EVALUATION"
    assert result["EURUSD"]["live_v3b_details"]["error"] == "example"
