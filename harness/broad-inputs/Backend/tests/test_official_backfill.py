import ast
import contextlib
import json
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from fundamentals.official_backfill import DEFAULT_MANIFEST, _validate_manifest, run_official_backfill
from fundamentals import locks as lock_module
from models import Base, EconomicEvent, EconomicEventObservation, EconomicEventProviderLink


@contextlib.contextmanager
def locks(_key):
    yield True


def event(actual="3.1"):
    return {
        "event_name": "US CPI y/y", "indicator": "cpi_y_y", "currency": "USD",
        "country": "United States", "impact": "UNKNOWN",
        "release_time": datetime(2026, 6, 10, 12, 30, tzinfo=timezone.utc),
        "actual": actual, "previous": "3.0", "forecast": None,
        "provider": "bls", "provider_event_id": "bls:CUSR0000SA0:2026:M05",
        "provider_dataset": "public_data_api_v2_manifest_v1",
        "data_status": "RELEASED",
    }


class OfficialBackfillTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)

    def tearDown(self):
        self.engine.dispose()

    def run_job(self, **overrides):
        args = dict(
            provider="bls", date_from="2026-06-01", date_to="2026-06-30",
            chunk_days=31, currencies="USD", session_factory=self.sessions,
            bind=self.engine, lock_manager=locks, sleeper=lambda _seconds: None,
            fetcher=lambda *_args: {"request_count": 1, "normalized_events": [event()]},
        )
        args.update(overrides)
        return run_official_backfill(**args)

    def test_dry_run_writes_nothing(self):
        report = self.run_job(
            dry_run=True,
            fetcher=lambda *_args: {
                "request_count": 1,
                "normalized_events": [event()],
                "manifest_rows": 1,
                "matched_values": 1,
                "missing_manifest_matches": [],
                "duplicate_manifest_ids": 0,
                "timestamp_conflicts": 0,
                "parsing_errors": [],
            },
        )
        self.assertEqual(report["status"], "DRY_RUN")
        self.assertEqual(report["parsed_releases"], 1)
        self.assertEqual(report["matched_values"], 1)
        self.assertEqual(report["unmatched_values"], [])
        self.assertEqual(report["duplicate_manifest_ids"], 0)
        self.assertEqual(report["timestamp_conflicts"], 0)
        self.assertEqual(report["parsing_errors"], [])
        self.assertEqual(report["proposed_inserts"], 1)
        self.assertEqual(report["database_writes"], 0)
        with self.sessions() as session:
            self.assertEqual(session.query(EconomicEvent).count(), 0)
            self.assertEqual(session.query(EconomicEventObservation).count(), 0)

    def test_verified_manifest_includes_new_bls_releases(self):
        rows = _validate_manifest(DEFAULT_MANIFEST)
        indexed = {row["stable_manifest_id"]: row for row in rows}
        expected = {
            "bls_manifest:employment_situation:2026-06": "2026-07-02T12:30:00.000Z",
            "bls_manifest:cpi:2026-06": "2026-07-14T12:30:00.000Z",
            "bls_manifest:ppi:2026-06": "2026-07-15T12:30:00.000Z",
            "bls_manifest:employment_situation:2026-07": "2026-08-07T12:30:00.000Z",
        }
        for stable_id, timestamp in expected.items():
            self.assertIn(stable_id, indexed)
            self.assertEqual(indexed[stable_id]["release_timestamp_utc"], timestamp)
            self.assertEqual(indexed[stable_id]["release_time"], "08:30:00")
            self.assertEqual(indexed[stable_id]["timezone"], "America/New_York")
            self.assertTrue(indexed[stable_id]["official_url"].startswith(
                "https://www.bls.gov/news.release/archives/"
            ))

    def test_dry_run_report_is_repeatable(self):
        first = self.run_job(dry_run=True)
        second = self.run_job(dry_run=True)
        self.assertEqual(first, second)

    def test_backfill_is_idempotent_and_resumable(self):
        first = self.run_job()
        second = self.run_job(resume=True)
        self.assertEqual(first["status"], "COMPLETED")
        self.assertEqual(second["status"], "COMPLETED")
        with self.sessions() as session:
            self.assertEqual(session.query(EconomicEvent).count(), 1)
            self.assertEqual(session.query(EconomicEventObservation).count(), 1)
            self.assertEqual(session.query(EconomicEventProviderLink).count(), 1)

    def test_manifest_rejects_timestamp_conflict(self):
        base = {
            "release_family": "cpi", "reference_period": "2026-05",
            "release_date": "2026-06-10", "release_time": "08:30:00",
            "timezone": "America/New_York", "release_timestamp_utc": "2026-06-10T12:30:00Z",
            "official_url": "https://www.bls.gov/news.release/archives/cpi_06102026.htm",
            "content_hash": "a" * 64, "stable_manifest_id": "bls_manifest:cpi:2026-05",
            "source": "BLS",
        }
        other = dict(base, release_timestamp_utc="2026-06-10T13:30:00Z")
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "manifest.json"
            path.write_text(json.dumps({"schema_version": "1.0", "manifests": [base, other]}))
            with self.assertRaisesRegex(ValueError, "conflict"):
                _validate_manifest(path)

    def test_manifest_rejects_incorrect_eastern_to_utc_conversion(self):
        row = {
            "release_family": "cpi", "reference_period": "2026-06",
            "release_date": "2026-07-14", "release_time": "08:30:00",
            "timezone": "America/New_York", "release_timestamp_utc": "2026-07-14T13:30:00Z",
            "official_url": "https://www.bls.gov/news.release/archives/cpi_07142026.htm",
            "content_hash": "a" * 64, "stable_manifest_id": "bls_manifest:cpi:2026-06",
            "source": "BLS",
        }
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "manifest.json"
            path.write_text(json.dumps({"schema_version": "1.0", "manifests": [row]}))
            with self.assertRaisesRegex(ValueError, "timezone conversion conflict"):
                _validate_manifest(path)

    def test_module_has_no_trading_or_broker_imports(self):
        source = Path(__file__).parents[1] / "fundamentals" / "official_backfill.py"
        tree = ast.parse(source.read_text())
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported += [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                imported.append(node.module or "")
        forbidden = ("broker", "strategy", "trade", "ctrader", "news_mode", "auto_trade")
        self.assertFalse([name for name in imported if any(term in name.lower() for term in forbidden)])

    def test_postgres_session_locks_use_direct_migration_endpoint(self):
        class Dialect:
            name = "postgresql"
        class PooledBind:
            dialect = Dialect()
        direct = object()
        lock_module._DIRECT_ENGINE = None
        lock_module._DIRECT_ENGINE_URL = None
        with mock.patch.dict(
            "os.environ",
            {"MIGRATION_DATABASE_URL": "postgres://user:pass@direct.example/neondb"},
        ), mock.patch.object(lock_module, "create_engine", return_value=direct) as creator:
            selected = lock_module._coordination_bind(PooledBind())
        self.assertIs(selected, direct)
        self.assertEqual(creator.call_args.args[0], "postgresql://user:pass@direct.example/neondb")


if __name__ == "__main__":
    unittest.main()
