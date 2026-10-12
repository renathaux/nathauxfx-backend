import unittest
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from models import Base, RuntimeSetting, StrategySettingAudit
from services.strategy_settings_service import (
    StrategySettingsValidationError,
    defaults,
    get_strategy_settings,
    get_strategy_settings_history,
    reset_strategy_settings,
    save_strategy_settings,
)


class StrategySettingsTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)

    def tearDown(self):
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def test_get_returns_current_defaults_and_validation_metadata(self):
        result = get_strategy_settings(self.sessions)
        self.assertEqual(result["current"], defaults())
        self.assertEqual(result["defaults"], defaults())
        self.assertEqual(result["limits"]["minimum_rr"]["min"], 1.0)
        self.assertEqual(result["limits"]["risk_per_trade_percent"]["max"], 1.0)
        self.assertIsNone(result["last_updated"])
        self.assertEqual(result["phase"], "V1_COMPLETE")
        self.assertEqual(
            {key for key, wired in result["execution_wiring"].items() if wired},
            {
                "minimum_rr",
                "maximum_rr",
                "bos_buffer_points",
                "minimum_sl_distance_points",
                "post_trade_cooldown_minutes",
                "consolidation_filter_enabled",
            },
        )

    def test_legacy_bos_floor_row_is_read_without_losing_saved_value(self):
        now = datetime(2026, 8, 11, tzinfo=timezone.utc)
        with self.sessions() as session:
            session.add(RuntimeSetting(
                setting_name="strategy.bos_buffer_min_points",
                setting_value="25",
                updated_at=now,
                updated_by="legacy",
            ))
            session.commit()
        loaded = get_strategy_settings(self.sessions)
        self.assertEqual(loaded["current"]["bos_buffer_points"], 25)
        self.assertNotIn("bos_buffer_min_points", loaded["current"])

    def test_invalid_update_is_rejected_without_partial_write(self):
        with self.assertRaisesRegex(
            StrategySettingsValidationError,
            "maximum_rr must be greater",
        ):
            save_strategy_settings(
                {"minimum_rr": 3.0, "maximum_rr": 2.0},
                updated_by="owner",
                session_factory=self.sessions,
            )
        with self.sessions() as session:
            self.assertEqual(session.query(RuntimeSetting).count(), 0)

    def test_valid_update_is_durable_and_preserves_unspecified_values(self):
        now = datetime(2026, 8, 11, 15, 0, tzinfo=timezone.utc)
        saved = save_strategy_settings(
            {"minimum_rr": 1.4, "post_trade_cooldown_minutes": 20},
            updated_by="owner@example.com",
            session_factory=self.sessions,
            now=now,
        )
        loaded = get_strategy_settings(self.sessions)
        self.assertEqual(saved["current"], loaded["current"])
        self.assertEqual(loaded["current"]["minimum_rr"], 1.4)
        self.assertEqual(loaded["current"]["maximum_rr"], 2.0)
        self.assertEqual(loaded["current"]["post_trade_cooldown_minutes"], 20)
        self.assertEqual(loaded["last_updated"], "2026-08-11T15:00:00Z")
        with self.sessions() as session:
            self.assertEqual(
                session.query(RuntimeSetting)
                .filter(RuntimeSetting.setting_name.like("strategy.%"))
                .count(),
                2,
            )
            audits = session.query(StrategySettingAudit).all()
            self.assertEqual(len(audits), 2)
            audit_by_name = {audit.setting_name: audit for audit in audits}
            self.assertEqual(audit_by_name["minimum_rr"].previous_value, "1.2")
            self.assertEqual(audit_by_name["minimum_rr"].new_value, "1.4")
            self.assertEqual(
                audit_by_name["minimum_rr"].updated_by,
                "owner@example.com",
            )

    def test_partial_update_validates_against_saved_configuration(self):
        save_strategy_settings(
            {"maximum_rr": 3.0},
            updated_by="owner",
            session_factory=self.sessions,
        )
        saved = save_strategy_settings(
            {"minimum_rr": 2.5},
            updated_by="owner",
            session_factory=self.sessions,
        )
        self.assertEqual(saved["current"]["minimum_rr"], 2.5)
        self.assertEqual(saved["current"]["maximum_rr"], 3.0)

    def test_reset_requires_confirmation_and_restores_defaults(self):
        save_strategy_settings(
            {"minimum_rr": 1.4},
            updated_by="owner",
            session_factory=self.sessions,
        )
        with self.assertRaisesRegex(
            StrategySettingsValidationError,
            "explicit confirmation",
        ):
            reset_strategy_settings(
                confirmed=False,
                updated_by="owner",
                session_factory=self.sessions,
            )
        reset = reset_strategy_settings(
            confirmed=True,
            updated_by="owner",
            session_factory=self.sessions,
        )
        self.assertEqual(reset["current"], defaults())

    def test_cross_field_and_unknown_setting_validation(self):
        with self.assertRaisesRegex(StrategySettingsValidationError, "read-only"):
            save_strategy_settings(
                {"ema_fast_period": 21, "ema_slow_period": 21},
                updated_by="owner",
                session_factory=self.sessions,
            )
        with self.assertRaisesRegex(StrategySettingsValidationError, "unknown"):
            save_strategy_settings(
                {"broker_account_id": "hidden"},
                updated_by="owner",
                session_factory=self.sessions,
            )

    def test_fixed_rule_rows_are_ignored_and_cannot_be_saved(self):
        now = datetime(2026, 8, 11, tzinfo=timezone.utc)
        with self.sessions() as session:
            session.add(RuntimeSetting(
                setting_name="strategy.ema_fast_period",
                setting_value="50",
                updated_at=now,
                updated_by="legacy",
            ))
            session.commit()
        self.assertEqual(get_strategy_settings(self.sessions)["current"]["ema_fast_period"], 9)
        with self.assertRaisesRegex(StrategySettingsValidationError, "read-only"):
            save_strategy_settings(
                {"ema_fast_period": 12},
                updated_by="owner",
                session_factory=self.sessions,
            )

    def test_history_is_newest_first_bounded_and_read_only(self):
        save_strategy_settings(
            {"minimum_rr": 1.3},
            updated_by="first",
            session_factory=self.sessions,
            now=datetime(2026, 8, 11, 15, 0, tzinfo=timezone.utc),
        )
        save_strategy_settings(
            {"minimum_rr": 1.4, "consolidation_filter_enabled": False},
            updated_by="second",
            session_factory=self.sessions,
            now=datetime(2026, 8, 11, 16, 0, tzinfo=timezone.utc),
        )
        history = get_strategy_settings_history(
            limit=2,
            session_factory=self.sessions,
        )
        self.assertTrue(history["read_only"])
        self.assertEqual(history["count"], 2)
        self.assertEqual(history["total"], 3)
        self.assertEqual(history["items"][0]["changed_by"], "second")
        self.assertEqual(history["items"][0]["changed_at"], "2026-08-11T16:00:00Z")
        self.assertIn(
            history["items"][0]["setting_name"],
            {"minimum_rr", "consolidation_filter_enabled"},
        )

    def test_history_empty_state(self):
        history = get_strategy_settings_history(session_factory=self.sessions)
        self.assertEqual(history["items"], [])
        self.assertEqual(history["total"], 0)

    def test_foundation_service_does_not_import_trading_or_broker_modules(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "services"
            / "strategy_settings_service.py"
        ).read_text(encoding="utf-8")
        for forbidden in (
            "strategies.strict_trader",
            "services.ctrader",
            "broker execution",
            "LIVE_ORDER_IN_FLIGHT",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
