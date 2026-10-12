import ast
import contextlib
from datetime import datetime, timezone
from pathlib import Path
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from fundamentals.backfill import run_backfill
from fundamentals.preflight import run_preflight
from fundamentals.provider_audit import analyze_events
from fundamentals.normalization.indicators import indicator_metadata, normalize_indicator
from fundamentals.providers.jblanked import (
    DATASET_ID,
    JBlankedAccessError,
    normalize_mql5_event,
)
from models import (
    Base,
    EconomicBackfillJob,
    EconomicEvent,
    EconomicEventObservation,
    EconomicProviderFetch,
    FundamentalInsightSnapshot,
)
from services import news_service


def raw_event(*, actual="0.4", event_id="840030018", date_value="2026.07.01 15:30:00"):
    return {
        "Name": "Core CPI m/m",
        "Currency": "USD",
        "Category": "Consumer Inflation Report",
        "Impact": "High",
        "Date": date_value,
        "Actual": actual,
        "Forecast": "0.3",
        "Previous": "0.2",
        "Event_ID": event_id,
    }


def provider_result(start, end, currencies, *, actual="0.4", duplicate=False):
    raw = raw_event(actual=actual)
    items = [raw, dict(raw)] if duplicate else [raw]
    return {
        "provider": "jblanked",
        "dataset": "mql5",
        "provider_identity": DATASET_ID,
        "source": "JBlanked MQL5 calendar/range",
        "provider_timezone": "GMT+3",
        "normalized_timezone": "UTC",
        "request_count": 1,
        "estimated_credit_usage": 1,
        "raw_events": items,
        "normalized_events": [normalize_mql5_event(item) for item in items],
    }


@contextlib.contextmanager
def all_locks(_key):
    yield True


class FundamentalProviderPreflightTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)

    def tearDown(self):
        self.engine.dispose()

    def test_preflight_is_read_only(self):
        report = run_preflight(
            provider="jblanked",
            date_from="2026-07-01",
            date_to="2026-07-07",
            currencies="EUR,USD",
            fetcher=provider_result,
        )
        self.assertTrue(report["read_only"])
        self.assertEqual(report["database_writes"], 0)
        self.assertEqual(report["request_count"], 1)
        self.assertEqual(report["currency_counts"], {"USD": 1})
        with self.sessions() as session:
            self.assertEqual(session.query(EconomicEvent).count(), 0)
            self.assertEqual(session.query(EconomicProviderFetch).count(), 0)

    def test_jblanked_dataset_normalization_and_timezone(self):
        item = normalize_mql5_event(raw_event())
        self.assertEqual(item["provider"], "jblanked_mql5")
        self.assertEqual(item["provider_event_id"], "840030018")
        self.assertEqual(item["release_time"].isoformat(), "2026-07-01T12:30:00+00:00")
        report = analyze_events([item], provider_identity=DATASET_ID)
        self.assertTrue(report["timezone"]["all_normalized_to_utc"])
        self.assertEqual(report["impact_counts"], {"HIGH": 1})
        self.assertEqual(report["high_impact_event_count"], 1)
        self.assertEqual(report["normalized_indicators"], ["Core CPI m/m -> core_cpi_m_m"])

    def test_distinct_indicator_variants_remain_distinct(self):
        expected = {
            "Average Hourly Earnings m/m": "average_hourly_earnings_m_m",
            "Average Hourly Earnings y/y": "average_hourly_earnings_y_y",
            "CPI m/m": "cpi_m_m",
            "CPI y/y": "cpi_y_y",
            "Core CPI m/m": "core_cpi_m_m",
            "Core CPI y/y": "core_cpi_y_y",
            "PCE Price Index m/m": "pce_m_m",
            "PCE Price Index y/y": "pce_y_y",
            "Core PCE Price Index m/m": "core_pce_m_m",
            "Core PCE Price Index y/y": "core_pce_y_y",
            "GDP q/q Preliminary": "gdp_q_q_preliminary",
            "GDP q/q Final": "gdp_q_q_final",
            "GDP y/y": "gdp_y_y",
            "Manufacturing PMI Preliminary": "manufacturing_pmi_preliminary",
            "Services PMI Final": "services_pmi_final",
            "Nonfarm Payrolls": "nonfarm_payrolls",
            "Unemployment Rate": "unemployment_rate",
            "Employment Change": "employment_change",
            "Fed Interest Rate Decision": "fed_interest_rate",
            "ECB Interest Rate Decision": "ecb_interest_rate",
        }
        normalized = {name: normalize_indicator(name) for name in expected}
        self.assertEqual(normalized, expected)
        self.assertEqual(len(set(normalized.values())), len(normalized))
        self.assertEqual(normalize_indicator("Headline CPI y/y"), "cpi_y_y")
        self.assertNotEqual(
            normalize_indicator("Headline CPI y/y"),
            normalize_indicator("Core CPI y/y"),
        )
        self.assertEqual(normalize_indicator("Headline PCE y/y"), "pce_y_y")
        self.assertNotEqual(
            normalize_indicator("Headline PCE y/y"),
            normalize_indicator("Core PCE y/y"),
        )

    def test_qualified_variants_keep_recognized_factor_metadata(self):
        for name in (
            "Average Hourly Earnings m/m",
            "Average Hourly Earnings y/y",
            "Core CPI y/y",
            "Core PCE Price Index m/m",
            "GDP q/q Preliminary",
            "Manufacturing PMI Preliminary",
            "Services PMI Final",
            "Fed Interest Rate Decision",
            "ECB Interest Rate Decision",
        ):
            with self.subTest(name=name):
                metadata = indicator_metadata(name)
                self.assertTrue(metadata["recognized"])
                self.assertNotEqual(metadata["category"], "other")

    def test_duplicate_detection(self):
        result = provider_result(None, None, None, duplicate=True)
        report = analyze_events(result["normalized_events"], provider_identity=DATASET_ID)
        self.assertEqual(report["duplicate_count"], 1)

    def test_conflicting_jblanked_datasets_remain_separate(self):
        mql5 = normalize_mql5_event(raw_event(actual="0.4"))
        forex_factory = {
            **mql5,
            "provider": "jblanked_forex_factory",
            "source": "jblanked_forex_factory",
            "actual": "0.5",
        }
        report = analyze_events(
            [mql5, forex_factory], provider_identity=DATASET_ID
        )
        self.assertEqual(report["conflict_count"], 1)
        self.assertEqual(
            report["conflicts"][0]["datasets"],
            ["jblanked_forex_factory", "jblanked_mql5"],
        )
        self.assertEqual(
            report["conflicts"][0]["resolution"],
            "PRESERVED_SEPARATELY_NOT_AVERAGED",
        )

    def test_provider_access_failure_is_safe(self):
        with self.assertRaises(JBlankedAccessError):
            run_preflight(
                provider="jblanked",
                date_from="2026-07-01",
                date_to="2026-07-07",
                currencies="EUR,USD",
                fetcher=lambda *_args: (_ for _ in ()).throw(
                    JBlankedAccessError("credits unavailable")
                ),
            )


class FmpStableContractTests(unittest.TestCase):
    def test_stable_endpoint_and_response_contract(self):
        class Response:
            status_code = 200
            text = "[]"

            def json(self):
                return [{
                    "event": "CPI",
                    "date": "2026-08-08 12:30:00",
                    "country": "US",
                    "actual": 3.2,
                    "estimate": 3.0,
                    "previous": 2.9,
                    "impact": "High",
                    "unit": "%",
                }]

        with (
            patch.dict("os.environ", {"FMP_API_KEY": "test-key", "FMP_NEWS_BASE_URL": ""}),
            patch.object(news_service.requests, "get", return_value=Response()) as request,
        ):
            raw, normalized = news_service.fetch_fmp_calendar_events()
        self.assertEqual(
            request.call_args.args[0],
            "https://financialmodelingprep.com/stable/economic-calendar",
        )
        self.assertEqual(len(raw), 1)
        self.assertEqual(normalized[0]["forecast"], 3.0)
        self.assertEqual(normalized[0]["currency"], "USD")
        self.assertEqual(normalized[0]["impact"], "HIGH")


class FundamentalBackfillTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)

    def tearDown(self):
        self.engine.dispose()

    def run_job(self, **overrides):
        values = {
            "provider": "jblanked",
            "date_from": "2026-07-01",
            "date_to": "2026-07-07",
            "chunk_days": 7,
            "currencies": "EUR,USD",
            "fetcher": provider_result,
            "session_factory": self.sessions,
            "bind": self.engine,
            "lock_manager": all_locks,
            "sleeper": lambda _seconds: None,
            "rate_limit_seconds": 0,
        }
        values.update(overrides)
        return run_backfill(**values)

    def test_dry_run_writes_nothing(self):
        result = self.run_job(dry_run=True)
        self.assertEqual(result["status"], "DRY_RUN")
        self.assertEqual(result["observations_would_add"], 1)
        with self.sessions() as session:
            self.assertEqual(session.query(EconomicBackfillJob).count(), 0)
            self.assertEqual(session.query(EconomicEvent).count(), 0)
            self.assertEqual(session.query(EconomicEventObservation).count(), 0)
            self.assertEqual(session.query(EconomicProviderFetch).count(), 0)
            self.assertEqual(session.query(FundamentalInsightSnapshot).count(), 0)

    def test_resume_after_chunk_restart(self):
        first = self.run_job(
            date_to="2026-07-14", chunk_days=7, max_chunks=1
        )
        self.assertEqual(first["status"], "PAUSED")
        with self.sessions() as session:
            job = session.query(EconomicBackfillJob).one()
            self.assertEqual(job.current_cursor.date().isoformat(), "2026-07-08")
        second = self.run_job(
            date_to="2026-07-14", chunk_days=7, resume=True
        )
        self.assertEqual(second["status"], "COMPLETED")
        self.assertEqual(second["chunks_completed"], 1)

    def test_idempotent_completed_job(self):
        self.assertEqual(self.run_job()["status"], "COMPLETED")
        second = self.run_job(resume=True)
        self.assertEqual(second["status"], "COMPLETED")
        with self.sessions() as session:
            self.assertEqual(session.query(EconomicEventObservation).count(), 1)

    def test_revision_is_append_only(self):
        calls = {"count": 0}

        def revised_fetch(*args):
            calls["count"] += 1
            return provider_result(*args, actual="0.4" if calls["count"] == 1 else "0.5")

        first = self.run_job(
            date_to="2026-07-14", chunk_days=7, max_chunks=1,
            fetcher=revised_fetch,
        )
        self.assertEqual(first["status"], "PAUSED")
        self.run_job(
            date_to="2026-07-14", chunk_days=7, resume=True,
            fetcher=revised_fetch,
        )
        with self.sessions() as session:
            observations = session.query(EconomicEventObservation).order_by(
                EconomicEventObservation.id
            ).all()
            self.assertEqual([item.actual for item in observations], ["0.4", "0.5"])

    def test_live_ingestion_lock_pauses_before_fetch(self):
        calls = {"fetch": 0}

        @contextlib.contextmanager
        def locks(key):
            from fundamentals.locks import LIVE_INGESTION_LOCK_KEY
            yield key != LIVE_INGESTION_LOCK_KEY

        def fetch(*args):
            calls["fetch"] += 1
            return provider_result(*args)

        result = self.run_job(fetcher=fetch, lock_manager=locks)
        self.assertEqual(result["status"], "PAUSED")
        self.assertEqual(calls["fetch"], 0)

    def test_provider_failure_keeps_cursor_and_records_fetch(self):
        def fail(*_args):
            raise JBlankedAccessError("credits unavailable")

        result = self.run_job(fetcher=fail)
        self.assertEqual(result["status"], "PAUSED")
        with self.sessions() as session:
            job = session.query(EconomicBackfillJob).one()
            self.assertEqual(job.current_cursor.date().isoformat(), "2026-07-01")
            fetch = session.query(EconomicProviderFetch).one()
            self.assertEqual(fetch.status, "FAILED")

    def test_no_broker_execution_or_toggle_imports(self):
        path = Path(__file__).resolve().parents[1] / "fundamentals" / "backfill.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.append(node.module or "")
        forbidden = ("ctrader", "broker", "execution", "strategies", "auto_trade")
        self.assertFalse(any(any(term in name for term in forbidden) for name in imported))


if __name__ == "__main__":
    unittest.main()
