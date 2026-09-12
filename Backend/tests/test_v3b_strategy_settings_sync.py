import unittest
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from models import Base, RuntimeSetting, StrategySettingAudit
from services import active_strategy_config_service as config
from services.v3b_strategy_settings_sync import _apply_management_to_candidate


class ActiveV3BStrategySettingsTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        config.invalidate_cache(self.sessions)

    def tearDown(self):
        config.invalidate_cache(self.sessions)
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def test_new_profile_starts_from_v3b_defaults(self):
        result = config.get_active_strategy_settings(self.sessions)
        self.assertEqual(result["profile"], "V3B_M5_FROZEN")
        self.assertEqual(result["phase"], "V3B_ACTIVE")
        self.assertEqual(result["current"]["target_rr"], 1.90)
        self.assertEqual(result["current"]["protection_trigger_percent"], 70.0)
        self.assertEqual(result["current"]["protected_stop_percent"], 60.0)
        self.assertTrue(result["strategy_version_scoped"])
        self.assertFalse(result["fixed_rules"]["ema_filter"])
        self.assertFalse(result["fixed_rules"]["m15_entry_dependency"])

    def test_overrides_are_durable_and_namespaced_to_active_profile(self):
        saved = config.save_active_strategy_settings(
            {
                "target_rr": 2.0,
                "protection_trigger_percent": 75,
                "protected_stop_percent": 55,
            },
            updated_by="owner",
            session_factory=self.sessions,
            now=datetime(2026, 9, 12, 18, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(saved["current"]["target_rr"], 2.0)
        self.assertEqual(saved["current"]["protection_trigger_percent"], 75.0)
        self.assertEqual(saved["current"]["protected_stop_percent"], 55.0)
        with self.sessions() as session:
            names = {
                row.setting_name for row in session.query(RuntimeSetting).all()
            }
            self.assertEqual(
                names,
                {
                    "strategy_profile.V3B_M5_FROZEN.target_rr",
                    "strategy_profile.V3B_M5_FROZEN.protection_trigger_percent",
                    "strategy_profile.V3B_M5_FROZEN.protected_stop_percent",
                },
            )
            audits = session.query(StrategySettingAudit).all()
            self.assertEqual(len(audits), 3)
            self.assertTrue(
                all(row.setting_name.startswith("V3B_M5_FROZEN.") for row in audits)
            )

    def test_invalid_protected_stop_beyond_trigger_is_rejected(self):
        with self.assertRaisesRegex(
            config.ActiveStrategyConfigError,
            "cannot be greater",
        ):
            config.save_active_strategy_settings(
                {
                    "protection_trigger_percent": 60,
                    "protected_stop_percent": 70,
                },
                updated_by="owner",
                session_factory=self.sessions,
            )
        with self.sessions() as session:
            self.assertEqual(session.query(RuntimeSetting).count(), 0)

    def test_production_candidate_management_uses_active_values(self):
        candidate = {
            "symbol": "EURUSD",
            "signal": "BUY",
            "entry_price": 1.10000,
            "stop_loss": 1.09900,
            "paper_entry_ready": True,
            "paper_entry_details": {},
        }
        values = {
            "target_rr": 2.0,
            "protection_trigger_percent": 75.0,
            "protected_stop_percent": 50.0,
        }
        result = _apply_management_to_candidate(candidate, values)
        self.assertEqual(result["tp2"], 1.10200)
        self.assertEqual(result["tp1"], 1.10150)
        self.assertEqual(result["protected_sl_price"], 1.10100)
        self.assertEqual(result["risk_reward_ratio"], 2.0)
        self.assertEqual(result["protection_trigger_tp2_fraction"], 0.75)
        self.assertEqual(result["protected_stop_tp2_fraction"], 0.50)
        self.assertEqual(result["strategy_config_profile"], "V3B_M5_FROZEN")

    def test_research_defaults_are_not_modified_by_production_override_math(self):
        from services.strategy_lab import v3b_m5_frozen_candidate as research

        before = (
            research.TARGET_RR,
            research.PROTECTION_TRIGGER_TP2_FRACTION,
            research.PROTECTED_STOP_TP2_FRACTION,
        )
        _apply_management_to_candidate(
            {
                "symbol": "EURUSD",
                "signal": "SELL",
                "entry_price": 1.10000,
                "stop_loss": 1.10100,
                "paper_entry_ready": True,
            },
            {
                "target_rr": 2.25,
                "protection_trigger_percent": 80.0,
                "protected_stop_percent": 65.0,
            },
        )
        after = (
            research.TARGET_RR,
            research.PROTECTION_TRIGGER_TP2_FRACTION,
            research.PROTECTED_STOP_TP2_FRACTION,
        )
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
