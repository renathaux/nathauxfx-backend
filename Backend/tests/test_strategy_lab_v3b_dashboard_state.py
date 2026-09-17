from services.v3b_dashboard_state import enrich_dashboard_payload


def test_active_v3b_dashboard_does_not_expose_legacy_15m_stream_block():
    payload = {
        "_meta": {"live_strategy_identity": "LIVE — V3B"},
        "EURUSD": {
            "signal": "WAIT",
            "block_reason": "WAIT_INDICATOR_EVENT_STREAM_UNAVAILABLE",
            "blocked_reason": "WAIT_INDICATOR_EVENT_STREAM_UNAVAILABLE",
            "blocked_by": "WAIT_INDICATOR_EVENT_STREAM_UNAVAILABLE",
            "plan_reason": "WAIT_INDICATOR_EVENT_STREAM_UNAVAILABLE",
            "signal_data_source": {"latest_5m_time": "2026-09-17T01:15:00+00:00"},
        },
    }
    statuses = {
        "EURUSD": {
            "status": "WAIT",
            "reason": "WAIT_V3B_PAPER_5M_BOS",
            "checked_at": 1789608000.0,
            "details": {"source_candidate": {"symbol": "EURUSD", "signal": "WAIT"}},
        }
    }

    result = enrich_dashboard_payload(payload, statuses)["EURUSD"]

    assert result["signal"] == "WAIT"
    assert result["blocked_reason"] == "WAIT_V3B_PAPER_5M_BOS"
    assert result["block_reason"] == "WAIT_V3B_PAPER_5M_BOS"
    assert result["plan_reason"] == "WAIT_V3B_PAPER_5M_BOS"
    assert result["blocked_by"] == "v3b_runtime"
    assert result["signal_data_source"]["latest_5m_time"] == "2026-09-17T01:15:00+00:00"
    assert payload["EURUSD"]["blocked_reason"] == "WAIT_INDICATOR_EVENT_STREAM_UNAVAILABLE"


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
        assert plan["block_reason"] == "WAIT_V3B_PAPER_5M_BOS"
        assert plan["blocked_reason"] == "WAIT_V3B_PAPER_5M_BOS"


def test_v3b_dashboard_keeps_real_5m_authority_block_fail_closed():
    payload = {
        "_meta": {"live_strategy_identity": "LIVE — V3B"},
        "EURUSD": {
            "signal": "WAIT",
            "blocked_reason": "WAIT_INDICATOR_EVENT_STREAM_UNAVAILABLE",
            "execution_allowed": False,
        },
    }
    statuses = {
        "EURUSD": {
            "status": "WAIT",
            "reason": "WAIT_V3B_5M_AUTHORITY_STALE",
            "checked_at": 1789608000.0,
            "details": {"source_candidate": {"symbol": "EURUSD", "signal": "WAIT"}},
        }
    }

    result = enrich_dashboard_payload(payload, statuses)["EURUSD"]

    assert result["blocked_reason"] == "WAIT_V3B_5M_AUTHORITY_STALE"
    assert result["signal"] == "WAIT"
    assert result["execution_allowed"] is False


def test_active_v3b_dashboard_shows_broker_block_instead_of_legacy_15m_block():
    payload = {
        "_meta": {"live_strategy_identity": "LIVE — V3B"},
        "EURUSD": {
            "signal": "WAIT",
            "blocked_reason": "WAIT_INDICATOR_EVENT_STREAM_UNAVAILABLE",
        },
    }
    statuses = {
        "EURUSD": {
            "status": "BLOCKED",
            "reason": "Live Auto paused — broker disconnected",
            "details": {},
        }
    }

    result = enrich_dashboard_payload(payload, statuses)["EURUSD"]

    assert result["blocked_reason"] == "Live Auto paused — broker disconnected"
    assert result["blocked_by"] == "v3b_runtime"
    assert result["signal"] == "WAIT"


def test_v3b_dashboard_does_not_hide_legacy_block_without_v3b_runtime_status():
    payload = {
        "_meta": {"live_strategy_identity": "LIVE — V3B"},
        "EURUSD": {
            "signal": "WAIT",
            "blocked_reason": "WAIT_INDICATOR_EVENT_STREAM_UNAVAILABLE",
        },
    }

    result = enrich_dashboard_payload(payload, {})["EURUSD"]

    assert result["blocked_reason"] == "WAIT_INDICATOR_EVENT_STREAM_UNAVAILABLE"
    assert result["signal"] == "WAIT"


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


def test_v3b_history_replaces_legacy_history_without_rewriting_blocked_buy():
    payload = {
        "_meta": {"live_strategy_identity": "LIVE — V3B"},
        "EURUSD": {"signal": "WAIT"},
        "history": [{"symbol": "EURUSD", "signal": "WAIT", "timestamp": "2026-09-01T00:00:00Z"}],
    }
    durable = [{
        "symbol": "EURUSD", "signal": "BUY", "execution_status": "BLOCKED",
        "reason": "WAIT_V3B_FROZEN_MANAGEMENT_CONTRACT",
        "timestamp": "2026-09-17T04:40:00Z",
    }]
    result = enrich_dashboard_payload(payload, {}, signal_history=durable)
    assert result["history"] == durable
    assert result["history"][0]["signal"] == "BUY"


def test_non_v3b_history_remains_legacy_when_durable_rows_are_supplied():
    payload = {
        "_meta": {"live_strategy_identity": "LIVE — V1"},
        "history": [{"symbol": "EURUSD", "signal": "WAIT"}],
    }
    assert enrich_dashboard_payload(payload, {}, signal_history=[{"signal": "BUY"}]) == payload


def test_previous_account_candidate_cannot_overwrite_selected_account_dashboard():
    payload = {
        "_meta": {"live_strategy_identity": "LIVE — V3B", "account_scope": "CTRADER:DEMO:47810571"},
        "EURUSD": {"signal": "WAIT", "blocked_reason": "WAIT_OWN_SCOPE"},
    }
    statuses = {"EURUSD": {
        "status": "BLOCKED", "reason": "WAIT_PREVIOUS_ACCOUNT",
        "details": {"account_scope": "CTRADER:DEMO:47784297", "source_candidate": {
            "signal": "BUY", "source_indicator_event_id": "other-account-event",
        }},
    }}
    result = enrich_dashboard_payload(payload, statuses)
    assert result["EURUSD"] == payload["EURUSD"]

    statuses["EURUSD"]["details"].pop("account_scope")
    unscoped = enrich_dashboard_payload(payload, statuses)
    assert unscoped["EURUSD"] == payload["EURUSD"]
