import pandas as pd
import pytest
import ctrader_connector as connector
from test_v3b_snapshot_restore import add_snapshot, broker_position, snapshots


@pytest.fixture
def selected(monkeypatch, tmp_path):
    state = {"active_account_id": "47784297", "active_account_env": "demo",
             "_durable_selection_authoritative": True}
    monkeypatch.setattr(connector, "load_ctrader_account_settings", lambda: dict(state))
    monkeypatch.setattr(connector, "CTRADER_CANDLE_CACHE", {})
    monkeypatch.setattr(connector, "CTRADER_CANDLE_CACHE_DIR", tmp_path)
    monkeypatch.setattr(connector, "hydrate_ctrader_tokens_from_storage", lambda: {})
    for name in ("CTRADER_CLIENT_ID", "CTRADER_CLIENT_SECRET", "CTRADER_ACCESS_TOKEN"):
        monkeypatch.setenv(name, "isolated-test-value")
    return state


def test_cache_isolated_on_a_b_a_without_discarding_history(selected):
    a = connector.get_ctrader_candle_cache_key("EURUSD", "5m")
    connector.CTRADER_CANDLE_CACHE[a] = {"source": "account-a"}
    selected["active_account_id"] = "47810571"
    b = connector.get_ctrader_candle_cache_key("EURUSD", "5m")
    assert a != b
    assert connector.CTRADER_CANDLE_CACHE.get(b) is None
    selected["active_account_id"] = "47784297"
    assert connector.CTRADER_CANDLE_CACHE[connector.get_ctrader_candle_cache_key("EURUSD", "5m")]["source"] == "account-a"


def test_environment_is_part_of_cache_identity(selected):
    demo = connector.get_ctrader_candle_cache_path("EURUSD", "5m")
    selected["active_account_env"] = "live"
    assert connector.get_ctrader_candle_cache_path("EURUSD", "5m") != demo


def test_fetch_switch_does_not_publish_old_frame_under_new_selection(selected, monkeypatch):
    frame = pd.DataFrame({"Open": [1.1], "High": [1.2], "Low": [1.0],
                          "Close": [1.15], "Volume": [10]},
                         index=pd.date_range("2026-09-14T12:00Z", periods=1))
    observed = []
    def broker(config, symbol, period, limit):
        observed.append(config["account_id"])
        selected["active_account_id"] = "47810571"
        return frame
    monkeypatch.setattr(connector, "fetch_ctrader_trendbars", broker)
    monkeypatch.setattr(connector, "append_current_forming_candle", lambda data, *args: data)
    try:
        result = connector.get_ctrader_market_data("EURUSD", "5m", force_refresh=True)
    except RuntimeError as exc:
        assert "selection changed" in str(exc).lower()
    else:
        assert result.empty, "old-account frame must not reach the current panel"
    assert observed == ["47784297"]
    assert connector.get_ctrader_candle_cache_key("EURUSD", "5m") not in connector.CTRADER_CANDLE_CACHE
    assert not connector.get_ctrader_candle_cache_path("EURUSD", "5m").exists()


def test_nested_operation_config_remains_pinned_during_switch(selected):
    from ctrader_account_context import pinned_account
    with pinned_account():
        selected["active_account_id"] = "47810571"
        selected["active_account_env"] = "live"
        config = connector.get_ctrader_config()
        assert (config["account_id"], config["env"]) == ("47784297", "demo")
    assert connector.get_ctrader_config()["account_id"] == "47810571"


def test_storage_scope_uses_operation_identity_not_new_selection(selected, monkeypatch):
    from ctrader_account_context import pinned_account
    from services import indicator_stream_account_scope as scoped
    monkeypatch.setattr(scoped, "load_active_account_selection", lambda: dict(selected))
    with pinned_account():
        selected["active_account_id"] = "47810571"
        assert scoped.active_ctrader_stream_scope() == "CTRADER:DEMO:47784297"


def test_old_frame_cannot_be_rekeyed_to_new_account(selected, monkeypatch):
    from services import indicator_stream_account_scope as scoped
    from services.indicator_event_stream_service import IndicatorStreamUnavailable
    from test_indicator_stream_account_scope import _frame
    frame = _frame()
    frame.attrs["ctrader_stream_scope"] = "CTRADER:DEMO:47784297"
    calls = []
    monkeypatch.setitem(scoped._ORIGINALS, "get_authoritative_structure", lambda *a, **kw: calls.append(a) or {})
    with pytest.raises(IndicatorStreamUnavailable, match="account"):
        scoped.account_scoped_get_authoritative_structure(
            frame, "EURUSD", "5m", .00001, stream_scope="CTRADER:DEMO:47810571")
    assert not calls


def test_old_panel_is_not_published_after_switch(selected, monkeypatch):
    import api
    from ctrader_account_context import AccountSelectionChanged
    monkeypatch.setattr(api, "PANEL_CACHE", {"data": None, "last_update": 0})
    monkeypatch.setattr(api, "_panel_cache_validity", lambda data: {"valid": True, "candle_counts": {}})
    monkeypatch.setattr(api, "process_signal_email_alerts", lambda data: pytest.fail("stale panel sent alert"))
    data = {"_meta": {"account_scope": "CTRADER:DEMO:47784297"}}
    selected["active_account_id"] = "47810571"
    with pytest.raises(AccountSelectionChanged):
        api.update_panel_cache(data, "test")
    assert api.PANEL_CACHE["data"] is None


def test_execution_operation_keeps_initial_account_and_environment(selected, monkeypatch):
    import api
    from services import account_execution_coordination as coordination
    monkeypatch.setattr(coordination, "_run_normal_submission", lambda session, account, callback: callback())
    def implementation(*args, **kwargs):
        selected.update(active_account_id="47810571", active_account_env="live")
        # Same getters used by risk, lifecycle claims, and the broker adapter.
        config = connector.get_ctrader_config()
        coordination.assert_execution_account(config["account_id"])
        return {"account_id": config["account_id"], "environment": config["env"]}
    monkeypatch.setattr(api, "_execute_live_order_core_impl", implementation)
    result = api.execute_live_order_core({})
    assert result == {"account_id": "47784297", "environment": "demo"}


def test_connection_cache_reset_does_not_destroy_account_candle_history(selected):
    key = connector.get_ctrader_candle_cache_key("EURUSD", "5m")
    connector.CTRADER_CANDLE_CACHE[key] = {"data": "account-history"}
    connector.clear_ctrader_connection_cache()
    assert connector.CTRADER_CANDLE_CACHE[key]["data"] == "account-history"


def test_old_tick_cannot_form_candle_for_new_account(selected, monkeypatch):
    import time
    monkeypatch.setattr(connector, "LIVE_TICKS", {"EURUSD": {
        "bid": 1.1, "ask": 1.2, "mid": 1.15, "timestamp": time.time(),
        "account_scope": "CTRADER:DEMO:47784297"}})
    selected["active_account_id"] = "47810571"
    assert connector.get_live_tick_snapshot("EURUSD") is None


def test_same_account_selected_again_still_rejects_old_operation(selected):
    from ctrader_account_context import pinned_account, assert_current_selection, AccountSelectionChanged
    selected["selection_revision"] = "before-switch"
    with pinned_account():
        selected["selection_revision"] = "after-a-b-a"
        with pytest.raises(AccountSelectionChanged):
            assert_current_selection()


def test_dashboard_does_not_serve_old_account_panel(selected, monkeypatch):
    import api
    from routes import ctrader as route
    old = {"EURUSD": {"signal": "BUY"}, "XAUUSD": {"signal": "SELL"},
           "candles": {"EURUSD": {"5m": [{"close": 1.1}]}},
           "_meta": {"account_scope": "CTRADER:DEMO:47784297"}}
    monkeypatch.setattr(api, "PANEL_CACHE", {"data": old, "last_update": 1})
    monkeypatch.setattr(route, "_load_durable_dashboard_candles", lambda: {})
    selected["active_account_id"] = "47810571"
    result = route.nonblocking_dashboard_feed()
    assert result["EURUSD"]["signal"] == "WAIT"
    assert result["_meta"]["account_scope"] == "CTRADER:DEMO:47810571"
    assert result.get("candles") != old["candles"]


def test_other_account_profit_cannot_offset_selected_account_loss(selected, monkeypatch):
    import api
    monkeypatch.setattr(api, "run_weekly_live_reset", lambda: None)
    monkeypatch.setattr(api, "get_live_broker_closed_history", lambda **kw: [])
    monkeypatch.setattr(api, "get_live_broker_monthly_history", lambda **kw: [])
    monkeypatch.setattr(api, "log_live_trade_audit", lambda *a, **kw: None)
    monkeypatch.setattr(api, "LIVE_ACTIVE_ORDERS", {"EURUSD": {
        "account_scope": "CTRADER:DEMO:47810571", "position_id": "123", "symbol": "EURUSD",
        "status": "OPEN", "floating_pl": 1000, "pnl": 1000}})
    monkeypatch.setattr(api, "get_ctrader_account_snapshot", lambda: {
        "ok": True, "account_id": "47784297", "mode": "demo",
        "balance": 1000, "equity": 950, "balance_verified": True, "equity_verified": True})
    result = api.calculate_live_pl_sync()
    assert result["floating_live_pl"] == -50


def test_strategy_market_cache_does_not_return_other_accounts_frame(selected, monkeypatch):
    from strategies import shared
    from test_indicator_stream_account_scope import _frame
    cache = shared._empty_market_data_cache()
    for key in ("eurusd_5m", "gold_5m", "eurusd_15m", "gold_15m", "eurusd_1h", "gold_1h"):
        cache[key] = _frame()
        cache[key].attrs["ctrader_stream_scope"] = "CTRADER:DEMO:47784297"
    monkeypatch.setattr(shared.fetch_market_data, "_cache", cache, raising=False)
    monkeypatch.setattr(shared, "_FETCH_LOCK", True)
    monkeypatch.setattr(shared, "_advance_cached_market_frames", lambda cache: None)
    monkeypatch.setattr(shared, "is_market_calendar_closed", lambda: False)
    selected["active_account_id"] = "47810571"
    result = shared.fetch_market_data()
    assert all(frame.empty for frame in result), "A's strategy cache leaked into B"


def test_monthly_empty_history_never_reuses_previous_account(selected, monkeypatch):
    import api
    import time
    monkeypatch.setattr(api, "LIVE_MONTHLY_HISTORY_CACHE", {
        "account_scope": "CTRADER:DEMO:47810571", "updated_at": time.time(),
        "month_key": api.datetime.now(api.LIVE_MARKET_TIMEZONE).strftime("%Y-%m"),
        "history": [{"broker_realized_profit": 1000}]})
    monkeypatch.setattr(api, "get_closed_deals_for_current_month", lambda **kw: [])
    monkeypatch.setattr(api, "save_live_monthly_history_cache", lambda: None)
    assert api.get_live_broker_monthly_history(force=True) == []


def test_connection_snapshot_does_not_claim_previous_account_connected(selected, monkeypatch):
    import time
    monkeypatch.setattr(connector, "CTRADER_CONNECTION_CACHE", {
        "state": {"account_id": "47810571", "connected": True, "mode": "demo"},
        "checked_at": time.time()})
    monkeypatch.setattr(connector, "CONNECTED", {"account_id": "47810571", "status": True, "mode": "demo"})
    result = connector.get_ctrader_connection_snapshot()
    assert str(result["account_id"]) == "47784297"
    assert not result["execution_ready"]


def test_switch_position_sync_never_returns_other_account_orders(selected, monkeypatch):
    import api
    monkeypatch.setattr(api, "sync_ctrader_account_state", lambda **kw: None)
    monkeypatch.setattr(api, "LIVE_ACCOUNT_STATE", {"connected": False})
    monkeypatch.setattr(api, "set_auto_trade_status", lambda **kw: None)
    monkeypatch.setattr(api, "LIVE_ACTIVE_ORDERS", {"EURUSD": {
        "account_scope": "CTRADER:DEMO:47810571", "symbol": "EURUSD",
        "status": "OPEN", "position_id": "other-account"}})
    assert api.sync_live_positions() == []


def test_selection_superseded_by_another_worker_never_refreshes_wrong_account(selected, monkeypatch):
    selected["accounts"] = [{"account_id": "47784297", "env": "demo"},
                            {"account_id": "47810571", "env": "demo"}]
    monkeypatch.setattr(connector, "verify_ctrader_account_auth", lambda account_id, **kw: {"ok": True})
    def save(settings, **kw):
        selected.update(active_account_id="47810571", active_account_env="demo")
    monkeypatch.setattr(connector, "save_ctrader_account_settings", save)
    monkeypatch.setattr(connector, "update_env_file_values", lambda values: None)
    snapshots = []
    def snapshot():
        snapshots.append(connector.get_active_ctrader_account_id())
        return {"ok": True}
    monkeypatch.setattr(connector, "get_ctrader_account_snapshot", snapshot)
    try:
        result = connector.set_active_ctrader_account("47784297")
    except RuntimeError:
        pass
    else:
        assert result["ok"] is False
    assert "47810571" not in snapshots


def test_real_selection_a_b_a_persists_restart_and_preserves_auto(tmp_path, monkeypatch):
    import json
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from db import Base
    from models import RuntimeSetting
    from services import broker_account_state_service as persistence
    engine = create_engine(f"sqlite:///{tmp_path / 'selection.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as session:
        from datetime import datetime, timezone
        session.add(RuntimeSetting(setting_name="live_auto_trade_enabled", setting_value="true",
                                   updated_at=datetime.now(timezone.utc), updated_by="test"))
        session.commit()
    path = tmp_path / 'accounts.json'
    path.write_text(json.dumps({"accounts": [
        {"account_id": "47784297", "is_live": False, "env": "demo"},
        {"account_id": "47810571", "is_live": False, "env": "demo"}]}))
    monkeypatch.setattr(connector, "CTRADER_ACCOUNTS_PATH", path)
    monkeypatch.setattr(connector, "load_active_account_selection", lambda: persistence.load_active_account_selection(Session))
    monkeypatch.setattr(connector, "save_active_account_selection", lambda account, env: persistence.save_active_account_selection(account, env, Session))
    monkeypatch.setattr(connector, "update_env_file_values", lambda values: None)
    monkeypatch.setattr(connector, "CONNECTED", {})
    for name in ("ACTIVE_CTRADER_ACCOUNT_ID", "ACTIVE_CTRADER_ACCOUNT_ENV", "CTRADER_ACCOUNT_ID"):
        monkeypatch.delenv(name, raising=False)
    authenticated, refreshed = [], []
    def auth(account, config):
        authenticated.append((account, config["env"]))
        return {"ok": True}
    balances = {"47784297": 1000, "47810571": 9309.96}
    def snapshot():
        account = connector.get_active_ctrader_account_id()
        refreshed.append(account)
        return {"ok": True, "account_id": account, "balance": balances[account],
                "equity": balances[account], "balance_verified": True, "equity_verified": True}
    monkeypatch.setattr(connector, "verify_ctrader_account_auth", auth)
    monkeypatch.setattr(connector, "get_ctrader_account_snapshot", snapshot)
    def forbidden(*args, **kwargs):
        pytest.fail("account selection must not fetch/rebuild history or place orders")
    monkeypatch.setattr(connector, "fetch_ctrader_historical_candles", forbidden)
    monkeypatch.setattr(connector, "place_market_order", forbidden)
    sequence = ["47784297", "47810571", "47784297"]
    for account in sequence:
        result = connector.set_active_ctrader_account(account)
        assert result["ok"]
        assert result["fresh_account_snapshot"]["balance"] == balances[account]
        with Session() as session:
            assert session.get(RuntimeSetting, "live_auto_trade_enabled").setting_value == "true"
    assert authenticated == [(account, "demo") for account in sequence]
    assert refreshed == sequence
    engine.dispose()
    restarted = create_engine(f"sqlite:///{tmp_path / 'selection.db'}")
    restored = persistence.load_active_account_selection(sessionmaker(bind=restarted))
    assert restored["active_account_id"] == "47784297"
    assert restored["active_account_env"] == "demo"
    restarted.dispose()


def test_market_return_keeps_provenance_after_synthetic_copy(selected, monkeypatch):
    from test_indicator_stream_account_scope import _frame
    monkeypatch.setattr(connector, "fetch_ctrader_trendbars", lambda *a: _frame())
    monkeypatch.setattr(connector, "persist_ctrader_candle_cache", lambda *a: True)
    def copy_without_attrs(frame, *args):
        result = frame.copy()
        result.attrs = {}
        return result
    monkeypatch.setattr(connector, "append_current_forming_candle", copy_without_attrs)
    result = connector.get_ctrader_market_data("EURUSD", "5m", force_refresh=True)
    assert result.attrs.get("ctrader_stream_scope") == "CTRADER:DEMO:47784297"


def test_position_sync_preserves_other_account_history(selected, monkeypatch):
    import api
    from services import account_execution_coordination as coordination
    old = {"account_scope": "CTRADER:DEMO:47810571", "symbol": "EURUSD",
           "position_id": "other", "status": "RUNNING", "result": "RUNNING"}
    monkeypatch.setattr(api, "LIVE_TRADE_HISTORY", [dict(old)])
    monkeypatch.setattr(api, "LIVE_ACTIVE_ORDERS", {"EURUSD": None})
    monkeypatch.setattr(api, "LIVE_ACCOUNT_STATE", {"connected": True})
    monkeypatch.setattr(api, "sync_ctrader_account_state", lambda: None)
    monkeypatch.setattr(api, "get_open_positions", lambda: [])
    monkeypatch.setattr(api, "get_ctrader_position_fetch_error", lambda: None)
    monkeypatch.setattr(coordination, "exclude_test_positions", lambda *a: [])
    monkeypatch.setattr(api, "save_live_backup", lambda: None)
    monkeypatch.setattr(api, "invalidate_symbol_setup_state", lambda *a, **kw: pytest.fail("other account setup mutated"))
    api.sync_live_positions()
    assert api.LIVE_TRADE_HISTORY == [old]


def test_position_sync_propagates_selection_change(selected, monkeypatch):
    import api
    from ctrader_account_context import AccountSelectionChanged
    monkeypatch.setattr(api, "sync_ctrader_account_state", lambda: None)
    monkeypatch.setattr(api, "LIVE_ACCOUNT_STATE", {"connected": True})
    def positions():
        selected["active_account_id"] = "47810571"
        return []
    monkeypatch.setattr(api, "get_open_positions", positions)
    with pytest.raises(AccountSelectionChanged):
        api.sync_live_positions()


def test_live_price_api_cannot_overlay_previous_account_tick(selected, monkeypatch):
    monkeypatch.setattr(connector, "LIVE_TICKS", {"EURUSD": {
        "mid": 1.1, "timestamp": 1, "account_scope": "CTRADER:DEMO:47810571"}})
    assert connector.get_ctrader_live_price_status()["live_prices"] == {}


def test_a_b_a_old_panel_revision_cannot_publish(selected, monkeypatch):
    import api
    from ctrader_account_context import AccountSelectionChanged
    selected["selection_revision"] = "new"
    monkeypatch.setattr(api, "PANEL_CACHE", {"data": None})
    monkeypatch.setattr(api, "_panel_cache_validity", lambda data: {"valid": True, "candle_counts": {}})
    monkeypatch.setattr(api, "process_signal_email_alerts", lambda data: None)
    with pytest.raises(AccountSelectionChanged):
        api.update_panel_cache({"_meta": {"account_scope": "CTRADER:DEMO:47784297",
                                        "selection_revision": "old"}}, "test")


def test_profile_postprocessing_keeps_submission_account_pinned(selected, monkeypatch):
    from test_strategy_lab_live_v3b_runtime_install import _fake_api, _payload
    from services.live_v3b_runtime_install import install_live_v3b_runtime
    api, *_ = _fake_api()
    observed = []
    def persist(trade):
        observed.append(connector.get_active_ctrader_account_id())
    original = api.execute_live_order_core
    def execute(*args, **kw):
        result = original(*args, **kw)
        selected["active_account_id"] = "47810571"
        return result
    api.execute_live_order_core = execute
    api.persist_live_trade_state = persist
    install_live_v3b_runtime(api)
    api.execute_live_order_core(_payload())
    assert observed == ["47784297"]


def test_amendment_rejects_other_account_position(selected, monkeypatch):
    import api
    monkeypatch.setattr(api, "LIVE_ACTIVE_ORDERS", {"EURUSD": {
        "symbol": "EURUSD", "account_scope": "CTRADER:DEMO:47810571",
        "position_id": "other", "entry": 1.1}})
    monkeypatch.setattr(api, "modify_position_sltp", lambda *a, **kw: pytest.fail("wrong-account amendment"))
    result = api.modify_live_position_levels({"symbol": "EURUSD", "position_id": "other"})
    assert result["ok"] is False
    assert "account" in result["reason"].lower()


def test_existing_live_backup_restores_v3b_trade_on_same_account_restart(selected, monkeypatch, tmp_path):
    import api
    from test_strategy_lab_live_v3b_runtime_install import _payload
    monkeypatch.setattr(api, "LIVE_BACKUP_FILE", str(tmp_path / "live.json"))
    trade = {**_payload(), "account_scope": "CTRADER:DEMO:47784297", "source": "broker",
             "position_id": "42", "status": "OPEN"}
    monkeypatch.setattr(api, "LIVE_ACTIVE_ORDERS", {"EURUSD": trade, "XAUUSD": None})
    api.save_live_backup()
    api.LIVE_ACTIVE_ORDERS["EURUSD"] = None
    api.load_live_backup()
    restored = api.LIVE_ACTIVE_ORDERS["EURUSD"]
    for key in ("strategy_execution_profile", "source_indicator_event_id", "protected_sl_price",
                "no_partial_close_at_protection_trigger"):
        assert restored[key] == trade[key]
    assert restored["account_scope"] == "CTRADER:DEMO:47784297"


def test_post_close_timestamp_is_account_scoped_and_reused(selected, monkeypatch):
    import api
    monkeypatch.setattr(api, "LIVE_ACCOUNT_CLOSE_TIMES", {})
    api.set_account_closed_at("EURUSD", 1234)
    selected["active_account_id"] = "47810571"
    assert api.get_account_closed_at("EURUSD") == 0
    api.set_account_closed_at("EURUSD", 2000)
    selected["active_account_id"] = "47784297"
    assert api.get_account_closed_at("EURUSD") == 1234


def test_legacy_strategy_memory_keys_are_account_scoped(selected):
    from strategies import shared
    from ctrader_account_context import pinned_account
    with pinned_account():
        a = (shared.get_final_signal_hold_key("EURUSD"), shared.get_15m_swing_watch_key("EURUSD", "BUY"))
    selected["active_account_id"] = "47810571"
    with pinned_account():
        b = (shared.get_final_signal_hold_key("EURUSD"), shared.get_15m_swing_watch_key("EURUSD", "BUY"))
    assert all(left != right for left, right in zip(a, b))


@pytest.mark.parametrize("return_state", ["same_worker", "other_worker"])
def test_actual_position_sync_a_b_a_preserves_frozen_management(selected, monkeypatch, return_state, snapshots):
    import api
    from test_strategy_lab_live_v3b_runtime_install import _payload
    from services import account_execution_coordination as coordination
    from services import forex_observability_service as observability
    add_snapshot(snapshots)
    monkeypatch.setattr(observability, "SessionLocal", snapshots)
    trade = {**_payload(), "account_scope": "CTRADER:DEMO:47784297", "source": "broker",
             "position_id": "42", "status": "OPEN", "volume": 1000, "opened_at": 1}
    monkeypatch.setattr(api, "LIVE_ACTIVE_ORDERS", {"EURUSD": trade, "XAUUSD": None})
    monkeypatch.setattr(api, "LIVE_TRADE_HISTORY", [])
    monkeypatch.setattr(api, "LIVE_ACCOUNT_STATE", {"connected": True, "mode": "demo", "broker": "ctrader"})
    monkeypatch.setattr(api, "sync_ctrader_account_state", lambda: None)
    monkeypatch.setattr(api, "get_ctrader_position_fetch_error", lambda: None)
    monkeypatch.setattr(api, "get_live_prices", lambda: {})
    monkeypatch.setattr(api, "get_ctrader_symbol_risk_metadata", lambda *a, **kw: {})
    monkeypatch.setattr(connector, "get_ctrader_symbol_risk_metadata", lambda *a, **kw: {})
    monkeypatch.setattr(api, "get_signal_trade_plan", lambda symbol: {})
    monkeypatch.setattr(api, "save_live_backup", lambda: None)
    monkeypatch.setattr(coordination, "exclude_test_positions", lambda session, account, rows: rows)
    checked = []
    def protect(row):
        checked.append(row)
        assert row["strategy_execution_profile"] == trade["strategy_execution_profile"]
        assert row["protected_sl_price"] == trade["protected_sl_price"]
        assert row["source_indicator_event_id"] == trade["source_indicator_event_id"]
        return row
    monkeypatch.setattr(api, "update_live_trade_tp_protection", protect)
    monkeypatch.setattr(api, "get_open_positions", lambda: [])
    selected["active_account_id"] = "47810571"
    api.sync_live_positions()
    assert api.LIVE_ACTIVE_ORDERS["EURUSD"] is None
    selected["active_account_id"] = "47784297"
    monkeypatch.setattr(api, "get_open_positions", lambda: [{
        **broker_position(), "volume": 1000, "entry_price": 1.1,
        "current_price": 1.101, "sl": 1.09, "tp": 1.119, "profit": 1}])
    api.sync_live_positions()
    assert len(checked) == 1
    assert api.LIVE_ACTIVE_ORDERS["EURUSD"]["no_partial_close_at_protection_trigger"] is True
