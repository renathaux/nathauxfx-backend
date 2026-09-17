from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from services.live_v3b_execution_profile import V3B_EXECUTION_PROFILE
from services.live_v3b_runtime_install import install_live_v3b_runtime


@pytest.fixture(autouse=True)
def _active_v3b_config(monkeypatch):
    monkeypatch.setattr(
        "services.active_strategy_config_service.get_active_values",
        lambda **_kwargs: {
            "target_rr": 1.90,
            "protection_trigger_percent": 70.0,
            "protected_stop_percent": 60.0,
        },
    )


def _payload():
    return {
        "ok": True,
        "symbol": "EURUSD",
        "side": "BUY",
        "action": "BUY",
        "signal": "BUY",
        "entry": 1.1000,
        "sl": 1.0900,
        "tp1": 1.1133,
        "tp2": 1.1190,
        "protection_trigger_price": 1.1133,
        "protected_sl_price": 1.1114,
        "protection_trigger_tp2_fraction": 0.70,
        "protected_stop_tp2_fraction": 0.60,
        "no_partial_close_at_protection_trigger": True,
        "v3b_frozen_target_rr": 1.90,
        "risk_reward_ratio": 1.90,
        "strategy_execution_profile": V3B_EXECUTION_PROFILE,
        "live_strategy_model": "LIVE_V3B_M5_FROZEN",
        "signal_setup_id": "setup-v3b",
        "source_indicator_event_id": "event-v3b",
        "indicator_event_identity": {"event_id": "event-v3b"},
        "m5_confirmation_id": "confirm-v3b",
        "m5_confirmation_identity": {"id": "confirm-v3b"},
        "five_m_break_close_time": "2026-09-11T12:05:00+00:00",
        "five_m_closed_candle_time": "2026-09-11T12:10:00+00:00",
        "setup_identity": {
            "symbol": "EURUSD",
            "direction": "BUY",
            "swing_type": "HIGH",
            "swing_timestamp": "2026-09-11T11:40:00+00:00",
            "swing_price": 1.0990,
            "bos_candle_timestamp": "2026-09-11T12:00:00+00:00",
            "bos_level": 1.0990,
            "confirmation_timestamp": "2026-09-11T12:10:00+00:00",
            "indicator_event_id": "event-v3b",
            "m5_confirmation_id": "confirm-v3b",
            "setup_timeframe": "5m",
            "strategy_execution_profile": V3B_EXECUTION_PROFILE,
        },
    }


def _fake_api():
    prepare = Mock(side_effect=lambda payload, volume=0.01: {
        **payload,
        "ok": True,
        "tp1": 1.1152,  # legacy V1 would derive its own TP1 ratio
        "protected_sl_price": None,
    })
    locked = Mock(return_value={"ok": True, "reason": None, "details": {"v1": True}})
    fresh = Mock(return_value={"ok": True, "reason": None, "details": {"ema": True}})
    protect = Mock(side_effect=lambda trade: {**trade, "legacy_protection": True})
    auto_cycle = Mock(return_value=[{"v1": True}])
    api = SimpleNamespace(
        prepare_ctrader_trade=prepare,
        validate_auto_entry_state_locked=locked,
        validate_fresh_ema_permission_locked=fresh,
        protect_live_trade_after_tp1=protect,
        execute_live_order_core=None,
        run_ctrader_auto_trade_checks=auto_cycle,
        LIVE_ACTIVE_ORDERS={"EURUSD": None, "XAUUSD": None},
        LIVE_LAST_POSITION_CLOSED_AT={"EURUSD": 0, "XAUUSD": 0},
        LIVE_AUTO_TRADE_ENABLED={"enabled": False},
        LIVE_ACCOUNT_STATE={"connected": False, "execution_ready": False, "account_id": "demo"},
        ENGINE_RUNTIME_STATE={},
        normalize_symbol=lambda value: str(value or "").upper().replace("/", ""),
        get_live_post_close_cooldown_seconds=lambda: 900,
        get_signal_setup_id=lambda payload, side=None: payload.get("signal_setup_id"),
        get_event_lifecycles=lambda *args, **kwargs: {
            "event-v3b": {"LIVE": {"status": "ELIGIBLE"}}
        },
        live_sl_protection_confirmed=lambda trade: bool(trade.get("protection_confirmed")),
        normalize_price_to_broker_increment=lambda price, trade: (float(price), 0.00001, 5),
        modify_position_stop_loss=Mock(return_value={"ok": True}),
        read_back_broker_stop_loss=lambda trade: (trade.get("protected_sl_price"), {"ok": True}),
        persist_live_trade_state=Mock(),
        save_live_backup=Mock(),
        refresh_auto_trade_state_from_persistence=Mock(),
        sync_ctrader_account_state=Mock(),
        get_ctrader_market_data=Mock(),
        set_auto_trade_status=Mock(),
    )

    def execute(payload, source="manual"):
        if payload.get("strategy_execution_profile") == V3B_EXECUTION_PROFILE:
            api.LIVE_ACTIVE_ORDERS["EURUSD"] = {
                "symbol": "EURUSD",
                "side": "BUY",
                "entry": payload["entry"],
                "sl": payload["sl"],
                "tp1": payload["tp1"],
                "tp2": payload["tp2"],
                "position_id": "demo-position",
            }
        return {"ok": True, "active_order": api.LIVE_ACTIVE_ORDERS.get("EURUSD")}

    api.execute_live_order_core = Mock(side_effect=execute)
    return api, prepare, locked, fresh, protect, auto_cycle


def test_install_is_idempotent_and_v1_path_delegates_unchanged():
    api, prepare, locked, fresh, protect, auto_cycle = _fake_api()
    first = install_live_v3b_runtime(api, strict_trader_module=SimpleNamespace())
    second = install_live_v3b_runtime(api, strict_trader_module=SimpleNamespace())
    assert first["installed"] is True
    assert second["installed"] is False

    v1 = {"symbol": "EURUSD", "side": "BUY", "tp1": 1.1}
    api.prepare_ctrader_trade(v1)
    prepare.assert_called_once()
    api.validate_fresh_ema_permission_locked("EURUSD", "BUY", {})
    fresh.assert_called_once()
    api.protect_live_trade_after_tp1({"symbol": "EURUSD"})
    protect.assert_called_once()
    with patch("services.live_v3b_runtime_install.live_v3b_enabled", return_value=False):
        assert api.run_ctrader_auto_trade_checks({}) == [{"v1": True}]
    auto_cycle.assert_called_once_with({})


def test_v3b_prepare_restores_frozen_trigger_and_profile_after_legacy_prepare():
    api, *_ = _fake_api()
    install_live_v3b_runtime(api, strict_trader_module=SimpleNamespace())
    prepared = api.prepare_ctrader_trade(_payload())
    assert prepared["ok"] is True
    assert prepared["tp1"] == pytest.approx(1.1133)
    assert prepared["protection_trigger_price"] == pytest.approx(1.1133)
    assert prepared["protected_sl_price"] == pytest.approx(1.1114)
    assert prepared["strategy_execution_profile"] == V3B_EXECUTION_PROFILE
    assert prepared["setup_identity"]["strategy_execution_profile"] == V3B_EXECUTION_PROFILE
    assert prepared["v3b_frozen_management"]["ok"] is True


def test_runtime_prepare_uses_current_profile_validator_not_stale_import():
    api, *_ = _fake_api()
    install_live_v3b_runtime(api, strict_trader_module=SimpleNamespace())
    with patch(
        "services.live_v3b_execution_profile.validate_frozen_management_contract",
        return_value={"ok": False, "reason": "TEST_PROFILE_VALIDATOR"},
    ) as validator:
        result = api.prepare_ctrader_trade(_payload())
    validator.assert_called_once()
    assert result["ok"] is False
    assert result["reason"] == "TEST_PROFILE_VALIDATOR"


def test_v3b_fresh_gate_bypasses_only_v1_ema_and_consolidation():
    api, _prepare, _locked, fresh, *_ = _fake_api()
    install_live_v3b_runtime(api, strict_trader_module=SimpleNamespace())
    result = api.validate_fresh_ema_permission_locked(
        "EURUSD", "BUY", _payload()["setup_identity"]
    )
    assert result["ok"] is True
    assert result["details"]["v1_15m_ema_bypassed"] is True
    assert result["details"]["v1_consolidation_bypassed"] is True
    fresh.assert_not_called()


def test_v3b_protection_uses_exact_stored_protected_stop_not_legacy_calculation():
    api, _prepare, _locked, _fresh, legacy_protect, *_ = _fake_api()
    install_live_v3b_runtime(api, strict_trader_module=SimpleNamespace())
    trade = {
        **_payload(),
        "position_id": "p1",
        "original_sl": 1.0900,
        "hit_tp1": False,
        "protection_confirmed": False,
        "management_paused": True,
    }
    protected = api.protect_live_trade_after_tp1(trade)
    legacy_protect.assert_not_called()
    api.modify_position_stop_loss.assert_called_once_with(
        "p1", pytest.approx(1.1114), take_profit_price=pytest.approx(1.1190)
    )
    assert protected["protection_confirmed"] is True
    assert protected["sl"] == pytest.approx(1.1114)
    assert protected["protected_sl_price"] == pytest.approx(1.1114)


def test_successful_v3b_execution_stamps_active_trade_for_future_management():
    api, *_ = _fake_api()
    install_live_v3b_runtime(api, strict_trader_module=SimpleNamespace())
    payload = _payload()
    result = api.execute_live_order_core(payload, source="auto")
    assert result["ok"] is True
    active = api.LIVE_ACTIVE_ORDERS["EURUSD"]
    assert active["strategy_execution_profile"] == V3B_EXECUTION_PROFILE
    assert active["signal_setup_id"] == "setup-v3b"
    assert active["source_indicator_event_id"] == "event-v3b"
    assert active["tp1"] == pytest.approx(1.1133)
    assert active["protected_sl_price"] == pytest.approx(1.1114)
    assert active["no_partial_close_at_protection_trigger"] is True


def test_runtime_install_does_not_enable_any_trading_switch():
    api, *_ = _fake_api()
    with patch("services.live_v3b_runtime_install.live_v3b_enabled", return_value=False), patch(
        "services.live_v3b_runtime_install.v3b_broker_handoff_enabled",
        return_value=False,
    ):
        result = install_live_v3b_runtime(api, strict_trader_module=SimpleNamespace())
    assert result["strategy_enabled"] is False
    assert result["broker_handoff_enabled"] is False
    assert result["live_auto_enabled"] is False
    assert api.LIVE_AUTO_TRADE_ENABLED["enabled"] is False


def test_active_v3b_cycle_ignores_legacy_15m_dashboard_block():
    api, _prepare, _locked, _fresh, _protect, legacy_cycle = _fake_api()
    api.LIVE_AUTO_TRADE_ENABLED["enabled"] = True
    api.LIVE_ACCOUNT_STATE.update({"connected": True, "execution_ready": True})
    api.get_ctrader_market_data.return_value = SimpleNamespace(
        attrs={"ctrader_stream_scope": "CTRADER:DEMO:47810571"}
    )
    broker_core = api.execute_live_order_core
    install_live_v3b_runtime(api, strict_trader_module=SimpleNamespace())
    legacy_panel = {
        symbol: {
            "signal": "WAIT",
            "blocked_reason": "WAIT_INDICATOR_EVENT_STREAM_UNAVAILABLE",
        }
        for symbol in ("EURUSD", "XAUUSD")
    }
    with patch("services.live_v3b_runtime_install.selected_identity", return_value=SimpleNamespace(scope="CTRADER:DEMO:47810571")), patch(
        "services.v3b_signal_history.record_v3b_transition"
    ), patch("services.live_v3b_runtime_install.live_v3b_enabled", return_value=True), patch(
        "services.live_v3b_runtime_install.build_live_v3b_candidate",
        side_effect=lambda symbol, *_args, **_kwargs: {
            **_payload(),
            "symbol": symbol,
            "live_v3b_ready": True,
        },
    ), patch(
        "services.live_v3b_runtime_install.dispatch_v3b_to_live_core",
        return_value={"ok": False, "reason": "simulated downstream safety veto"},
    ) as dispatch:
        result = api.run_ctrader_auto_trade_checks(legacy_panel)

    assert len(result) == 2
    assert dispatch.call_count == 2
    assert [call.args[0]["symbol"] for call in dispatch.call_args_list] == ["EURUSD", "XAUUSD"]
    assert all(item["reason"] == "simulated downstream safety veto" for item in result)
    assert [call.args[:2] for call in api.get_ctrader_market_data.call_args_list] == [
        ("EURUSD", "5m"),
        ("XAUUSD", "5m"),
    ]
    legacy_cycle.assert_not_called()
    broker_core.assert_not_called()


def test_ready_buy_is_recorded_before_blocked_broker_handoff():
    from ctrader_account_context import AccountIdentity

    api, *_ = _fake_api()
    api.LIVE_AUTO_TRADE_ENABLED["enabled"] = True
    api.LIVE_ACCOUNT_STATE.update({"connected": True, "execution_ready": True})
    api.get_ctrader_market_data.return_value = SimpleNamespace(
        attrs={"ctrader_stream_scope": "CTRADER:DEMO:47810571"}
    )
    install_live_v3b_runtime(api, strict_trader_module=SimpleNamespace())
    candidate = {
        **_payload(),
        "live_v3b_ready": True,
        "source_indicator_event_id": "bos-1",
        "m5_confirmation_id": "confirmation-1",
        "signal_setup_id": "setup-1",
        "five_m_closed_candle_time": "2026-09-17T04:40:00Z",
    }
    with patch("services.live_v3b_runtime_install.live_v3b_enabled", return_value=True), patch(
        "services.live_v3b_runtime_install.build_live_v3b_candidate",
        side_effect=lambda symbol, *_args, **_kwargs: ({**candidate, "symbol": symbol}
            if symbol == "EURUSD" else {"symbol": symbol, "signal": "WAIT", "live_v3b_ready": False}),
    ), patch(
        "services.live_v3b_runtime_install.dispatch_v3b_to_live_core",
        return_value={"ok": False, "submitted": False, "reason": "WAIT_V3B_FROZEN_MANAGEMENT_CONTRACT"},
    ), patch(
        "services.live_v3b_runtime_install.selected_identity",
        return_value=AccountIdentity("47810571", "demo"),
    ), patch("services.v3b_signal_history.record_v3b_transition") as record:
        api.run_ctrader_auto_trade_checks({})

    eurusd_calls = [call for call in record.call_args_list if call.args[1] == "EURUSD"]
    assert len(eurusd_calls) == 2
    assert all(call.args[2] == "BUY" for call in eurusd_calls)
    assert eurusd_calls[0].args[0] == "CTRADER:DEMO:47810571"
    assert eurusd_calls[0].kwargs["setup_id"] == "setup-1"
    assert eurusd_calls[1].kwargs["execution_status"] == "BLOCKED"
    assert eurusd_calls[1].kwargs["reason"] == "WAIT_V3B_FROZEN_MANAGEMENT_CONTRACT"
    eurusd_status = [call for call in api.set_auto_trade_status.call_args_list
                     if call.kwargs["symbol"] == "EURUSD"][-1]
    assert eurusd_status.kwargs["signal"] == "BUY"
    assert eurusd_status.kwargs["status"] == "BLOCKED"
    assert eurusd_status.kwargs["details"]["source_candidate"]["source_indicator_event_id"] == "bos-1"
    assert eurusd_status.kwargs["details"]["source_candidate"]["m5_confirmation_id"] == "confirmation-1"


def test_repeated_poll_preserves_executed_setup_without_second_dispatch():
    from ctrader_account_context import AccountIdentity

    api, *_ = _fake_api()
    api.LIVE_AUTO_TRADE_ENABLED["enabled"] = True
    api.LIVE_ACCOUNT_STATE.update({"connected": True, "execution_ready": True})
    api.get_ctrader_market_data.return_value = SimpleNamespace(
        attrs={"ctrader_stream_scope": "CTRADER:DEMO:47810571"}
    )
    install_live_v3b_runtime(api, strict_trader_module=SimpleNamespace())
    candidate = {
        **_payload(), "live_v3b_ready": True,
        "v3b_setup_state": {"signal": "BUY", "lifecycle_state": "ELIGIBLE"},
    }
    polls = {"count": 0}

    def record(_scope, symbol, signal, _time, **kwargs):
        if symbol != "EURUSD" or signal != "BUY":
            return {"signal": "WAIT"}
        if kwargs.get("execution_status") == "EXECUTED":
            return {"signal": "BUY", "signal_setup_id": "setup-v3b", "execution_status": "EXECUTED"}
        polls["count"] += 1
        return {"signal": "BUY", "signal_setup_id": "setup-v3b",
                "execution_status": "EXECUTED" if polls["count"] > 1 else "CANDIDATE"}

    with patch("services.live_v3b_runtime_install.live_v3b_enabled", return_value=True), patch(
        "services.live_v3b_runtime_install.selected_identity",
        return_value=AccountIdentity("47810571", "demo"),
    ), patch(
        "services.live_v3b_runtime_install.build_live_v3b_candidate",
        side_effect=lambda symbol, *_args, **_kwargs: candidate if symbol == "EURUSD" else {
            "symbol": symbol, "signal": "WAIT", "live_v3b_ready": False},
    ), patch("services.v3b_signal_history.record_v3b_transition", side_effect=record), patch(
        "services.live_v3b_runtime_install.dispatch_v3b_to_live_core",
        return_value={"ok": True, "submitted": True},
    ) as dispatch:
        api.run_ctrader_auto_trade_checks({})
        api.run_ctrader_auto_trade_checks({})

    assert dispatch.call_count == 1
    statuses = [call.kwargs for call in api.set_auto_trade_status.call_args_list
                if call.kwargs["symbol"] == "EURUSD"]
    assert statuses[-1]["status"] == "EXECUTED"
    assert statuses[-1]["details"]["source_candidate"]["v3b_setup_state"]["lifecycle_state"] == "CONSUMED"


def test_ambiguous_broker_result_remains_reconciliation_required_on_replay():
    from ctrader_account_context import AccountIdentity

    api, *_ = _fake_api()
    api.LIVE_AUTO_TRADE_ENABLED["enabled"] = True
    api.LIVE_ACCOUNT_STATE.update({"connected": True, "execution_ready": True})
    api.get_ctrader_market_data.return_value = SimpleNamespace(
        attrs={"ctrader_stream_scope": "CTRADER:DEMO:47810571"}
    )
    install_live_v3b_runtime(api, strict_trader_module=SimpleNamespace())
    candidate = {**_payload(), "live_v3b_ready": True,
                 "v3b_setup_state": {"signal": "BUY", "lifecycle_state": "ELIGIBLE"}}
    history = {}

    def record(_scope, symbol, signal, _time, **kwargs):
        if symbol != "EURUSD" or signal != "BUY":
            return {"signal": "WAIT"}
        if kwargs.get("execution_status"):
            history.update(signal="BUY", signal_setup_id="setup-v3b",
                           execution_status=kwargs["execution_status"])
        return history or {"signal": "BUY", "signal_setup_id": "setup-v3b",
                           "execution_status": "CANDIDATE"}

    with patch("services.live_v3b_runtime_install.live_v3b_enabled", return_value=True), patch(
        "services.live_v3b_runtime_install.selected_identity",
        return_value=AccountIdentity("47810571", "demo"),
    ), patch("services.live_v3b_runtime_install.build_live_v3b_candidate",
             side_effect=lambda symbol, *_args, **_kwargs: candidate if symbol == "EURUSD" else {
                 "symbol": symbol, "signal": "WAIT", "live_v3b_ready": False}), patch(
        "services.v3b_signal_history.record_v3b_transition", side_effect=record,
    ), patch("services.live_v3b_runtime_install.dispatch_v3b_to_live_core",
             return_value={"ok": False, "submitted": True, "reason": "broker timeout",
                           "execution_result": {"broker_result": "AMBIGUOUS"}}) as dispatch:
        api.run_ctrader_auto_trade_checks({})
        api.run_ctrader_auto_trade_checks({})

    assert dispatch.call_count == 1
    assert history["execution_status"] == "RECONCILIATION_REQUIRED"
    statuses = [call.kwargs for call in api.set_auto_trade_status.call_args_list
                if call.kwargs["symbol"] == "EURUSD"]
    assert statuses[-1]["status"] == "RECONCILIATION_REQUIRED"


def test_durable_submission_marker_prevents_replay_after_executor_exception():
    from ctrader_account_context import AccountIdentity

    api, *_ = _fake_api()
    api.LIVE_AUTO_TRADE_ENABLED["enabled"] = True
    api.LIVE_ACCOUNT_STATE.update({"connected": True, "execution_ready": True})
    api.get_ctrader_market_data.return_value = SimpleNamespace(
        attrs={"ctrader_stream_scope": "CTRADER:DEMO:47810571"}
    )
    marker = {"status": "ELIGIBLE"}
    api.get_event_lifecycles = Mock(side_effect=lambda *_args, **kwargs: {
        "event-v3b": {"LIVE": {"status": marker["status"]}}
    })
    install_live_v3b_runtime(api, strict_trader_module=SimpleNamespace())
    candidate = {**_payload(), "live_v3b_ready": True,
                 "v3b_setup_state": {"signal": "BUY", "lifecycle_state": "ELIGIBLE"}}
    history = {}

    def record(_scope, symbol, signal, _time, **kwargs):
        if symbol != "EURUSD" or signal != "BUY":
            return {"signal": "WAIT"}
        if kwargs.get("execution_status"):
            history.update(signal="BUY", signal_setup_id="setup-v3b",
                           execution_status=kwargs["execution_status"])
        return history or {"signal": "BUY", "signal_setup_id": "setup-v3b",
                           "execution_status": "CANDIDATE"}

    def uncertain_send(_candidate, **_kwargs):
        marker["status"] = "RECONCILIATION_REQUIRED"
        raise RuntimeError("network dropped after durable request-start marker")

    with patch("services.live_v3b_runtime_install.live_v3b_enabled", return_value=True), patch(
        "services.live_v3b_runtime_install.selected_identity",
        return_value=AccountIdentity("47810571", "demo"),
    ), patch("services.live_v3b_runtime_install.build_live_v3b_candidate",
             side_effect=lambda symbol, *_args, **_kwargs: candidate if symbol == "EURUSD" else {
                 "symbol": symbol, "signal": "WAIT", "live_v3b_ready": False}), patch(
        "services.v3b_signal_history.record_v3b_transition", side_effect=record,
    ), patch("services.live_v3b_runtime_install.dispatch_v3b_to_live_core",
             side_effect=uncertain_send) as dispatch:
        with pytest.raises(RuntimeError, match="network dropped"):
            api.run_ctrader_auto_trade_checks({})
        api.run_ctrader_auto_trade_checks({})
        marker["status"] = "CONSUMED"
        api.run_ctrader_auto_trade_checks({})

    assert dispatch.call_count == 1
    assert history["execution_status"] == "EXECUTED"
    statuses = [call.kwargs for call in api.set_auto_trade_status.call_args_list
                if call.kwargs["symbol"] == "EURUSD"]
    assert [row["status"] for row in statuses[-2:]] == ["RECONCILIATION_REQUIRED", "EXECUTED"]
    api.get_event_lifecycles.assert_called_with(
        ["event-v3b"], owner_id="OWNER", account_id="47810571")


def test_selection_change_before_dispatch_never_sends_previous_account_candidate():
    from ctrader_account_context import AccountIdentity

    api, *_ = _fake_api()
    api.LIVE_AUTO_TRADE_ENABLED["enabled"] = True
    api.LIVE_ACCOUNT_STATE.update({"connected": True, "execution_ready": True})
    api.get_ctrader_market_data.return_value = SimpleNamespace(
        attrs={"ctrader_stream_scope": "CTRADER:DEMO:47810571"}
    )
    install_live_v3b_runtime(api, strict_trader_module=SimpleNamespace())
    candidate = {**_payload(), "live_v3b_ready": True,
                 "v3b_setup_state": {"signal": "BUY", "lifecycle_state": "ELIGIBLE"}}
    picks = {"count": 0}
    def switched_selection():
        picks["count"] += 1
        return AccountIdentity("47810571" if picks["count"] == 1 else "47784297", "demo")
    with patch("services.live_v3b_runtime_install.live_v3b_enabled", return_value=True), patch(
        "services.live_v3b_runtime_install.selected_identity", side_effect=switched_selection,
    ), patch("services.live_v3b_runtime_install.build_live_v3b_candidate",
             side_effect=lambda symbol, *_args, **_kwargs: candidate if symbol == "EURUSD" else {
                 "symbol": symbol, "signal": "WAIT", "live_v3b_ready": False}), patch(
        "services.v3b_signal_history.record_v3b_transition",
        return_value={"signal": "BUY", "signal_setup_id": "setup-v3b", "execution_status": "CANDIDATE"},
    ), patch("services.live_v3b_runtime_install.dispatch_v3b_to_live_core") as dispatch:
        result = api.run_ctrader_auto_trade_checks({})
    dispatch.assert_not_called()
    assert result[0]["reason"] == "WAIT_V3B_ACCOUNT_SELECTION_CHANGED"


def test_other_worker_submitting_does_not_poison_history_as_reconciliation():
    from ctrader_account_context import AccountIdentity

    api, *_ = _fake_api()
    api.LIVE_AUTO_TRADE_ENABLED["enabled"] = True
    api.LIVE_ACCOUNT_STATE.update({"connected": True, "execution_ready": True})
    api.get_ctrader_market_data.return_value = SimpleNamespace(
        attrs={"ctrader_stream_scope": "CTRADER:DEMO:47810571"}
    )
    marker = {"status": "SUBMITTING"}
    api.get_event_lifecycles = Mock(side_effect=lambda *_args, **_kwargs: {
        "event-v3b": {"LIVE": {"status": marker["status"]}}
    })
    install_live_v3b_runtime(api, strict_trader_module=SimpleNamespace())
    candidate = {**_payload(), "live_v3b_ready": True,
                 "v3b_setup_state": {"signal": "BUY", "lifecycle_state": "ELIGIBLE"}}
    records = []

    def record(_scope, symbol, signal, _time, **kwargs):
        if symbol == "EURUSD" and signal == "BUY":
            records.append(kwargs.get("execution_status"))
        return {"signal": signal, "signal_setup_id": "setup-v3b",
                "execution_status": "CANDIDATE"}

    with patch("services.live_v3b_runtime_install.live_v3b_enabled", return_value=True), patch(
        "services.live_v3b_runtime_install.selected_identity",
        return_value=AccountIdentity("47810571", "demo"),
    ), patch("services.live_v3b_runtime_install.build_live_v3b_candidate",
             side_effect=lambda symbol, *_args, **_kwargs: candidate if symbol == "EURUSD" else {
                 "symbol": symbol, "signal": "WAIT", "live_v3b_ready": False}), patch(
        "services.v3b_signal_history.record_v3b_transition", side_effect=record,
    ), patch("services.live_v3b_runtime_install.dispatch_v3b_to_live_core",
             return_value={"ok": False, "submitted": True, "reason": "definitely rejected",
                           "execution_result": {"broker_result": "DEFINITELY_REJECTED"}}) as dispatch:
        api.run_ctrader_auto_trade_checks({})
        assert dispatch.call_count == 0
        assert records == [None]
        marker["status"] = "ELIGIBLE"
        api.run_ctrader_auto_trade_checks({})

    assert dispatch.call_count == 1
    assert "RECONCILIATION_REQUIRED" not in records
    assert records[-1] == "BLOCKED"


def test_partial_bos_history_uses_nested_canonical_identity():
    from ctrader_account_context import AccountIdentity

    api, *_ = _fake_api()
    api.get_ctrader_market_data.return_value = SimpleNamespace(
        attrs={"ctrader_stream_scope": "CTRADER:DEMO:47810571"}
    )
    install_live_v3b_runtime(api, strict_trader_module=SimpleNamespace())
    partial = {
        "symbol": "EURUSD", "signal": "WAIT", "live_v3b_ready": False,
        "live_v3b_reason": "WAIT_V3B_NEXT_5M_CONFIRMATION",
        "live_v3b_details": {"source_candidate": {
            "source_indicator_event_id": "bos-1",
            "five_m_break_close_time": "2026-09-17T04:35:00Z",
            "v3b_setup_state": {"signal": "WAIT", "lifecycle_state": "WAITING_CONFIRMATION"},
        }},
    }
    with patch("services.live_v3b_runtime_install.live_v3b_enabled", return_value=True), patch(
        "services.live_v3b_runtime_install.selected_identity",
        return_value=AccountIdentity("47810571", "demo"),
    ), patch("services.live_v3b_runtime_install.build_live_v3b_candidate",
             side_effect=lambda symbol, *_args, **_kwargs: partial if symbol == "EURUSD" else {
                 "symbol": symbol, "signal": "WAIT", "live_v3b_ready": False}), patch(
        "services.v3b_signal_history.record_v3b_transition",
    ) as record:
        api.run_ctrader_auto_trade_checks({})
    call = [call for call in record.call_args_list if call.args[1] == "EURUSD"][0]
    assert call.kwargs["event_id"] == "bos-1"
    assert call.args[3] == "2026-09-17T04:35:00Z"


def test_closed_5m_replay_preserves_one_event_from_partial_to_consumed():
    from ctrader_account_context import AccountIdentity
    from services.live_v3b_service import build_live_v3b_candidate as evaluate
    import pandas as pd

    index = pd.to_datetime(["2026-09-17T04:25:00Z", "2026-09-17T04:30:00Z", "2026-09-17T04:35:00Z"])
    frame = pd.DataFrame({
        "Open": [1.0998, 1.1000, 1.1017], "High": [1.1002, 1.1020, 1.1028],
        "Low": [1.0995, 1.0995, 1.1015], "Close": [1.1000, 1.1018, 1.1025],
    }, index=index)
    event = {
        "event_id": "replay-bos", "symbol": "EURUSD", "timeframe": "5m",
        "timestamp": index[1].isoformat(), "event_type": "BOS",
        "direction": "BULLISH", "broken_level": 1.1015,
        "broken_swing_timestamp": "2026-09-17T04:20:00Z",
        "event_invalidation_swing": {"type": "LOW", "price": 1.1000},
        "tradable": True,
    }

    class Strict:
        @staticmethod
        def closed_frame(value, minutes):
            assert minutes == 5
            return value.copy()

        @staticmethod
        def point_size(symbol):
            return 0.00001

    def evaluate_symbol(symbol, data, **_kwargs):
        if symbol != "EURUSD":
            return {"symbol": symbol, "signal": "WAIT", "live_v3b_ready": False}
        return evaluate(
            symbol, data, strict_trader_module=Strict,
            setup_id_builder=lambda _candidate, _side: "replay-setup",
            authoritative_reader=lambda *_args: {"events": [event]}, enabled=True,
        )

    api, *_ = _fake_api()
    api.LIVE_AUTO_TRADE_ENABLED["enabled"] = True
    api.LIVE_ACCOUNT_STATE.update({"connected": True, "execution_ready": True})
    stage = [0]
    # The test frame is explicitly bound to the selected account at fetch time.
    def scoped_fetch(symbol, *_args, **_kwargs):
        result = frame.iloc[:2].copy() if stage[0] == 0 else frame.copy()
        result.attrs["ctrader_stream_scope"] = "CTRADER:DEMO:47810571"
        return result
    api.get_ctrader_market_data.side_effect = scoped_fetch
    expected_partial = evaluate_symbol("EURUSD", scoped_fetch("EURUSD"))
    assert expected_partial["live_v3b_details"]["source_candidate"]["source_indicator_event_id"] == "replay-bos"
    install_live_v3b_runtime(api, strict_trader_module=Strict)
    recorded = {}

    def record(scope, symbol, signal, _time, **kwargs):
        assert scope == "CTRADER:DEMO:47810571"
        if symbol != "EURUSD":
            return {"signal": "WAIT"}
        if signal == "WAIT":
            assert kwargs["event_id"] == "replay-bos"
            return {"signal": "WAIT", "event_id": "replay-bos"}
        if not kwargs.get("execution_status"):
            assert kwargs["event_id"] == "replay-bos"
        if kwargs.get("execution_status") == "EXECUTED":
            recorded.update(signal="BUY", signal_setup_id="replay-setup",
                            execution_status="EXECUTED")
        return recorded or {"signal": "BUY", "signal_setup_id": "replay-setup",
                            "execution_status": "CANDIDATE"}

    with patch("services.live_v3b_runtime_install.live_v3b_enabled", return_value=True), patch(
        "services.live_v3b_runtime_install.selected_identity",
        return_value=AccountIdentity("47810571", "demo"),
    ), patch("services.live_v3b_runtime_install.build_live_v3b_candidate",
             side_effect=evaluate_symbol), patch(
        "services.v3b_signal_history.record_v3b_transition", side_effect=record,
    ), patch("services.live_v3b_runtime_install.dispatch_v3b_to_live_core",
             return_value={"ok": True, "submitted": True}) as dispatch:
        first_cycle = api.run_ctrader_auto_trade_checks({})
        assert any(call.kwargs["symbol"] == "EURUSD" for call in api.set_auto_trade_status.call_args_list), first_cycle
        partial = [call.kwargs for call in api.set_auto_trade_status.call_args_list
                   if call.kwargs["symbol"] == "EURUSD"][-1]
        stage[0] = 1
        api.run_ctrader_auto_trade_checks({})
        eligible = [call.kwargs for call in api.set_auto_trade_status.call_args_list
                    if call.kwargs["symbol"] == "EURUSD"][-1]
        api.run_ctrader_auto_trade_checks({})
        replay = [call.kwargs for call in api.set_auto_trade_status.call_args_list
                  if call.kwargs["symbol"] == "EURUSD"][-1]

    assert partial["details"]["source_candidate"]["v3b_setup_state"]["indicator_event_id"] == "replay-bos"
    assert partial["details"]["source_candidate"]["v3b_setup_state"]["second_5m_same_direction"] is None
    assert eligible["details"]["source_candidate"]["v3b_setup_state"]["indicator_event_id"] == "replay-bos"
    assert eligible["details"]["source_candidate"]["v3b_setup_state"]["signal"] == "BUY"
    assert eligible["status"] == replay["status"] == "EXECUTED"
    assert dispatch.call_count == 1
    assert dispatch.call_args.args[0]["source_indicator_event_id"] == "replay-bos"
    assert dispatch.call_args.args[0]["signal_setup_id"] == "replay-setup"
