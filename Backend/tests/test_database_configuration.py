import ast
import sqlite3
import unittest
from pathlib import Path

from sqlalchemy import inspect

import db
from models import Base


BACKEND_DIR = Path(__file__).resolve().parents[1]


class DatabaseConfigurationTests(unittest.TestCase):
    def test_render_postgres_url_is_normalized_for_sqlalchemy(self):
        self.assertEqual(
            db.normalize_database_url("postgres://user:pass@example/db"),
            "postgresql://user:pass@example/db",
        )

    def test_database_url_redaction_removes_password_and_query(self):
        redacted = db.redact_database_url(
            "postgresql://flow:secret@example.neon.tech/db?sslmode=require"
        )
        self.assertNotIn("secret", redacted)
        self.assertNotIn("sslmode", redacted)
        self.assertIn("flow:***@example.neon.tech", redacted)

    def test_models_match_existing_sqlite_tables(self):
        expected = {
            "users",
            "runtime_settings",
            "strategy_setting_audit",
            "news_trading_mode_audit",
            "auto_trade_state_audit",
            "ctrader_oauth_tokens",
            "strategy_cycle_diagnostics",
            "forex_lifecycle_evaluations",
            "forex_execution_snapshots",
            "strategy_shadow_runtime",
            "strategy_shadow_evaluations",
            "strategy_shadow_trades",
            "execution_risk_audits",
            "economic_events",
            "economic_event_observations",
            "economic_event_provider_links",
            "economic_event_disagreements",
            "economic_provider_fetches",
            "economic_backfill_jobs",
            "fundamental_factor_inputs",
            "currency_strength_snapshots",
            "fundamental_insight_snapshots",
            "indicator_candles",
            "indicator_events",
            "indicator_stream_state",
            "indicator_event_lifecycle",
            "trade_submission_attempts",
        }
        self.assertEqual(set(Base.metadata.tables), expected)
        legacy_tables = expected - {
            "ctrader_oauth_tokens",
            "strategy_cycle_diagnostics",
            "forex_lifecycle_evaluations",
            "forex_execution_snapshots",
            "strategy_shadow_runtime",
            "strategy_shadow_evaluations",
            "strategy_shadow_trades",
            "execution_risk_audits",
            "economic_events",
            "economic_event_observations",
            "economic_event_provider_links",
            "economic_event_disagreements",
            "economic_provider_fetches",
            "economic_backfill_jobs",
            "fundamental_factor_inputs",
            "currency_strength_snapshots",
            "fundamental_insight_snapshots",
            "strategy_setting_audit",
            "indicator_candles",
            "indicator_events",
            "indicator_stream_state",
            "indicator_event_lifecycle",
            "trade_submission_attempts",
        }
        self.assertTrue(
            legacy_tables.issubset(set(inspect(db.engine).get_table_names()))
        )

    def test_migration_revision_and_tool_are_valid_python(self):
        files = [
            BACKEND_DIR / "migrations" / "env.py",
            BACKEND_DIR / "migrations" / "versions" / "20260807_0001_initial_schema.py",
            BACKEND_DIR / "migrations" / "versions" / "20260807_0002_ctrader_token_storage.py",
            BACKEND_DIR / "migrations" / "versions" / "20260807_0003_strategy_cycle_diagnostics.py",
            BACKEND_DIR / "migrations" / "versions" / "20260807_0004_fundamental_engine_phase1.py",
            BACKEND_DIR / "migrations" / "versions" / "20260808_0005_economic_backfill_jobs.py",
            BACKEND_DIR / "migrations" / "versions" / "20260808_0006_official_provider_reconciliation.py",
            BACKEND_DIR / "migrations" / "versions" / "20260811_0007_strategy_setting_audit.py",
            BACKEND_DIR / "migrations" / "versions" / "20260813_0008_strategy_v2_shadow.py",
            BACKEND_DIR / "migrations" / "versions" / "20260829_0015_forex_lifecycle_observability.py",
            BACKEND_DIR / "scripts" / "migrate_sqlite_to_neon.py",
        ]
        for path in files:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    def test_initial_revision_contains_every_model_table(self):
        source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted((BACKEND_DIR / "migrations" / "versions").glob("*.py"))
        )
        for table_name in Base.metadata.tables:
            self.assertIn(f'"{table_name}"', source)

    def test_services_do_not_create_schema_at_import_time(self):
        for relative in (
            "services/auto_trade_state_service.py",
            "services/news_mode_service.py",
            "services/broker_account_state_service.py",
        ):
            source = (BACKEND_DIR / relative).read_text(encoding="utf-8")
            self.assertNotIn("metadata.create_all", source)

    def test_local_sqlite_source_is_readable_for_one_time_copy(self):
        connection = sqlite3.connect(BACKEND_DIR / "database" / "flowsignal.db")
        try:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            connection.close()
        source_tables = set(Base.metadata.tables) - {
            "ctrader_oauth_tokens",
            "strategy_cycle_diagnostics",
            "forex_lifecycle_evaluations",
            "forex_execution_snapshots",
            "strategy_shadow_runtime",
            "strategy_shadow_evaluations",
            "strategy_shadow_trades",
            "execution_risk_audits",
            "economic_events",
            "economic_event_observations",
            "economic_event_provider_links",
            "economic_event_disagreements",
            "economic_provider_fetches",
            "economic_backfill_jobs",
            "fundamental_factor_inputs",
            "currency_strength_snapshots",
            "fundamental_insight_snapshots",
            "strategy_setting_audit",
            "indicator_candles",
            "indicator_events",
            "indicator_stream_state",
            "indicator_event_lifecycle",
            "trade_submission_attempts",
        }
        self.assertTrue(source_tables.issubset(tables))


if __name__ == "__main__":
    unittest.main()
