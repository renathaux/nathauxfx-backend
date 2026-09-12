from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from services.live_v3b_execution_profile import V3B_EXECUTION_PROFILE
from services.live_v3b_runtime_install import install_live_v3b_runtime


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
