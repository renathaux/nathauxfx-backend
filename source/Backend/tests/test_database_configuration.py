import ast
import sqlite3
import unittest
from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.migration import MigrationContext
from alembic.operations import Operations
from test_recovery_store import db as recovery_db, store_api

import db
from models import Base


BACKEND_DIR = Path(__file__).resolve().parents[1]
RECOVERY_TABLES = {'recovery_accounts', 'recovery_attempts',
                   'recovery_mutations', 'recovery_checkpoint_heads'}


class DatabaseConfigurationTests(unittest.TestCase):
    def setUp(self):
        # Studio owns a separate model registry; do not rely on another test's
        # imports to populate Base.metadata before the schema assertions.
        from services import strategy_studio_models

    @pytest.fixture(autouse=True)
    def migration_database(self, request):
        if request.node.name == 'test_initial_revision_contains_every_model_table':
            self.recovery_db = request.getfixturevalue('recovery_db')

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
            "execution_protocol_state",
            "broker_integration_test_submissions",
            "saved_strategies",
            "strategy_studio_selection",
            "v3b_signal_transitions",
            "strategy_setup_lifecycle",
            "strategy_studio_live_state",
            "indicator_stream_generations",
            "indicator_stream_heads",
            "strategy_setup_generations",
        }
        self.assertEqual(set(Base.metadata.tables), expected | RECOVERY_TABLES |
                         {'recovery_legacy_adoptions', 'recovery_news_strategy_compatibility'})
        legacy_tables = expected - {
            "broker_integration_test_submissions",
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
            "execution_protocol_state",
            "saved_strategies",
            "strategy_studio_selection",
            "v3b_signal_transitions",
            "strategy_setup_lifecycle",
            "strategy_studio_live_state",
            "indicator_stream_generations",
            "indicator_stream_heads",
            "strategy_setup_generations",
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
            BACKEND_DIR / "migrations" / "versions" / "20260917_0023_strategy_studio.py",
            BACKEND_DIR / "migrations" / "versions" / "20260917_0024_v3b_signal_transitions.py",
            BACKEND_DIR / "migrations" / "versions" / "20260917_0025_strategy_studio_live.py",
            BACKEND_DIR / "scripts" / "migrate_sqlite_to_neon.py",
        ]
        for path in files:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    def test_initial_revision_contains_every_model_table(self):
        config = Config()
        config.set_main_option('script_location', str(BACKEND_DIR / 'migrations'))
        scripts = ScriptDirectory.from_config(config)
        self.assertEqual(scripts.get_heads(), ['20261016_0032'])
        recovery = scripts.get_revision('20261001_0029')
        self.assertEqual(recovery.down_revision, '20261001_0028')
        self.assertEqual(scripts.get_revision('20261001_0030').down_revision, recovery.revision)
        self.assertEqual(scripts.get_revision('20261010_0031').down_revision, '20261001_0030')
        self.assertEqual(scripts.get_revision('20261016_0032').down_revision, '20261010_0031')
        # Inspect create-table operations, not arbitrary quoted strings/comments
        # or downgrade-only definitions. Historical model tables may later retire.
        created = set()
        for revision in scripts.walk_revisions():
            tree = ast.parse(Path(revision.path).read_text(encoding='utf-8'))
            upgrade = next(node for node in tree.body
                           if isinstance(node, ast.FunctionDef) and node.name == 'upgrade')
            for node in ast.walk(upgrade):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == 'op'
                    and node.func.attr == 'create_table' and node.args
                    and isinstance(node.args[0], ast.Constant)):
                    created.add(node.args[0].value)
        self.assertTrue(set(Base.metadata.tables).issubset(created),
                        set(Base.metadata.tables) - created)

        # Apply the approved recovery migration to a disposable pre-recovery
        # schema, using the same Operations boundary as the recovery migration tests.
        with self.recovery_db.begin() as session:
            from datetime import datetime, timezone
            from models import RuntimeSetting
            session.add(RuntimeSetting(setting_name='migration-preservation-proof',
                setting_value='unchanged', updated_by='test', updated_at=datetime.now(timezone.utc)))
            session.flush()
            connection = session.connection()
            for name in ('recovery_news_strategy_compatibility', 'recovery_legacy_adoptions',
                         'recovery_checkpoint_heads', 'recovery_mutations',
                         'recovery_attempts', 'recovery_accounts'):
                Base.metadata.tables[name].drop(connection)
            schema = connection.get_execution_options()['schema_translate_map'][None]
            connection.execute(text('SET LOCAL search_path TO ' + schema))
            before = inspect(connection)
            existing = set(before.get_table_names())
            columns = {name: before.get_columns(name) for name in existing}
            self.assertTrue(RECOVERY_TABLES.isdisjoint(existing))
            with Operations.context(MigrationContext.configure(connection)):
                recovery.module.upgrade()
            after = inspect(connection)
            self.assertEqual(set(after.get_table_names()), existing | RECOVERY_TABLES)
            for name in existing:
                self.assertEqual([(c['name'], type(c['type']), getattr(c['type'], 'length', None),
                                   getattr(c['type'], 'timezone', None), c['nullable']) for c in columns[name]],
                    [(c['name'], type(c['type']), getattr(c['type'], 'length', None),
                      getattr(c['type'], 'timezone', None), c['nullable']) for c in after.get_columns(name)])
            self.assertEqual(session.execute(text(
                "SELECT setting_value FROM runtime_settings WHERE setting_name='migration-preservation-proof'"
            )).scalar_one(), 'unchanged')
            for name in RECOVERY_TABLES:
                model = Base.metadata.tables[name]
                actual = {c['name']: c for c in after.get_columns(name)}
                self.assertEqual(set(actual), set(model.columns.keys()))
                for column in model.columns:
                    self.assertEqual(actual[column.name]['nullable'], column.nullable)
                    self.assertIsInstance(actual[column.name]['type'], type(column.type))
                    self.assertEqual(getattr(actual[column.name]['type'], 'length', None),
                                     getattr(column.type, 'length', None))
                    self.assertEqual(getattr(actual[column.name]['type'], 'timezone', None),
                                     getattr(column.type, 'timezone', None))
                self.assertEqual(after.get_pk_constraint(name)['constrained_columns'],
                                 [c.name for c in model.primary_key.columns])
                self.assertEqual(session.execute(text('SELECT count(*) FROM ' + name)).scalar_one(), 0)
            self.assertEqual({tuple(c['column_names']) for c in after.get_unique_constraints('recovery_accounts')},
                             {('broker', 'environment', 'account_id')})
            self.assertEqual({tuple(c['column_names']) for c in after.get_unique_constraints('recovery_attempts')},
                             {('scope_key', 'epoch')})
            for name in RECOVERY_TABLES:
                self.assertEqual(
                    {(tuple(f['constrained_columns']), f['referred_table'], tuple(f['referred_columns']))
                     for f in after.get_foreign_keys(name)},
                    {(tuple(e.parent.name for e in f.elements), f.referred_table.name,
                      tuple(e.column.name for e in f.elements)) for f in Base.metadata.tables[name].foreign_key_constraints})

    def test_services_do_not_create_schema_at_import_time(self):
        for relative in (
            "services/auto_trade_state_service.py",
            "services/news_mode_service.py",
            "services/broker_account_state_service.py",
        ):
            source = (BACKEND_DIR / relative).read_text(encoding="utf-8")
            self.assertNotIn("metadata.create_all", source)

    def test_local_sqlite_source_is_readable_for_one_time_copy(self):
        source = BACKEND_DIR / 'database' / 'flowsignal.db'
        connection = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)
        try:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            connection.close()
        self.assertTrue(RECOVERY_TABLES.isdisjoint(tables))
        source_tables = set(Base.metadata.tables) - RECOVERY_TABLES - {
            'recovery_legacy_adoptions', 'recovery_news_strategy_compatibility'} - {
            "broker_integration_test_submissions",
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
            "execution_protocol_state",
            "saved_strategies",
            "strategy_studio_selection",
            "v3b_signal_transitions",
            "strategy_setup_lifecycle",
            "strategy_studio_live_state",
            "indicator_stream_generations",
            "indicator_stream_heads",
            "strategy_setup_generations",
        }
        self.assertTrue(source_tables.issubset(tables))


if __name__ == "__main__":
    unittest.main()
