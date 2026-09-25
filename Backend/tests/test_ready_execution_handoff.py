import copy
import io
import unittest
from contextlib import ExitStack, redirect_stdout
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import api
from ctrader_account_context import AccountIdentity
from strategies import shared


def ready_plan(symbol="XAUUSD", side="BUY"):
    identity = {
        "symbol": symbol,
        "direction": side,
        "swing_type": "HIGH" if side == "BUY" else "LOW",
        "swing_timestamp": "2026-08-10T05:45:00+00:00",
        "swing_price": 4351.99,
        "bos_candle_timestamp": "2026-08-10T07:15:00+00:00",
        "bos_level": 4351.99,
        "confirmation_timestamp": "2026-08-10T07:40:00+00:00",
    }
    plan = {
        "symbol": symbol,
        "signal": side,
        "strategy_setup_complete": True,
        "entry_price": 4358.62,
        "stop_loss": 4322.53,
        "tp1": 4394.94,
        "tp2": 4404.02,
        "risk_reward": 1.258,
        "trend_15m": {
            "trend": "BULLISH",
            "buy_allowed": True,
            "sell_allowed": False,
        },
        "setup_identity": identity,
        "fifteen_m_break_time": identity["bos_candle_timestamp"],
        "fifteen_m_break_close_time": "2026-08-10T07:30:00+00:00",
        "five_m_closed_candle_time": identity["confirmation_timestamp"],
        "fifteen_m_swing_break": {
            "side": side,
            "level": identity["bos_level"],
            "break_time": identity["bos_candle_timestamp"],
            "break_close_time": "2026-08-10T07:30:00+00:00",
            "swing": {
                "type": identity["swing_type"],
                "time": identity["swing_timestamp"],
                "price": identity["swing_price"],
            },
        },
        "confirmation_5m": {
            "side": side,
            "close_confirmed": True,
            "confirmation_close_time": identity["confirmation_timestamp"],
        },
        "consolidation": {"is_consolidation": False},
        "audit_diagnostics": {"cycle_id": "cycle-xau-ready"},
    }
    plan["signal_setup_id"] = api.get_signal_setup_id(plan, side)
    return plan


class BoundedPanelSnapshotTests(unittest.TestCase):
    def test_deep_reference_chain_is_finite_and_execution_fields_are_unchanged(self):
        plan = ready_plan()
        current = {"type": "LOW", "time": "2026-08-10T05:30:00Z", "price": 4322.03}
        for index in range(1500):
            current = {
                "type": "HIGH" if index % 2 else "LOW",
                "time": f"swing-{index}",
                "price": 4300 + index,
                "reference_swing": current,
            }
        plan["fifteen_m_swing_break"]["swing"]["reference_swing"] = current
        original_levels = tuple(plan[key] for key in ("entry_price", "stop_loss", "tp1", "tp2"))
        original_setup_id = plan["signal_setup_id"]

        snapshot = shared.bounded_panel_snapshot({"XAUUSD": plan})
        copied_plan = snapshot["XAUUSD"]
        immediate = copied_plan["fifteen_m_swing_break"]["swing"]["reference_swing"]

        self.assertNotIn("reference_swing", immediate)
        self.assertEqual(
            tuple(copied_plan[key] for key in ("entry_price", "stop_loss", "tp1", "tp2")),
            original_levels,
        )
        self.assertEqual(copied_plan["signal_setup_id"], original_setup_id)
        self.assertIn("reference_swing", plan["fifteen_m_swing_break"]["swing"])

    def test_genuine_object_cycle_is_bounded(self):
        payload = {"XAUUSD": ready_plan()}
        payload["debug_cycle"] = payload
        snapshot = shared.bounded_panel_snapshot(payload)
        self.assertEqual(snapshot["debug_cycle"], shared.PANEL_CACHE_CYCLE_MARKER)
        self.assertEqual(snapshot["XAUUSD"]["signal"], "BUY")

    def test_optional_cache_failure_does_not_escape_or_replace_previous_cache(self):
        previous = {"EURUSD": {"signal": "WAIT"}}
        shared.get_panel_data._last_open_payload = previous
        with patch.object(shared, "bounded_panel_snapshot", side_effect=RuntimeError("cache failed")):
            self.assertFalse(shared.remember_last_open_payload({"XAUUSD": ready_plan()}))
        self.assertIs(shared.get_panel_data._last_open_payload, previous)


class ReadyExecutionHandoffTests(unittest.TestCase):
    def setUp(self):
        # Runtime selection is an external boundary; do not read developer account files.
        selection = patch.object(api, 'get_active_ctrader_account_id', return_value='handoff-test')
        selection.start()
        self.addCleanup(selection.stop)
        identity = patch('ctrader_account_context.selected_identity', return_value=AccountIdentity('handoff-test', 'demo'))
        identity.start()
        self.addCleanup(identity.stop)
        # These are V3B handoff tests. Studio selection and configured RR are
        # external settings boundaries, not a dependency on a developer database.
        studio_owner = patch.object(api, "get_enabled_studio_live_owner", return_value=None)
        studio_owner.start()
        self.addCleanup(studio_owner.stop)
        rr_window = patch.object(api, "get_configured_rr_window", return_value=(1.0, 3.0))
        rr_window.start()
        self.addCleanup(rr_window.stop)
        self.auto_enabled = api.LIVE_AUTO_TRADE_ENABLED.get("enabled")
        self.account_state = copy.deepcopy(api.LIVE_ACCOUNT_STATE)
        self.trade_history = copy.deepcopy(api.LIVE_TRADE_HISTORY)
        api.LIVE_AUTO_TRADE_ENABLED["enabled"] = True
        api.LIVE_ACCOUNT_STATE.update({"connected": True, "execution_ready": True})
        api.LIVE_ORDER_IN_FLIGHT.clear()
        api.LIVE_ACTIVE_ORDERS.clear()
        api.LIVE_TRADE_HISTORY.clear()

    def tearDown(self):
        api.LIVE_AUTO_TRADE_ENABLED["enabled"] = self.auto_enabled
        api.LIVE_ACCOUNT_STATE.clear()
        api.LIVE_ACCOUNT_STATE.update(self.account_state)
        api.LIVE_ORDER_IN_FLIGHT.clear()
        api.LIVE_ACTIVE_ORDERS.clear()
        api.LIVE_TRADE_HISTORY[:] = self.trade_history

    @staticmethod
    def panel(plan=None):
        return {
            "_meta": {"account_scope": "CTRADER:DEMO:handoff-test"},
            "EURUSD": {"symbol": "EURUSD", "signal": "WAIT"},
            "XAUUSD": plan or ready_plan(),
            "candles": {},
        }

    def test_strategy_calculation_failure_blocks_before_execution(self):
        with patch.object(api, "calculate_fresh_panel_data", side_effect=RuntimeError("strategy failed")), \
                patch.object(api, "record_auto_execution_gate") as gate:
            self.assertFalse(api.refresh_panel_cache(reason="test_failure"))
        self.assertTrue(any(call.args[4] == "STRATEGY_CALCULATION_FAILED" for call in gate.call_args_list))

    def test_critical_position_sync_failure_blocks_auto(self):
        with patch.object(api, "sync_live_positions", side_effect=RuntimeError("sync failed")), \
                patch.object(api, "run_ctrader_auto_trade_checks") as auto, \
                patch.object(api, "record_auto_execution_gate") as gate:
            self.assertFalse(api.refresh_live_panel_meta(self.panel()))
        auto.assert_not_called()
        self.assertEqual(gate.call_args.args[2:5], ("POSITION_STATE_SYNC", "BLOCK", "BROKER_POSITION_SYNC_FAILED"))

    def test_display_overlay_failure_does_not_suppress_auto(self):
        with ExitStack() as stack:
            stack.enter_context(patch.object(api, "sync_live_positions", return_value=[]))
            stack.enter_context(patch.object(api, "apply_trade_signal_lifecycle"))
            stack.enter_context(patch.object(api, "apply_broker_closed_to_panel_signal_history", side_effect=RuntimeError("display failed")))
            auto = stack.enter_context(patch.object(api, "run_ctrader_auto_trade_checks"))
            stack.enter_context(patch.object(api, "update_live_trade_exit_states"))
            stack.enter_context(patch.object(api, "get_live_prices", return_value={}))
            stack.enter_context(patch.object(api, "calculate_live_pl_sync", return_value={}))
            stack.enter_context(patch.object(api, "get_live_recent_history_for_panel", return_value=[]))
            stack.enter_context(patch.object(api, "calculate_live_trade_stats", return_value={}))
            stack.enter_context(patch.object(api, "get_last_execution_time", return_value=None))
            self.assertTrue(api.refresh_live_panel_meta(self.panel()))
        auto.assert_called_once()

    def test_broker_exception_and_timeout_release_inflight_guard(self):
        for exception in (RuntimeError("broker failed"), TimeoutError("broker timed out")):
            api.LIVE_ORDER_IN_FLIGHT.add("XAUUSD")
            with patch.object(api, "place_market_order", side_effect=exception):
                with self.assertRaises(type(exception)):
                    api.place_market_order_with_inflight_cleanup("XAUUSD", action="BUY")
            self.assertNotIn("XAUUSD", api.LIVE_ORDER_IN_FLIGHT)

    def test_successful_broker_call_keeps_guard_until_core_finishes(self):
        api.LIVE_ORDER_IN_FLIGHT.add("XAUUSD")
        with patch.object(api, "place_market_order", return_value={"ok": False}) as broker:
            result = api.place_market_order_with_inflight_cleanup("XAUUSD", action="BUY")
        self.assertFalse(result["ok"])
        self.assertIn("XAUUSD", api.LIVE_ORDER_IN_FLIGHT)
        broker.assert_called_once()

    def test_duplicate_inflight_order_is_blocked_without_clearing_other_owner(self):
        plan = ready_plan()
        payload = {
            "ok": True,
            "symbol": "XAUUSD",
            "action": "BUY",
            "signal": "BUY",
            "mode": "DEMO",
            "entry": plan["entry_price"],
            "sl": plan["stop_loss"],
            "tp1": plan["tp1"],
            "tp2": plan["tp2"],
            "signal_setup_id": plan["signal_setup_id"],
            "setup_identity": plan["setup_identity"],
        }
        risk = {
            "ok": True,
            "lot_size": 0.01,
            "volume_units": 100,
            "risk_percent": 0.5,
            "risk_amount": 50.0,
            "sl_pips": 360.9,
            "account_balance": 10000.0,
            "account_equity": 10000.0,
            "account_equity_used": 10000.0,
            "symbol_metadata": {},
        }
        api.LIVE_ORDER_IN_FLIGHT.add("XAUUSD")
        with ExitStack() as stack:
            stack.enter_context(patch.object(api, "prepare_ctrader_trade", return_value=payload))
            stack.enter_context(patch.object(api, "get_signal_trade_plan", return_value=plan))
            stack.enter_context(patch.object(api, "get_live_loss_limit_status", return_value={"blocked": False}))
            stack.enter_context(patch.object(api, "calculate_live_risk_size", return_value=risk))
            stack.enter_context(patch.object(api, "calculate_expected_loss_usd_from_risk_size", return_value=50.0))
            stack.enter_context(patch.object(api, "is_expected_loss_oversized", return_value=False))
            stack.enter_context(patch.object(api, "validate_live_trade_risk_reward", return_value={"ok": True}))
            broker = stack.enter_context(patch.object(api, "place_market_order"))
            result = api.execute_live_order_core({}, source="auto")
        self.assertFalse(result["ok"])
        broker.assert_not_called()
        self.assertIn("XAUUSD", api.LIVE_ORDER_IN_FLIGHT)

    def test_xauusd_deep_ready_payload_reaches_auto_attempt(self):
        plan = ready_plan()
        reference = {"type": "LOW", "time": "anchor", "price": 4322.03}
        for index in range(1200):
            reference = {"type": "HIGH", "time": index, "price": 4300 + index, "reference_swing": reference}
        plan["fifteen_m_swing_break"]["swing"]["reference_swing"] = reference
        panel = shared.bounded_panel_snapshot(self.panel(plan))
        with ExitStack() as stack, redirect_stdout(io.StringIO()) as output:
            stack.enter_context(patch.object(api, "refresh_auto_trade_state_from_persistence"))
            stack.enter_context(patch.object(api, "sync_ctrader_account_state"))
            stack.enter_context(patch.object(api, "evaluate_news_entry_state", return_value={"allow_news_entry": False, "allow_normal_entry": True}))
            stack.enter_context(patch.object(api, "record_execution_gate_safely"))
            stack.enter_context(patch.object(api, "update_execution_outcome_safely"))
            execute = stack.enter_context(patch.object(api, "execute_live_order_core", return_value={"ok": False, "reason": "MOCK_BROKER_DISABLED"}))
            api.run_ctrader_auto_trade_checks(panel)
        execute.assert_called_once()
        self.assertEqual(execute.call_args.args[0]["entry"], 4358.62)
        self.assertIn("AUTO TRADE XAUUSD ATTEMPT", output.getvalue())
        self.assertIn("VALIDATED_STRATEGY_PAYLOAD", output.getvalue())

    def test_eurusd_wait_buy_sell_handoff_behavior_is_unchanged(self):
        for signal, expected_calls in (("WAIT", 0), ("BUY", 1), ("SELL", 1)):
            plan = ready_plan("EURUSD", signal if signal != "WAIT" else "BUY")
            plan["signal"] = signal
            panel = shared.bounded_panel_snapshot({"EURUSD": plan, "XAUUSD": {"symbol": "XAUUSD", "signal": "WAIT"}, "candles": {}})
            self.assertEqual(panel["EURUSD"]["signal"], signal)
            with ExitStack() as stack:
                stack.enter_context(patch.object(api, "refresh_auto_trade_state_from_persistence"))
                stack.enter_context(patch.object(api, "sync_ctrader_account_state"))
                stack.enter_context(patch.object(api, "evaluate_news_entry_state", return_value={"allow_news_entry": False, "allow_normal_entry": True}))
                stack.enter_context(patch.object(api, "record_execution_gate_safely"))
                stack.enter_context(patch.object(api, "update_execution_outcome_safely"))
                execute = stack.enter_context(patch.object(api, "execute_live_order_core", return_value={"ok": False, "reason": "MOCK"}))
                api.run_ctrader_auto_trade_checks(panel)
            self.assertEqual(execute.call_count, expected_calls)

    def test_active_trade_blocks_new_buy_or_sell_without_suppressing_strategy_signal(self):
        for signal in ("BUY", "SELL"):
            api.LIVE_ACTIVE_ORDERS["EURUSD"] = {
                "symbol": "EURUSD",
                "side": "BUY",
                "status": "RUNNING",
                "broker_position_id": "position-123",
                "signal_setup_id": "original-position-setup",
            }
            plan = ready_plan("EURUSD", signal)
            panel = {
                "EURUSD": plan,
                "XAUUSD": {"symbol": "XAUUSD", "signal": "WAIT"},
                "candles": {},
            }
            api.apply_trade_signal_lifecycle(panel)

            self.assertEqual(panel["EURUSD"]["signal"], signal)
            self.assertEqual(panel["EURUSD"]["strategy_decision"], signal)
            self.assertFalse(panel["EURUSD"]["execution_allowed"])
            self.assertEqual(
                panel["EURUSD"]["execution_block_reason"],
                "ACTIVE_TRADE_ALREADY_RUNNING",
            )
            self.assertEqual(panel["EURUSD"]["active_trade_direction"], "BUY")
            self.assertEqual(panel["EURUSD"]["active_trade_id"], "position-123")

            with ExitStack() as stack:
                stack.enter_context(patch.object(api, "refresh_auto_trade_state_from_persistence"))
                stack.enter_context(patch.object(api, "sync_ctrader_account_state"))
                stack.enter_context(patch.object(api, "evaluate_news_entry_state", return_value={"allow_news_entry": False, "allow_normal_entry": True}))
                stack.enter_context(patch.object(api, "record_execution_gate_safely"))
                stack.enter_context(patch.object(api, "update_execution_outcome_safely"))
                execute = stack.enter_context(patch.object(api, "execute_live_order_core"))
                results = api.run_ctrader_auto_trade_checks(panel)

            execute.assert_not_called()
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["strategy_decision"], signal)
            self.assertFalse(results[0]["execution_allowed"])
            self.assertEqual(
                results[0]["execution_block_reason"],
                "ACTIVE_TRADE_ALREADY_RUNNING",
            )
            self.assertEqual(panel["EURUSD"]["signal"], "WAIT")
            self.assertEqual(panel["EURUSD"]["smc_progress"], 0)
            self.assertEqual(panel["EURUSD"]["smc_status"], "WAIT")
            self.assertTrue(panel["EURUSD"]["trade_setup_consumed"])
            consumed = api.LIVE_ACTIVE_ORDERS["EURUSD"]["consumed_signal_setups"]
            self.assertEqual(len(consumed), 1)
            self.assertEqual(consumed[0]["side"], signal)
            self.assertEqual(consumed[0]["signal_setup_id"], plan["signal_setup_id"])

            refreshed_plan = ready_plan("EURUSD", signal)
            refreshed_panel = {
                "EURUSD": refreshed_plan,
                "XAUUSD": {"symbol": "XAUUSD", "signal": "WAIT"},
                "candles": {},
            }
            api.apply_trade_signal_lifecycle(refreshed_panel)
            self.assertEqual(refreshed_panel["EURUSD"]["signal"], "WAIT")
            self.assertEqual(refreshed_panel["EURUSD"]["smc_progress"], 0)
            self.assertTrue(refreshed_panel["EURUSD"]["trade_already_running"])

    def test_setup_that_opened_running_trade_resets_plan_immediately(self):
        plan = ready_plan("EURUSD", "BUY")
        plan["executed_trade_setup_snapshot"] = {
            "symbol": "EURUSD",
            "direction": "BUY",
            "broker_position_id": "position-123",
            "status": "RUNNING",
        }
        plan["smc_plan_state_source"] = "executed_trade_setup_snapshot"
        api.LIVE_ACTIVE_ORDERS["EURUSD"] = {
            "symbol": "EURUSD",
            "side": "BUY",
            "status": "RUNNING",
            "broker_position_id": "position-123",
            "signal_setup_id": plan["signal_setup_id"],
        }
        panel = {
            "EURUSD": plan,
            "XAUUSD": {"symbol": "XAUUSD", "signal": "WAIT"},
            "candles": {},
        }

        api.apply_trade_signal_lifecycle(panel)

        self.assertEqual(panel["EURUSD"]["signal"], "WAIT")
        self.assertEqual(panel["EURUSD"]["smc_progress"], 0)
        self.assertEqual(panel["EURUSD"]["smc_status"], "WAIT")
        self.assertTrue(panel["EURUSD"]["trade_already_running"])
        self.assertTrue(panel["EURUSD"]["trade_setup_consumed"])
        self.assertNotIn("executed_trade_setup_snapshot", panel["EURUSD"])
        self.assertNotIn("smc_plan_state_source", panel["EURUSD"])

    def test_active_trade_consumption_is_idempotent_and_bounded(self):
        api.LIVE_ACTIVE_ORDERS["EURUSD"] = {
            "symbol": "EURUSD",
            "side": "BUY",
            "status": "RUNNING",
            "broker_position_id": "position-123",
        }
        plan = ready_plan("EURUSD", "SELL")

        first = api.consume_signal_setup_for_active_trade("EURUSD", plan, "SELL")
        second = api.consume_signal_setup_for_active_trade("EURUSD", plan, "SELL")

        self.assertEqual(first, second)
        self.assertEqual(
            len(api.LIVE_ACTIVE_ORDERS["EURUSD"]["consumed_signal_setups"]),
            1,
        )

    def test_setup_consumed_while_active_stays_reset_after_trade_closes(self):
        active_trade = {
            "symbol": "EURUSD",
            "side": "BUY",
            "status": "RUNNING",
            "broker_position_id": "position-123",
        }
        api.LIVE_ACTIVE_ORDERS["EURUSD"] = active_trade
        plan = ready_plan("EURUSD", "SELL")
        api.consume_signal_setup_for_active_trade("EURUSD", plan, "SELL")

        closed_trade = copy.deepcopy(api.LIVE_ACTIVE_ORDERS["EURUSD"])
        closed_trade.update({"status": "CLOSED", "result": "WIN"})
        api.LIVE_ACTIVE_ORDERS.clear()
        api.LIVE_TRADE_HISTORY.insert(0, closed_trade)

        refreshed_panel = {
            "EURUSD": ready_plan("EURUSD", "SELL"),
            "XAUUSD": {"symbol": "XAUUSD", "signal": "WAIT"},
            "candles": {},
        }
        api.apply_trade_signal_lifecycle(refreshed_panel)

        self.assertEqual(refreshed_panel["EURUSD"]["signal"], "WAIT")
        self.assertEqual(refreshed_panel["EURUSD"]["smc_progress"], 0)
        self.assertFalse(refreshed_panel["EURUSD"]["trade_already_running"])
        self.assertTrue(refreshed_panel["EURUSD"]["trade_setup_consumed"])

    def test_active_trade_with_no_valid_setup_remains_normal_wait(self):
        api.LIVE_ACTIVE_ORDERS["EURUSD"] = {
            "symbol": "EURUSD",
            "side": "BUY",
            "status": "RUNNING",
            "broker_position_id": "position-123",
        }
        plan = ready_plan("EURUSD", "BUY")
        plan["signal"] = "WAIT"
        panel = {
            "EURUSD": plan,
            "XAUUSD": {"symbol": "XAUUSD", "signal": "WAIT"},
            "candles": {},
        }
        api.apply_trade_signal_lifecycle(panel)
        self.assertEqual(panel["EURUSD"]["signal"], "WAIT")
        self.assertEqual(panel["EURUSD"]["strategy_decision"], "WAIT")
        self.assertEqual(panel["EURUSD"]["execution_status"], "NOT_APPLICABLE")
        self.assertIsNone(panel["EURUSD"]["execution_block_reason"])

        with ExitStack() as stack:
            stack.enter_context(patch.object(api, "refresh_auto_trade_state_from_persistence"))
            stack.enter_context(patch.object(api, "sync_ctrader_account_state"))
            stack.enter_context(patch.object(api, "evaluate_news_entry_state", return_value={"allow_news_entry": False, "allow_normal_entry": True}))
            execute = stack.enter_context(patch.object(api, "execute_live_order_core"))
            results = api.run_ctrader_auto_trade_checks(panel)
        execute.assert_not_called()
        self.assertEqual(results, [])

    def test_august_10_0740_ready_setup_passes_full_stack_to_mocked_broker(self):
        plan = ready_plan()
        payload = {
            "ok": True,
            "symbol": "XAUUSD",
            "action": "BUY",
            "signal": "BUY",
            "mode": "DEMO",
            "entry": plan["entry_price"],
            "sl": plan["stop_loss"],
            "tp1": plan["tp1"],
            "tp2": plan["tp2"],
            "signal_setup_id": plan["signal_setup_id"],
            "setup_identity": copy.deepcopy(plan["setup_identity"]),
            "fifteen_m_break_close_time": plan["fifteen_m_break_close_time"],
            "five_m_confirmation_close_time": plan["five_m_closed_candle_time"],
            "trend_15m": copy.deepcopy(plan["trend_15m"]),
        }
        risk = {
            "ok": True,
            "lot_size": 0.01,
            "volume_units": 100,
            "risk_percent": 0.5,
            "risk_amount": 50.0,
            "sl_pips": 360.9,
            "account_balance": 10000.0,
            "account_equity": 10000.0,
            "account_equity_used": 10000.0,
            "symbol_metadata": {},
        }
        historical_now = datetime(2026, 8, 10, 7, 41, tzinfo=timezone.utc).timestamp()
        broker_response = {"ok": False, "reason": "MOCK_BROKER_DISABLED"}

        with ExitStack() as stack, redirect_stdout(io.StringIO()) as output:
            stack.enter_context(patch.object(api, "refresh_auto_trade_state_from_persistence"))
            stack.enter_context(patch.object(api, "sync_ctrader_account_state"))
            stack.enter_context(patch.object(api, "evaluate_news_entry_state", return_value={"allow_news_entry": False, "allow_normal_entry": True}))
            stack.enter_context(patch.object(api, "prepare_ctrader_trade", return_value=copy.deepcopy(payload)))
            stack.enter_context(patch.object(api, "get_signal_trade_plan", return_value=plan))
            stack.enter_context(patch.object(api, "get_live_loss_limit_status", return_value={"blocked": False}))
            stack.enter_context(patch.object(api, "calculate_live_risk_size", return_value=risk))
            stack.enter_context(patch.object(api, "calculate_expected_loss_usd_from_risk_size", return_value=50.0))
            stack.enter_context(patch.object(api, "is_expected_loss_oversized", return_value=False))
            stack.enter_context(patch.object(api, "validate_live_trade_risk_reward", return_value={"ok": True, "rr": 1.258}))
            stack.enter_context(patch.object(api, "get_open_positions", return_value=[]))
            stack.enter_context(patch.object(api, "validate_fresh_ema_permission_locked", return_value={"ok": True, "details": {}}))
            stack.enter_context(patch.object(api, "check_live_market_data_health", return_value={"ok": True}))
            broker = stack.enter_context(patch.object(api, "place_market_order", return_value=broker_response))
            stack.enter_context(patch.object(api, "record_execution_gate_safely"))
            stack.enter_context(patch.object(api, "update_execution_outcome_safely"))
            stack.enter_context(patch.object(api.time, "time", return_value=historical_now))
            result = api.run_ctrader_auto_trade_checks(self.panel(plan))
            broker.side_effect = TimeoutError("mock broker timeout")
            with self.assertRaises(TimeoutError):
                api.execute_live_order_core({}, source="auto")

        self.assertEqual(len(result), 1)
        self.assertFalse(result[0]["ok"])
        self.assertEqual(result[0]["reason"], "MOCK_BROKER_DISABLED")
        self.assertIn("AUTO TRADE XAUUSD ATTEMPT", output.getvalue())
        self.assertIn("LIVE_AUTO_FINAL_ENTRY_GATE", output.getvalue())
        self.assertIn("LIVE_FRESH_EMA_FINAL_GATE", output.getvalue())
        self.assertIn("LIVE_ORDER_PROTECTION_AUDIT", output.getvalue())
        self.assertIn("before_place_market_order", output.getvalue())
        self.assertNotIn("XAUUSD", api.LIVE_ORDER_IN_FLIGHT)


if __name__ == "__main__":
    unittest.main()
