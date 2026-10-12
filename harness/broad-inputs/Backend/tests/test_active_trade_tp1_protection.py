"""An active broker position must not lose TP1 protection with its snapshot."""

import pytest
import time


def _trade(*, bid=4336.0, high=4338.0):
    return {
        "account_id": "47810571",
        "account_scope": "CTRADER:DEMO:47810571",
        "symbol": "XAUUSD",
        "side": "BUY",
        "position_id": "59231654",
        "entry": 4316.54,
        "sl": 4273.40,
        "tp1": 4338.00,
        "tp2": 4343.36,
        "bid": bid,
        "ask": bid + 0.10,
        "quote_timestamp": time.time(),
        "quote_account_scope": "CTRADER:DEMO:47810571",
        "current_price": bid,
        "current_high": high,
        "trusted_tp1_high": high,
        "management_paused": True,
        "management_pause_reason": "EXACT_V3B_SNAPSHOT_UNAVAILABLE",
    }


def test_paused_active_trade_wick_secures_at_active_strategy_stop_without_closing(monkeypatch):
    import api
    from services import active_strategy_config_service as config

    amendments = []
    monkeypatch.setattr(config, "get_active_values", lambda **kwargs: {
        "target_rr": 1.9,
        "protection_trigger_percent": 70.0,
        "protected_stop_percent": 60.0,
    })
    monkeypatch.setattr(api, "persist_live_trade_state", lambda row: None)
    monkeypatch.setattr(api, "close_position", lambda *args, **kwargs: pytest.fail("TP2 close must remain fenced"))
    monkeypatch.setattr(api, "modify_position_stop_loss", lambda position, stop, **kwargs: (
        amendments.append((position, stop, kwargs)) or {"ok": True}
    ))
    monkeypatch.setattr(api, "read_back_broker_stop_loss", lambda row: (4332.63, {"ok": True}))

    trade = _trade(bid=4336.0, high=4338.0)
    result = api.update_live_trade_tp_protection(trade)

    assert result["tp1"] == pytest.approx(4335.31)
    assert result["protected_sl_price"] == pytest.approx(4332.63)
    assert result["protection_confirmed"] is True
    assert result["management_paused"] is True
    assert amendments == [("59231654", 4332.63, {"take_profit_price": 4343.36})]
    api.update_live_trade_tp_protection(result)
    assert len(amendments) == 1


def test_paused_trade_does_not_request_stop_after_price_retraces_below_it(monkeypatch):
    import api
    from services import active_strategy_config_service as config

    monkeypatch.setattr(config, "get_active_values", lambda **kwargs: {
        "target_rr": 1.9,
        "protection_trigger_percent": 70.0,
        "protected_stop_percent": 60.0,
    })
    monkeypatch.setattr(api, "persist_live_trade_state", lambda row: None)
    monkeypatch.setattr(api, "close_position", lambda *args, **kwargs: pytest.fail("TP2 close must remain fenced"))
    monkeypatch.setattr(api, "modify_position_stop_loss", lambda *args, **kwargs: pytest.fail("invalid stop amendment"))

    result = api.update_live_trade_tp_protection(_trade(bid=4330.56, high=4338.0))

    assert result["management_paused"] is True
    assert result.get("protection_confirmed") is not True
    assert result["sl_protection_failed"] is True


def test_paused_trade_without_tp1_wick_does_not_amend_or_close(monkeypatch):
    import api
    from services import active_strategy_config_service as config

    monkeypatch.setattr(config, "get_active_values", lambda **kwargs: {
        "target_rr": 1.9,
        "protection_trigger_percent": 70.0,
        "protected_stop_percent": 60.0,
    })
    monkeypatch.setattr(api, "persist_live_trade_state", lambda row: None)
    monkeypatch.setattr(api, "close_position", lambda *args, **kwargs: pytest.fail("unexpected close"))
    monkeypatch.setattr(api, "modify_position_stop_loss", lambda *args, **kwargs: pytest.fail("unexpected amendment"))

    result = api.update_live_trade_tp_protection(_trade(bid=4330.0, high=4331.0))

    assert result["management_paused"] is True
    assert result.get("protection_confirmed") is not True


def test_paused_sell_trade_low_wick_secures_with_active_strategy_stop(monkeypatch):
    import api
    from services import active_strategy_config_service as config

    monkeypatch.setattr(config, "get_active_values", lambda **kwargs: {
        "target_rr": 1.9, "protection_trigger_percent": 70.0,
        "protected_stop_percent": 60.0,
    })
    monkeypatch.setattr(api, "persist_live_trade_state", lambda row: None)
    monkeypatch.setattr(api, "close_position", lambda *args, **kwargs: pytest.fail("unexpected close"))
    amendments = []
    monkeypatch.setattr(api, "modify_position_stop_loss", lambda position, stop, **kwargs: (
        amendments.append((position, stop)) or {"ok": True}
    ))
    monkeypatch.setattr(api, "read_back_broker_stop_loss", lambda row: (1.188, {"ok": True}))
    trade = {
        "account_scope": "CTRADER:DEMO:47810571", "position_id": "sell-1",
        "symbol": "EURUSD", "side": "SELL", "entry": 1.2,
        "sl": 1.21, "tp2": 1.18, "bid": 1.1849, "ask": 1.185,
        "quote_timestamp": time.time(),
        "quote_account_scope": "CTRADER:DEMO:47810571",
        "current_low": 1.185, "trusted_tp1_low": 1.185,
        "management_paused": True,
    }

    result = api.update_live_trade_tp_protection(trade)

    assert result["tp1"] == pytest.approx(1.186)
    assert result["protection_confirmed"] is True
    assert amendments == [("sell-1", 1.188)]


def test_paused_trade_does_not_use_default_strategy_levels_if_config_unavailable(monkeypatch):
    import api
    from services import active_strategy_config_service as config

    monkeypatch.setattr(config, "get_active_values", lambda **kwargs: (_ for _ in ()).throw(
        config.ActiveStrategyConfigError("database unavailable")
    ))
    monkeypatch.setattr(api, "modify_position_stop_loss", lambda *args, **kwargs: pytest.fail("unexpected amendment"))
    result = api.update_live_trade_tp_protection(_trade())

    assert result.get("protection_confirmed") is not True
    assert result["management_paused"] is True


def test_protected_state_is_revoked_if_broker_stop_moves_back(monkeypatch):
    import api
    from services import active_strategy_config_service as config

    monkeypatch.setattr(config, "get_active_values", lambda **kwargs: {
        "target_rr": 1.9, "protection_trigger_percent": 70.0,
        "protected_stop_percent": 60.0,
    })
    monkeypatch.setattr(api, "persist_live_trade_state", lambda row: None)
    monkeypatch.setattr(api, "close_position", lambda *args, **kwargs: pytest.fail("unexpected close"))
    amendments = []
    monkeypatch.setattr(api, "modify_position_stop_loss", lambda position, stop, **kwargs: (
        amendments.append(stop) or {"ok": True}
    ))
    monkeypatch.setattr(api, "read_back_broker_stop_loss", lambda row: (4332.63, {"ok": True}))
    trade = _trade(bid=4336.0, high=4338.0)
    trade.update({
        "hit_tp1": True, "protection_confirmed": True,
        "sl_protection_broker_result": {"ok": True},
        "protected_sl_price": 4332.63,
        "sl": 4273.40,
    })

    result = api.update_live_trade_tp_protection(trade)

    assert result["protection_confirmed"] is True
    assert amendments == [4332.63]


def test_closed_wick_scan_uses_only_selected_account_and_post_open_candles():
    import api
    from datetime import datetime, timezone

    opened = datetime(2026, 9, 17, 11, 3, tzinfo=timezone.utc).timestamp()
    panel = {
        "_meta": {"account_scope": "CTRADER:DEMO:47810571"},
        "candles": {"XAUUSD": {"5m": [
            {"time": datetime(2026, 9, 17, 11, 0, tzinfo=timezone.utc).timestamp(),
             "high": 4444.0, "low": 4300.0},
            {"time": datetime(2026, 9, 17, 11, 5, tzinfo=timezone.utc).timestamp(),
             "high": 4338.0, "low": 4320.0},
            {"time": datetime(2026, 9, 17, 11, 10, tzinfo=timezone.utc).timestamp(),
             "high": 4330.0, "low": 4325.0},
        ]}},
    }
    now = datetime(2026, 9, 17, 11, 20, tzinfo=timezone.utc).timestamp()

    extremes = api.get_closed_panel_wick_since_open(
        "XAUUSD", panel, opened, "CTRADER:DEMO:47810571", now=now,
    )

    assert extremes == {"high": 4338.0, "low": 4320.0}
    assert api.get_closed_panel_wick_since_open(
        "XAUUSD", panel, opened, "CTRADER:DEMO:47784297", now=now,
    ) == {}


def test_latest_panel_wick_cannot_cross_account_scope():
    import api

    panel = {
        "_meta": {"account_scope": "CTRADER:DEMO:47784297"},
        "candles": {"XAUUSD": {"5m": [{"time": 1, "high": 4338.0, "low": 4320.0}]}},
    }

    assert api.get_panel_candle_extremes(
        "XAUUSD", panel, account_scope="CTRADER:DEMO:47810571",
    ) == {}


def test_restart_adopts_already_protected_broker_stop_without_amendment(monkeypatch):
    import api
    from services import active_strategy_config_service as config

    monkeypatch.setattr(config, "get_active_values", lambda **kwargs: {
        "target_rr": 1.9, "protection_trigger_percent": 70.0,
        "protected_stop_percent": 60.0,
    })
    monkeypatch.setattr(api, "persist_live_trade_state", lambda row: None)
    monkeypatch.setattr(api, "modify_position_stop_loss", lambda *args, **kwargs: pytest.fail("duplicate amendment"))
    trade = _trade(bid=4336.0, high=4338.0)
    trade["sl"] = 4332.63

    result = api.update_live_trade_tp_protection(trade)

    assert result["protection_confirmed"] is True
    assert result["sl"] == 4332.63


def test_stale_broker_quote_cannot_authorize_stop_amendment(monkeypatch):
    import api
    from services import active_strategy_config_service as config

    monkeypatch.setattr(config, "get_active_values", lambda **kwargs: {
        "target_rr": 1.9, "protection_trigger_percent": 70.0,
        "protected_stop_percent": 60.0,
    })
    monkeypatch.setattr(api, "persist_live_trade_state", lambda row: None)
    monkeypatch.setattr(api, "modify_position_stop_loss", lambda *args, **kwargs: pytest.fail("stale quote amendment"))
    trade = _trade(bid=4336.0, high=4338.0)
    trade["quote_timestamp"] = time.time() - 60

    result = api.update_live_trade_tp_protection(trade)

    assert result["sl_protection_failed"] is True
    assert result.get("protection_confirmed") is not True


def test_old_position_high_cannot_trigger_new_position_protection(monkeypatch):
    import api
    from services import active_strategy_config_service as config

    monkeypatch.setattr(config, "get_active_values", lambda **kwargs: {
        "target_rr": 1.9, "protection_trigger_percent": 70.0,
        "protected_stop_percent": 60.0,
    })
    monkeypatch.setattr(api, "modify_position_stop_loss", lambda *args, **kwargs: pytest.fail("old position wick used"))
    trade = _trade(bid=4334.0, high=4338.0)
    trade["trusted_tp1_high"] = 4331.0

    result = api.update_live_trade_tp_protection(trade)

    assert result.get("hit_tp1") is not True


@pytest.mark.parametrize("bid,ask", [(-1.0, 1.185), (1.19, 1.185), (1.0, 1.185)])
def test_malformed_fresh_quote_cannot_authorize_sell_stop(monkeypatch, bid, ask):
    import api
    from services import active_strategy_config_service as config

    monkeypatch.setattr(config, "get_active_values", lambda **kwargs: {
        "target_rr": 1.9, "protection_trigger_percent": 70.0,
        "protected_stop_percent": 60.0,
    })
    monkeypatch.setattr(api, "persist_live_trade_state", lambda row: None)
    monkeypatch.setattr(api, "modify_position_stop_loss", lambda *args, **kwargs: pytest.fail("malformed quote used"))
    trade = {
        "account_scope": "CTRADER:DEMO:47810571", "position_id": "sell-1",
        "symbol": "EURUSD", "side": "SELL", "entry": 1.2,
        "sl": 1.21, "tp2": 1.18, "bid": bid, "ask": ask,
        "quote_timestamp": time.time(),
        "quote_account_scope": "CTRADER:DEMO:47810571",
        "trusted_tp1_low": 1.185, "management_paused": True,
    }

    result = api.update_live_trade_tp_protection(trade)

    assert result["sl_protection_failed"] is True
    assert result.get("protection_confirmed") is not True


def test_latest_candle_started_before_position_open_cannot_trigger_wick():
    import api
    from datetime import datetime, timezone

    started = datetime(2026, 9, 17, 11, 0, tzinfo=timezone.utc).timestamp()
    opened = datetime(2026, 9, 17, 11, 3, tzinfo=timezone.utc).timestamp()
    panel = {
        "_meta": {"account_scope": "CTRADER:DEMO:47810571"},
        "candles": {"XAUUSD": {"5m": [{
            "time": started, "high": 4338.0, "low": 4320.0, "close": 4329.0,
        }]}},
    }

    assert api.get_panel_candle_extremes(
        "XAUUSD", panel, account_scope="CTRADER:DEMO:47810571",
        opened_at=opened,
    ) == {}
