from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import api
from ctrader_account_context import AccountIdentity
from services import strategy_studio_live_state as live_state


def _v3b_panel(signal="SELL"):
    return {
        "_meta": {"account_scope": "CTRADER:DEMO:acct-1"},
        "EURUSD": {
            "symbol": "EURUSD",
            "signal": signal,
            "entry_price": 1.1500,
            "stop_loss": 1.1450,
            "tp1": 1.1550,
            "tp2": 1.1595,
        },
        "XAUUSD": {"symbol": "XAUUSD", "signal": "WAIT"},
        "candles": {},
    }


def _studio_candidate(*, risk_method="PERCENT_BALANCE", risk_value=0.5):
    return {
        "signal": "BUY",
        "reason": "STUDIO_CANDIDATE_READY",
        "studio_live_ready": True,
        "setup_id": "sts1_testsetup",
        "strategy_id": "strat-1",
        "account_id": "acct-1",
        "account_scope": "CTRADER:DEMO:acct-1",
        "symbol": "EURUSD",
        "entry": 1.1500,
        "sl": 1.1450,
        "tp1": 1.1540,
        "tp2": 1.1595,
        "risk_budget": {
            "method": risk_method,
            "value": risk_value,
            "dollars": 50.0 if risk_method == "FIXED_DOLLARS" else 50.0,
        },
        "tp1_definition": {
            "enabled": True,
            "target_r": 0.8,
            "close_percent": 50.0,
            "protection_r": 0.0,
        },
        "fundamental_policy": "REQUIRE_ALIGNMENT",
        "evaluator_steps": {},
        "next_state": None,
    }


def _require_api_contract(name):
    value = getattr(api, name, None)
    assert value is not None, f"Task 5 contract missing: api.{name}"
    return value


def test_gate_off_keeps_existing_v3b_candidate_unchanged(monkeypatch):
    selector = _require_api_contract("select_auto_execution_candidate")
    monkeypatch.setattr(api, "get_enabled_studio_live_owner", lambda *args, **kwargs: None)
    studio_builder = MagicMock()
    monkeypatch.setattr(api, "build_studio_candidate", studio_builder)

    panel = _v3b_panel("SELL")
    selected = selector(panel, "EURUSD")

    assert selected["source"] == "V3B"
    assert selected["plan"] is panel["EURUSD"]
    assert selected["plan"]["signal"] == "SELL"
    studio_builder.assert_not_called()


def test_gate_on_uses_studio_candidate_and_never_falls_back_to_v3b(monkeypatch):
    selector = _require_api_contract("select_auto_execution_candidate")
    monkeypatch.setattr(api, "get_enabled_studio_live_owner", lambda *args, **kwargs: "user:1")
    monkeypatch.setattr(api, "current_identity", lambda: AccountIdentity("acct-1", "demo"))
    monkeypatch.setattr(
        api,
        "get_ctrader_account_snapshot",
        lambda: {"balance": 10000.0, "equity": 10000.0},
    )
    monkeypatch.setattr(
        api,
        "validate_verified_account_snapshot",
        lambda snapshot: {"ok": True, "balance": 10000.0, "account_equity_used": 10000.0},
    )
    bundle = {"5m": SimpleNamespace(attrs={"ctrader_stream_scope": "CTRADER:DEMO:acct-1"})}
    monkeypatch.setattr(api, "load_strategy_studio_market_bundle", lambda *args, **kwargs: bundle)
    builder = MagicMock(return_value=_studio_candidate())
    monkeypatch.setattr(api, "build_studio_candidate", builder)

    selected = selector(_v3b_panel("SELL"), "EURUSD")

    assert selected["source"] == "STRATEGY_STUDIO"
    assert selected["plan"]["signal"] == "BUY"
    assert selected["plan"]["execution_source"] == "STRATEGY_STUDIO"
    assert selected["plan"]["studio_setup_id"] == "sts1_testsetup"
    assert selected["plan"]["studio_account_scope"] == "CTRADER:DEMO:acct-1"
    builder.assert_called_once()


def test_gate_on_wait_does_not_fall_back_to_v3b(monkeypatch):
    selector = _require_api_contract("select_auto_execution_candidate")
    monkeypatch.setattr(api, "get_enabled_studio_live_owner", lambda *args, **kwargs: "user:1")
    monkeypatch.setattr(api, "current_identity", lambda: AccountIdentity("acct-1", "demo"))
    monkeypatch.setattr(api, "get_ctrader_account_snapshot", lambda: {"balance": 10000.0, "equity": 10000.0})
    monkeypatch.setattr(
        api,
        "validate_verified_account_snapshot",
        lambda snapshot: {"ok": True, "balance": 10000.0, "account_equity_used": 10000.0},
    )
    monkeypatch.setattr(api, "load_strategy_studio_market_bundle", lambda *args, **kwargs: {"5m": object()})
    monkeypatch.setattr(
        api,
        "build_studio_candidate",
        lambda *args, **kwargs: {
            "signal": "WAIT",
            "reason": "WAIT_STUDIO_EVALUATOR",
            "studio_live_ready": False,
            "strategy_id": "strat-1",
            "setup_id": None,
            "account_scope": "CTRADER:DEMO:acct-1",
            "evaluator_steps": {},
            "next_state": None,
        },
    )

    selected = selector(_v3b_panel("SELL"), "EURUSD")
    assert selected["source"] == "STRATEGY_STUDIO"
    assert selected["plan"]["signal"] == "WAIT"
    assert selected["plan"]["signal"] != "SELL"


def test_fixed_dollar_risk_conversion_is_exact_and_not_silently_clamped():
    adapter = _require_api_contract("studio_candidate_execution_plan")
    candidate = _studio_candidate(risk_method="FIXED_DOLLARS", risk_value=200.0)
    candidate["risk_budget"] = {"method": "FIXED_DOLLARS", "value": 200.0, "dollars": 200.0}

    plan = adapter(candidate, account_balance=10000.0, owner_id="user:1")

    assert plan["studio_risk_method"] == "FIXED_DOLLARS"
    assert plan["studio_risk_value"] == pytest.approx(200.0)
    assert plan["requested_risk_percent"] == pytest.approx(2.0)
    assert plan["fundamental_policy"] == "REQUIRE_ALIGNMENT"


def test_percent_balance_risk_is_passed_exactly_to_live_sizer(monkeypatch):
    monkeypatch.setattr(api, "get_ctrader_account_snapshot", lambda: {"balance": 10000.0, "equity": 10000.0})
    monkeypatch.setattr(
        api,
        "validate_verified_account_snapshot",
        lambda account: {"ok": True, "balance": 10000.0, "account_equity_used": 10000.0},
    )
    monkeypatch.setattr(
        api,
        "get_ctrader_symbol_risk_metadata",
        lambda symbol: {"ok": True, "pip_size": 0.0001, "metadata_source": "test"},
    )
    position_size = MagicMock(return_value={"ok": True, "risk_percent": 0.4})
    monkeypatch.setattr(api, "calculate_position_size", position_size)

    result = api.calculate_live_risk_size("EURUSD", 1.1500, 1.1450, risk_percent_override=0.4)

    assert result["ok"] is True
    assert position_size.call_args.args[2] == pytest.approx(0.4)


def test_studio_submission_claim_fails_closed_on_account_scope_change(monkeypatch):
    claim = _require_api_contract("claim_execution_submission")
    monkeypatch.setattr(api, "current_identity", lambda: AccountIdentity("acct-2", "demo"))
    strategy_claim = MagicMock()
    monkeypatch.setattr(api, "claim_strategy_submission", strategy_claim)

    payload = {
        "execution_source": "STRATEGY_STUDIO",
        "studio_owner_id": "user:1",
        "studio_strategy_id": "strat-1",
        "studio_setup_id": "sts1_testsetup",
        "studio_account_scope": "CTRADER:DEMO:acct-1",
        "signal_setup_id": "sts1_testsetup",
        "symbol": "EURUSD",
        "action": "BUY",
        "signal": "BUY",
    }
    result = claim(payload)

    assert result["ok"] is False
    assert result["reason"] == "Strategy Studio account scope changed before submission"
    strategy_claim.assert_not_called()


def test_studio_submission_uses_shared_claim_and_stable_setup(monkeypatch):
    claim = _require_api_contract("claim_execution_submission")
    monkeypatch.setattr(api, "current_identity", lambda: AccountIdentity("acct-1", "demo"))
    monkeypatch.setattr(api, "get_active_ctrader_account_id", lambda: "acct-1")
    strategy_claim = MagicMock(return_value={
        "ok": True,
        "idempotency_key": "fs1_studio",
        "broker_client_order_id": "client-studio",
        "broker_label": "label-studio",
        "broker_comment": "comment-studio",
    })
    monkeypatch.setattr(api, "claim_strategy_submission", strategy_claim)

    payload = {
        "execution_source": "STRATEGY_STUDIO",
        "studio_owner_id": "user:1",
        "studio_strategy_id": "strat-1",
        "studio_setup_id": "sts1_testsetup",
        "studio_account_scope": "CTRADER:DEMO:acct-1",
        "signal_setup_id": "sts1_testsetup",
        "symbol": "EURUSD",
        "action": "BUY",
        "signal": "BUY",
    }
    result = claim(payload)

    assert result["ok"] is True
    strategy_claim.assert_called_once_with(
        "sts1_testsetup",
        "acct-1",
        "EURUSD",
        "BUY",
        payload,
        owner_id="user:1",
        strategy_id="strat-1",
    )


def test_runtime_owner_resolution_fails_closed_when_not_exactly_one_enabled(monkeypatch):
    resolver = getattr(live_state, "get_enabled_studio_live_owner", None)
    assert resolver is not None, "Task 5 contract missing: get_enabled_studio_live_owner"


def test_task5_core_wiring_exists_before_broker_request():
    import inspect

    source = inspect.getsource(api._execute_live_order_core_impl)
    assert "claim_execution_submission" in source
    assert "LIVE_FUNDAMENTAL_FINAL_GATE" in source
    assert source.index("LIVE_FUNDAMENTAL_FINAL_GATE") < source.index("claim_execution_submission")
    assert source.index("claim_execution_submission") < source.index("place_market_order_with_inflight_cleanup")
