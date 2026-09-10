import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from models import Base, RuntimeSetting
from services import strategy_settings_service as settings_service
from strategies import shared, strict_trader

# Keep these unit tests aimed at the generic strict-trader functions. Importing
# api activates brain.py's production XAUUSD adapters process-wide.
ORIGINAL_EVALUATE_15M_BREAKOUT = strict_trader.evaluate_15m_breakout
ORIGINAL_BUILD_RISK_LEVELS = strict_trader.build_risk_levels

import api


class StrategySettingsExecutionCacheTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        settings_service.invalidate_execution_settings_cache(self.sessions)

    def tearDown(self):
        settings_service.invalidate_execution_settings_cache(self.sessions)
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def test_missing_rows_use_exact_production_defaults(self):
        self.assertEqual(
            settings_service.get_cached_execution_settings(
                self.sessions, force_refresh=True
            ),
            {
                "minimum_rr": 1.2,
                "maximum_rr": 2.0,
                "bos_buffer_points": 10,
                "minimum_sl_distance_points": 100,
                "post_trade_cooldown_minutes": 15,
                "consolidation_filter_enabled": True,
            },
        )

    def test_malformed_or_unavailable_store_uses_complete_safe_defaults(self):
        now = datetime(2026, 8, 11, tzinfo=timezone.utc)
        with self.sessions() as session:
            session.add(RuntimeSetting(
                setting_name="strategy.minimum_rr",
                setting_value="not-json",
                updated_at=now,
                updated_by="test",
            ))
            session.add(RuntimeSetting(
                setting_name="strategy.maximum_rr",
                setting_value="3.0",
                updated_at=now,
                updated_by="test",
            ))
            session.commit()
        self.assertEqual(
            settings_service.get_cached_execution_settings(
                self.sessions, force_refresh=True
            ),
            settings_service.execution_defaults(),
        )

        def unavailable_factory():
            raise RuntimeError("database unavailable")

        settings_service.invalidate_execution_settings_cache(unavailable_factory)
        self.assertEqual(
            settings_service.get_cached_execution_settings(
                unavailable_factory, force_refresh=True
            ),
            settings_service.execution_defaults(),
        )

    def test_malformed_bos_or_sl_value_never_creates_partial_configuration(self):
        now = datetime(2026, 8, 11, tzinfo=timezone.utc)
        for setting_name, setting_value in (
            ("strategy.bos_buffer_points", '"not-a-number"'),
            ("strategy.minimum_sl_distance_points", "null"),
            ("strategy.consolidation_filter_enabled", '"not-a-boolean"'),
        ):
            with self.subTest(setting_name=setting_name):
                with self.sessions() as session:
                    session.query(RuntimeSetting).delete()
                    session.add(RuntimeSetting(
                        setting_name=setting_name,
                        setting_value=setting_value,
                        updated_at=now,
                        updated_by="test",
                    ))
                    session.commit()
                settings_service.invalidate_execution_settings_cache(self.sessions)
                self.assertEqual(
                    settings_service.get_cached_execution_settings(
                        self.sessions,
                        force_refresh=True,
                    ),
                    settings_service.execution_defaults(),
                )

    def test_failed_reads_use_five_second_failure_cache(self):
        def unavailable_factory():
            raise RuntimeError("database unavailable")

        settings_service.invalidate_execution_settings_cache(unavailable_factory)
        with patch.object(
            settings_service,
            "_read_execution_settings",
            side_effect=RuntimeError("database unavailable"),
        ) as reader:
            settings_service.get_cached_execution_settings(
                unavailable_factory,
                force_refresh=True,
                monotonic_now=0,
            )
            settings_service.get_cached_execution_settings(
                unavailable_factory,
                monotonic_now=4,
            )
            self.assertEqual(reader.call_count, 1)
            settings_service.get_cached_execution_settings(
                unavailable_factory,
                monotonic_now=6,
            )
            self.assertEqual(reader.call_count, 2)

    def test_cache_avoids_cycle_reads_and_save_reset_refresh_it(self):
        with patch.object(
            settings_service,
            "_read_execution_settings",
            wraps=settings_service._read_execution_settings,
        ) as reader:
            first = settings_service.get_cached_execution_settings(
                self.sessions, force_refresh=True
            )
            second = settings_service.get_cached_execution_settings(self.sessions)
            self.assertEqual(first, second)
            self.assertEqual(reader.call_count, 1)

            settings_service.save_strategy_settings(
                {"minimum_rr": 1.4},
                updated_by="owner",
                session_factory=self.sessions,
            )
            self.assertEqual(
                settings_service.get_cached_execution_settings(self.sessions)["minimum_rr"],
                1.4,
            )
            self.assertEqual(reader.call_count, 1)

            settings_service.reset_strategy_settings(
                confirmed=True,
                updated_by="owner",
                session_factory=self.sessions,
            )
            self.assertEqual(
                settings_service.get_cached_execution_settings(self.sessions),
                settings_service.execution_defaults(),
            )
            self.assertEqual(reader.call_count, 1)

    def test_cache_refreshes_after_thirty_second_ttl(self):
        settings_service.invalidate_execution_settings_cache(self.sessions)
        with patch.object(
            settings_service,
            "_read_execution_settings",
            wraps=settings_service._read_execution_settings,
        ) as reader:
            settings_service.get_cached_execution_settings(
                self.sessions, force_refresh=True, monotonic_now=0
            )
            settings_service.get_cached_execution_settings(
                self.sessions, monotonic_now=29
            )
            self.assertEqual(reader.call_count, 1)
            settings_service.get_cached_execution_settings(
                self.sessions, monotonic_now=31
            )
            self.assertEqual(reader.call_count, 2)

    def test_concurrent_cache_miss_cannot_overwrite_completed_save(self):
        started = threading.Event()
        release = threading.Event()

        def slow_read(_factory):
            started.set()
            self.assertTrue(release.wait(timeout=3))
            return settings_service.execution_defaults()

        settings_service.invalidate_execution_settings_cache(self.sessions)
        with patch.object(settings_service, "_read_execution_settings", side_effect=slow_read):
            with ThreadPoolExecutor(max_workers=2) as executor:
                reader = executor.submit(
                    settings_service.get_cached_execution_settings,
                    self.sessions,
                    force_refresh=True,
                )
                self.assertTrue(started.wait(timeout=3))
                writer = executor.submit(
                    settings_service.save_strategy_settings,
                    {"minimum_rr": 1.4},
                    "owner",
                    self.sessions,
                )
                release.set()
                self.assertEqual(reader.result(timeout=3)["minimum_rr"], 1.2)
                self.assertEqual(writer.result(timeout=3)["current"]["minimum_rr"], 1.4)

        self.assertEqual(
            settings_service.get_cached_execution_settings(self.sessions)["minimum_rr"],
            1.4,
        )


class StrategySettingsHistoryEndpointTests(unittest.TestCase):
    def tearDown(self):
        api.SESSIONS.pop("strategy-history-test", None)

    def test_history_endpoint_requires_authentication(self):
        request = SimpleNamespace(headers={})
        with self.assertRaises(HTTPException) as caught:
            api.get_strategy_settings_history_endpoint(request)
        self.assertEqual(caught.exception.status_code, 401)

    def test_history_endpoint_is_authenticated_and_read_only(self):
        api.SESSIONS["strategy-history-test"] = {
            "email": "owner@example.com",
            "role": "owner",
        }
        request = SimpleNamespace(headers={
            "authorization": "Bearer strategy-history-test",
        })
        expected = {
            "items": [], "count": 0, "total": 0,
            "limit": 25, "offset": 0, "read_only": True,
        }
        with patch.object(api, "get_strategy_settings_history", return_value=expected) as reader:
            result = api.get_strategy_settings_history_endpoint(
                request, limit=25, offset=0
            )
        self.assertEqual(result, expected)
        reader.assert_called_once_with(limit=25, offset=0)


class StrategySettingsRRWiringTests(unittest.TestCase):
    @staticmethod
    def swing(swing_type, price):
        return {"type": swing_type, "price": price, "time": "2026-08-11T12:00:00Z"}

    def test_default_buy_sell_and_wait_parity(self):
        with patch.object(strict_trader, "get_configured_rr_window", return_value=(1.2, 2.0)):
            default_results = [
                strict_trader.select_tp2(
                    [self.swing(swing_type, price)], side, 100.0, 10.0, "XAUUSD"
                )
                for side, swing_type, price in (
                    ("BUY", "HIGH", 111.0),
                    ("BUY", "HIGH", 112.0),
                    ("BUY", "HIGH", 125.0),
                    ("SELL", "LOW", 89.0),
                    ("SELL", "LOW", 88.0),
                    ("SELL", "LOW", 75.0),
                )
            ]
            wait_default = strict_trader.get_mtf_signal(
                pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), "XAUUSD"
            )
        explicit_results = [
            strict_trader.select_tp2(
                [self.swing(swing_type, price)], side, 100.0, 10.0,
                "XAUUSD", 1.2, 2.0,
            )
            for side, swing_type, price in (
                ("BUY", "HIGH", 111.0),
                ("BUY", "HIGH", 112.0),
                ("BUY", "HIGH", 125.0),
                ("SELL", "LOW", 89.0),
                ("SELL", "LOW", 88.0),
                ("SELL", "LOW", 75.0),
            )
        ]
        self.assertEqual(default_results, explicit_results)
        self.assertEqual(
            [result["rr"] for result in default_results],
            [2.0, 1.2, 2.0, 2.0, 1.2, 2.0],
        )
        self.assertEqual(wait_default["signal"], "WAIT")

    def test_minimum_rr_values_change_only_acceptance_threshold(self):
        candidate = [self.swing("HIGH", 113.0)]
        expected = {
            1.0: (1.3, "inverse_15m_swing"),
            1.4: (2.0, "fallback_2r"),
            2.0: (2.0, "fallback_2r"),
        }
        for minimum_rr, (expected_rr, source) in expected.items():
            with self.subTest(minimum_rr=minimum_rr):
                result = strict_trader.select_tp2(
                    candidate, "BUY", 100.0, 10.0, "XAUUSD",
                    minimum_rr, 2.0,
                )
                self.assertAlmostEqual(result["rr"], expected_rr)
                self.assertEqual(result["source"], source)

    def test_maximum_rr_values_change_only_existing_cap(self):
        candidate = [self.swing("HIGH", 125.0)]
        expected = {
            1.5: (1.5, "fallback_1.5r"),
            2.0: (2.0, "fallback_2r"),
            3.0: (2.5, "inverse_15m_swing"),
        }
        for maximum_rr, (expected_rr, source) in expected.items():
            with self.subTest(maximum_rr=maximum_rr):
                result = strict_trader.select_tp2(
                    candidate, "BUY", 100.0, 10.0, "XAUUSD",
                    1.0, maximum_rr,
                )
                self.assertAlmostEqual(result["rr"], expected_rr)
                self.assertEqual(result["source"], source)

    def test_final_live_rr_gate_uses_same_window(self):
        cases = [
            ((1.2, 2.0), 1.2, True),
            ((1.2, 2.0), 1.1, False),
            ((1.2, 2.0), 2.1, False),
            ((1.0, 2.0), 1.1, True),
            ((1.4, 2.0), 1.3, False),
            ((1.0, 1.5), 2.0, False),
            ((1.0, 3.0), 2.5, True),
        ]
        for rr_window, rr, expected_ok in cases:
            with self.subTest(rr_window=rr_window, rr=rr):
                with patch.object(api, "get_configured_rr_window", return_value=rr_window):
                    result = api.validate_live_trade_risk_reward(
                        "XAUUSD", "BUY", 100.0, 90.0, 100.0 + (10.0 * rr)
                    )
                self.assertEqual(result["ok"], expected_ok)


class StrategySettingsStructureWiringTests(unittest.TestCase):
    @staticmethod
    def atr_frame(symbol, true_range, rows=20):
        base = 100.0 if symbol == "XAUUSD" else 1.10000
        index = pd.date_range("2026-06-01", periods=rows, freq="15min", tz="UTC")
        return pd.DataFrame(
            {
                "Open": [base] * rows,
                "High": [base + true_range] * rows,
                "Low": [base] * rows,
                "Close": [base + (true_range / 2.0)] * rows,
            },
            index=index,
        )

    @staticmethod
    def breakout_frame(side, close_price):
        index = pd.date_range("2026-06-01", periods=20, freq="15min", tz="UTC")
        frame = pd.DataFrame(
            {
                "Open": [95.0] * 20,
                "High": [96.0] * 20,
                "Low": [94.0] * 20,
                "Close": [95.0] * 20,
            },
            index=index,
        )
        if side == "BUY":
            frame.iloc[-2, frame.columns.get_loc("Close")] = 99.0
            frame.iloc[-1] = [100.0, close_price + 0.05, 99.5, close_price]
        else:
            frame.iloc[-2, frame.columns.get_loc("Close")] = 91.0
            frame.iloc[-1] = [90.0, 90.5, close_price - 0.05, close_price]
        return frame

    @staticmethod
    def swings():
        return [
            {"type": "LOW", "price": 90.0, "time": "2026-06-01T01:00:00Z", "index": 4, "valid": True},
            {"type": "HIGH", "price": 100.0, "time": "2026-06-01T02:00:00Z", "index": 8, "valid": True},
        ]

    def test_default_bos_formula_matches_previous_production_for_both_symbols(self):
        for symbol, true_range in (("EURUSD", 0.0004), ("XAUUSD", 0.4)):
            with self.subTest(symbol=symbol):
                frame = self.atr_frame(symbol, true_range)
                previous = max(
                    strict_trader.BOS_MIN_BUFFER_POINTS * strict_trader.point_size(symbol),
                    0.10 * strict_trader.atr14(frame),
                )
                self.assertAlmostEqual(
                    strict_trader.bos_buffer(frame, symbol, 10),
                    previous,
                )

    def test_bos_floor_values_and_atr_dominance(self):
        quiet = self.atr_frame("XAUUSD", 0.02)
        for points in (5, 10, 25, 50):
            with self.subTest(points=points):
                self.assertAlmostEqual(
                    strict_trader.bos_buffer(quiet, "XAUUSD", points),
                    points * 0.01,
                )
        volatile = self.atr_frame("XAUUSD", 10.0)
        self.assertAlmostEqual(
            strict_trader.bos_buffer(volatile, "XAUUSD", 50),
            1.0,
        )

    def test_buy_sell_and_choch_use_the_same_configured_floor(self):
        settings = {"bos_buffer_points": 25}
        with patch.object(strict_trader, "atr14", return_value=0.0), patch.object(
            strict_trader, "detect_raw_swings", return_value=self.swings()
        ), patch.object(strict_trader, "clear_opposite_watch"):
            buy = ORIGINAL_EVALUATE_15M_BREAKOUT(
                self.breakout_frame("BUY", 100.26),
                "XAUUSD",
                execution_settings=settings,
            )
            sell = ORIGINAL_EVALUATE_15M_BREAKOUT(
                self.breakout_frame("SELL", 89.74),
                "XAUUSD",
                execution_settings=settings,
            )
        self.assertEqual((buy["side"], buy["break_type"]), ("BUY", "CHOCH"))
        self.assertEqual((sell["side"], sell["break_type"]), ("SELL", "CHOCH"))
        self.assertAlmostEqual(buy["bos_buffer"], 0.25)
        self.assertAlmostEqual(sell["bos_buffer"], 0.25)

    def test_stricter_floor_blocks_weak_close_and_wick_only_remains_rejected(self):
        with patch.object(strict_trader, "atr14", return_value=0.0), patch.object(
            strict_trader, "detect_raw_swings", return_value=self.swings()
        ), patch.object(strict_trader, "clear_opposite_watch"):
            accepted = ORIGINAL_EVALUATE_15M_BREAKOUT(
                self.breakout_frame("BUY", 100.11),
                "XAUUSD",
                execution_settings={"bos_buffer_points": 10},
            )
            rejected = ORIGINAL_EVALUATE_15M_BREAKOUT(
                self.breakout_frame("BUY", 100.11),
                "XAUUSD",
                execution_settings={"bos_buffer_points": 25},
            )
            wick_only = ORIGINAL_EVALUATE_15M_BREAKOUT(
                self.breakout_frame("BUY", 100.0),
                "XAUUSD",
                execution_settings={"bos_buffer_points": 5},
            )
        self.assertEqual(accepted["side"], "BUY")
        self.assertEqual(rejected["side"], "WAIT")
        self.assertEqual(wick_only["side"], "WAIT")

    def test_minimum_sl_values_change_only_distance_acceptance_symmetrically(self):
        buy_swings = [{"type": "LOW", "price": 99.1, "valid": True}]
        sell_swings = [{"type": "HIGH", "price": 100.9, "valid": True}]
        expected = {50: True, 100: True, 150: False, 250: False}
        for minimum_points, expected_ok in expected.items():
            with self.subTest(minimum_points=minimum_points):
                buy = strict_trader.select_stop_loss(
                    buy_swings, "BUY", 100.0, "XAUUSD", minimum_points
                )
                sell = strict_trader.select_stop_loss(
                    sell_swings, "SELL", 100.0, "XAUUSD", minimum_points
                )
                self.assertEqual(buy["ok"], expected_ok)
                self.assertEqual(sell["ok"], expected_ok)
                if not expected_ok:
                    self.assertEqual(buy["reason"], "WAIT_SL_TOO_SMALL")
                    self.assertEqual(sell["reason"], "WAIT_SL_TOO_SMALL")

    def test_one_cached_snapshot_is_passed_into_bos_evaluation(self):
        frame_15m = self.atr_frame("XAUUSD", 0.4, rows=25)
        settings = settings_service.execution_defaults()
        no_breakout = {
            "side": None,
            "reason": "WAIT_NO_VALID_SWING",
            "swings": [],
        }
        with patch.object(
            strict_trader,
            "get_cached_execution_settings",
            return_value=settings,
        ) as cached, patch.object(
            strict_trader,
            "trend_filter",
            return_value={"trend": "NEUTRAL", "buy_allowed": False, "sell_allowed": False},
        ), patch.object(
            strict_trader,
            "classify_consolidation",
            return_value={"is_consolidation": False},
        ), patch.object(
            strict_trader,
            "evaluate_15m_breakout",
            return_value=no_breakout,
        ) as breakout:
            strict_trader.get_mtf_signal(
                pd.DataFrame(),
                frame_15m,
                pd.DataFrame(),
                "XAUUSD",
            )
        cached.assert_called_once_with()
        self.assertIs(
            breakout.call_args.kwargs["execution_settings"],
            settings,
        )

    def test_consolidation_setting_only_changes_final_permission(self):
        frame_15m = self.atr_frame("XAUUSD", 0.4, rows=25)
        frame_5m = self.atr_frame("XAUUSD", 0.1, rows=30)
        breakout = {
            "side": "BUY",
            "reason": None,
            "level": 100.0,
            "break_time": "2026-06-01T05:30:00Z",
            "break_close_time": "2026-06-01T05:45:00Z",
            "break_close": 100.5,
            "break_type": "CHOCH",
            "remembered": False,
            "swings": self.swings(),
            "indicator_event_id": "smc1-consolidation-event",
            "indicator_event_identity": {"symbol": "XAUUSD"},
        }
        confirmation = {
            "side": "BUY",
            "close": 100.6,
            "close_confirmed": True,
            "confirmation_close_time": "2026-06-01T05:50:00Z",
        }
        levels = {
            "ok": True,
            "entry": 100.6,
            "stop_loss": 99.0,
            "tp1": 102.52,
            "tp2": 103.0,
            "protected_sl_price": 101.8,
            "risk_reward": "1:1.50",
            "risk_reward_ratio": 1.5,
            "tp1_rule": "80%",
            "tp_structure_source": "test",
            "protected_sl_rule": "50%",
        }
        common = [
            patch.object(strict_trader, "trend_filter", return_value={
                "trend": "BULLISH", "buy_allowed": True, "sell_allowed": False,
            }),
            patch.object(strict_trader, "classify_consolidation", return_value={
                "is_consolidation": True, "conditions_met": 2,
            }),
            patch.object(strict_trader, "evaluate_15m_breakout", return_value=breakout),
            patch.object(strict_trader, "confirm_5m", return_value=confirmation),
            patch.object(strict_trader, "last_position_closed_time", return_value=None),
            patch.object(strict_trader, "build_risk_levels", return_value=levels),
            patch.object(shared, "save_fifteen_m_swing_watch"),
            patch.object(strict_trader, "save_remembered_breakout"),
        ]
        for context in common:
            context.start()
            self.addCleanup(context.stop)

        with patch.object(
            strict_trader,
            "get_cached_execution_settings",
            return_value={**settings_service.execution_defaults(), "consolidation_filter_enabled": True},
        ):
            enabled = strict_trader.get_mtf_signal(
                frame_5m, frame_15m, pd.DataFrame(), "XAUUSD"
            )
        with patch.object(
            strict_trader,
            "get_cached_execution_settings",
            return_value={**settings_service.execution_defaults(), "consolidation_filter_enabled": False},
        ):
            disabled = strict_trader.get_mtf_signal(
                frame_5m, frame_15m, pd.DataFrame(), "XAUUSD"
            )

        self.assertEqual(enabled["signal"], "WAIT")
        self.assertEqual(enabled["blocked_reason"], "WAIT_CONSOLIDATION")
        self.assertTrue(enabled["consolidation"]["is_consolidation"])
        self.assertTrue(enabled["consolidation"]["blocking"])
        self.assertTrue(any(
            call.kwargs.get("indicator_event_id") == "smc1-consolidation-event"
            and call.kwargs.get("indicator_event_identity") == {"symbol": "XAUUSD"}
            for call in strict_trader.save_remembered_breakout.call_args_list
        ))
        self.assertEqual(disabled["signal"], "BUY")
        self.assertTrue(disabled["consolidation"]["is_consolidation"])
        self.assertFalse(disabled["consolidation"]["blocking"])

    def test_final_execution_gate_respects_same_consolidation_setting(self):
        frame = self.atr_frame("XAUUSD", 0.4, rows=25)
        with patch.object(api, "get_ctrader_market_data", return_value=frame), patch.object(
            strict_trader, "closed_frame", return_value=frame
        ), patch.object(strict_trader, "trend_filter", return_value={
            "trend": "BULLISH", "buy_allowed": True, "sell_allowed": False,
        }), patch.object(strict_trader, "classify_consolidation", return_value={
            "is_consolidation": True,
        }), patch.object(api, "get_cached_execution_settings", return_value={
            **settings_service.execution_defaults(), "consolidation_filter_enabled": True,
        }):
            enabled = api.validate_fresh_ema_permission_locked("XAUUSD", "BUY")
        with patch.object(api, "get_ctrader_market_data", return_value=frame), patch.object(
            strict_trader, "closed_frame", return_value=frame
        ), patch.object(strict_trader, "trend_filter", return_value={
            "trend": "BULLISH", "buy_allowed": True, "sell_allowed": False,
        }), patch.object(strict_trader, "classify_consolidation", return_value={
            "is_consolidation": True,
        }), patch.object(api, "get_cached_execution_settings", return_value={
            **settings_service.execution_defaults(), "consolidation_filter_enabled": False,
        }):
            disabled = api.validate_fresh_ema_permission_locked("XAUUSD", "BUY")
        self.assertEqual(enabled["reason"], "WAIT_CONSOLIDATION")
        self.assertEqual(disabled["reason"], "WAIT_SETUP_SWING_IDENTITY_MISSING")

    def test_risk_plan_uses_supplied_snapshot_without_second_cache_read(self):
        settings = {
            **settings_service.execution_defaults(),
            "minimum_sl_distance_points": 100,
        }
        swings = [
            {"type": "LOW", "price": 99.1, "valid": True},
            {"type": "HIGH", "price": 103.0, "valid": True},
        ]
        frame = self.atr_frame("XAUUSD", 0.4, rows=25)
        with patch.object(
            strict_trader,
            "detect_valid_swings",
            return_value=swings,
        ), patch.object(
            strict_trader,
            "get_cached_execution_settings",
            side_effect=AssertionError("unexpected second settings read"),
        ):
            levels = ORIGINAL_BUILD_RISK_LEVELS(
                frame,
                "BUY",
                100.0,
                "XAUUSD",
                execution_settings=settings,
                event_invalidation_swing={
                    "type": "LOW", "price": 99.1, "swing_time": None,
                    "confirmation_time": None,
                    "source": "LEGACY_CURRENT_STRUCTURE",
                },
            )
        self.assertTrue(levels["ok"])
        self.assertEqual(levels["minimum_sl_points"], 100)


class StrategySettingsCooldownWiringTests(unittest.TestCase):
    def build_live_payload(self):
        identity = {
            "symbol": "XAUUSD",
            "direction": "BUY",
            "swing_type": "HIGH",
            "swing_timestamp": "1970-01-01T00:17:30Z",
            "swing_price": 101.0,
            "bos_candle_timestamp": "1970-01-01T00:18:20Z",
            "bos_level": 101.0,
            "confirmation_timestamp": "1970-01-01T00:19:10Z",
        }
        return {
            "signal_setup_id": "stable-id",
            "fifteen_m_break_close_time": "1970-01-01T00:18:20Z",
            "five_m_confirmation_close_time": "1970-01-01T00:19:10Z",
            "trend_15m": {"trend": "BULLISH", "buy_allowed": True},
            "setup_identity": identity,
        }

    def test_live_and_paper_use_same_zero_five_fifteen_thirty_minute_setting(self):
        age_seconds = 600.0
        expected_blocked = {0: False, 5: False, 15: True, 30: True}
        payload = self.build_live_payload()
        for minutes, blocked in expected_blocked.items():
            seconds = minutes * 60
            with self.subTest(minutes=minutes):
                with patch.object(shared, "PAPER_TRADE_HISTORY", [{"symbol": "XAUUSD", "side": "BUY"}]), patch.object(
                    shared, "paper_trade_age_seconds", return_value=age_seconds
                ), patch.object(
                    shared, "get_configured_cooldown_seconds", return_value=seconds
                ):
                    paper_blocked, paper_age = shared.is_paper_reentry_cooling_down(
                        "XAUUSD", "BUY"
                    )
                self.assertEqual(paper_age, age_seconds)
                self.assertEqual(paper_blocked, blocked)

                with patch.dict(api.LIVE_LAST_POSITION_CLOSED_AT, {"XAUUSD": 1000.0}), patch.dict(
                    api.LIVE_ACTIVE_ORDERS, {"XAUUSD": None}
                ), patch.object(
                    api, "get_signal_setup_id", return_value="stable-id"
                ), patch.object(
                    api, "get_live_post_close_cooldown_seconds", return_value=seconds
                ):
                    live = api.validate_auto_entry_state_locked(
                        "XAUUSD", "BUY", payload, broker_positions=[], now=1600.0
                    )
                self.assertEqual(live["reason"] == "post-close cooldown active", blocked)
                self.assertEqual(live["details"]["post_close_cooldown_seconds"], seconds)


if __name__ == "__main__":
    unittest.main()
