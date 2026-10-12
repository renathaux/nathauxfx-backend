import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from fundamentals.engine import build_currency_strength
from fundamentals.factors.central_bank import calculate_policy_factor
from fundamentals.factors.employment import calculate_employment_factor
from fundamentals.freshness import evidence_freshness
from fundamentals.ingestion import collect_official_provider_data
from fundamentals.normalization.indicators import normalize_indicator
from fundamentals.providers import bea
from fundamentals.reconciliation import provider_fingerprint
from fundamentals.repositories.economic_events import persist_calendar_batch
from fundamentals.repositories.observations import latest_released_observations
from fundamentals.schemas import EconomicEventSchema
from models import Base


NOW = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)


class EffectiveEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)

    def tearDown(self):
        self.engine.dispose()

    def persist(self, provider, item):
        persist_calendar_batch(provider, [item], [item], session_factory=self.sessions)

    def test_official_actual_wins_and_jblanked_forecast_attaches(self):
        release = NOW - timedelta(days=10)
        official = {
            "event_name": "US CPI y/y", "indicator": "cpi_y_y", "currency": "USD",
            "release_time": release, "actual": "2.7%", "previous": "2.6%",
            "provider_event_id": "bls:cpi:2026-07", "provider_dataset": "bls-api",
        }
        commercial = {
            **official, "actual": "2.8%", "forecast": "2.7%", "impact": "HIGH",
            "provider_event_id": "jblanked-cpi", "provider_dataset": "mql5",
            "raw": {"Outcome": "As Expected", "Quality": "Good Data"},
        }
        self.persist("bls", official)
        self.persist("jblanked_mql5", commercial)
        result = latest_released_observations(["USD"], now=NOW, session_factory=self.sessions)[0]
        self.assertEqual(result["actual"], "2.7%")
        self.assertEqual(result["forecast"], "2.7%")
        self.assertEqual(result["field_sources"]["actual"], "bls")
        self.assertEqual(result["field_sources"]["forecast"], "jblanked_mql5")

    def test_placeholder_zero_is_rejected_but_true_zero_is_accepted(self):
        placeholder = {
            "event_name": "US Retail Sales m/m", "indicator": "retail_sales_m_m",
            "currency": "USD", "release_time": NOW - timedelta(days=8), "actual": 0,
            "forecast": "0.2%", "provider_event_id": "retail-placeholder",
            "provider_dataset": "mql5",
            "raw": {"Outcome": "Data Not Loaded", "Quality": "Data Not Loaded"},
        }
        valid = {
            **placeholder, "release_time": NOW - timedelta(days=4),
            "provider_event_id": "retail-real", "forecast": "0.1%",
            "raw": {"Outcome": "Lower than expected", "Quality": "Good Data"},
        }
        self.persist("jblanked_mql5", placeholder)
        self.persist("jblanked_mql5", valid)
        rows = latest_released_observations(["USD"], now=NOW, session_factory=self.sessions)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["actual"], "0")
        self.assertFalse(any(
            item["reason"] == "JBLANKED_PLACEHOLDER_ACTUAL"
            for item in rows[0]["data_quality_rejections"]
        ))

    def test_placeholder_rejection_reason_is_exposed_on_effective_event(self):
        release = NOW - timedelta(days=3)
        official = {
            "event_name": "US CPI y/y", "indicator": "cpi_y_y", "currency": "USD",
            "release_time": release, "actual": "2.7%", "previous": "2.6%",
            "provider_event_id": "official-cpi", "provider_dataset": "bls-api",
        }
        placeholder = {
            **official, "actual": 0, "forecast": "2.7%", "provider_event_id": "jb-cpi",
            "provider_dataset": "mql5", "raw": {"Outcome": "Data Not Loaded"},
        }
        self.persist("bls", official)
        self.persist("jblanked_mql5", placeholder)
        result = latest_released_observations(["USD"], now=NOW, session_factory=self.sessions)[0]
        self.assertEqual(result["actual"], "2.7%")
        self.assertIn("JBLANKED_PLACEHOLDER_ACTUAL", {
            item["reason"] for item in result["data_quality_rejections"]
        })

    def test_reused_jblanked_series_id_has_release_specific_fingerprint(self):
        def schema(release):
            return EconomicEventSchema(
                event_id="x", event_name="US CPI", indicator="cpi_y_y",
                country="United States", currency="USD", impact="HIGH",
                release_time=release, provider="jblanked_mql5",
                provider_event_id="series-123", provider_timestamp=None,
                fetched_at=NOW, data_status="RELEASED", raw={"provider_dataset": "mql5"},
            )
        self.assertNotEqual(
            provider_fingerprint(schema(NOW - timedelta(days=30))),
            provider_fingerprint(schema(NOW)),
        )

    def test_fed_policy_uses_prior_official_decision_without_forecast(self):
        for index, rate in enumerate(("4.50%", "4.25%")):
            self.persist("federal_reserve", {
                "event_name": "Federal Reserve Interest Rate Decision",
                "indicator": "fed_interest_rate", "currency": "USD",
                "release_time": NOW - timedelta(days=70 - index * 40),
                "actual": rate, "provider_event_id": f"fed-{index}",
                "provider_dataset": "fomc_statements",
            })
        rows = latest_released_observations(["USD"], now=NOW, lookback_days=365, session_factory=self.sessions)
        latest = rows[0]
        self.assertEqual(latest["previous"], "4.50%")
        result = calculate_policy_factor(rows, "USD", now=NOW)
        self.assertEqual(result["status"], "ACTIVE")
        self.assertLess(result["score"], 0)

    def test_bea_archive_previous_with_incompatible_units_is_derived(self):
        for index, actual in enumerate(("0.3%", "0.4%")):
            self.persist("bea", {
                "event_name": "US PCE Price Index", "indicator": "pce",
                "currency": "USD", "release_time": NOW - timedelta(days=60 - index * 30),
                "actual": actual, "previous": "130.932",
                "provider_event_id": f"bea-pce-{index}", "provider_dataset": "nipa",
                "raw": {"release_actual_source": "BEA news archive"},
            })
        latest = latest_released_observations(
            ["USD"], now=NOW, session_factory=self.sessions
        )[0]
        self.assertEqual(latest["actual"], "0.4%")
        self.assertEqual(latest["previous"], "0.3%")
        self.assertEqual(latest["field_sources"]["previous"], "bea")
        self.assertIn("BEA_INCOMPATIBLE_PREVIOUS_UNIT", {
            item["reason"] for item in latest["data_quality_rejections"]
        })

    def test_eurostat_employment_without_forecast_is_selected(self):
        self.persist("eurostat", {
            "event_name": "Euro Area Employment Change q/q",
            "indicator": "employment_change_q_q", "currency": "EUR",
            "release_time": NOW - timedelta(days=35), "actual": "0.2%", "previous": "0.1%",
            "provider_event_id": "estat-employment-2026q2", "provider_dataset": "dissemination_api",
        })
        rows = latest_released_observations(["EUR"], now=NOW, session_factory=self.sessions)
        result = calculate_employment_factor(rows, "EUR", now=NOW)
        self.assertEqual(result["status"], "ACTIVE")
        self.assertGreater(result["score"], 0)


class FreshnessAndSeriesTests(unittest.TestCase):
    def test_latest_monthly_release_is_usable_before_expected_next_release(self):
        state = evidence_freshness({
            "indicator": "cpi_y_y", "release_time": NOW - timedelta(days=40),
        }, now=NOW)
        self.assertEqual(state["status"], "ACTIVE")

    def test_missing_expected_monthly_release_becomes_stale_after_grace(self):
        state = evidence_freshness({
            "indicator": "cpi_y_y", "release_time": NOW - timedelta(days=50),
        }, now=NOW)
        self.assertEqual(state["status"], "STALE")
        self.assertEqual(state["reason"], "EXPECTED_NEWER_RELEASE_MISSING")

    def test_core_pce_is_distinct_from_headline_pce(self):
        self.assertEqual(normalize_indicator("PCE Price Index m/m"), "pce_m_m")
        self.assertEqual(normalize_indicator("Core PCE Price Index m/m"), "core_pce_m_m")
        self.assertNotEqual(normalize_indicator("PCE"), normalize_indicator("Core PCE"))
        alternate = "The PCE price index excluding food and energy increased 0.2 percent."
        self.assertEqual(bea._release_actual("core_pce", alternate), 0.2)

    def test_reconciled_official_evidence_drives_factor_and_confidence(self):
        events = []
        for indicator, actual, previous in (
            ("fed_interest_rate", "4.25%", "4.50%"),
            ("cpi_y_y", "2.7%", "2.6%"),
            ("nonfarm_payrolls", "180K", "150K"),
            ("gdp_q_q", "2.0%", "1.5%"),
        ):
            events.append({
                "event_id": indicator, "event_name": indicator, "indicator": indicator,
                "currency": "USD", "actual": actual, "forecast": None, "previous": previous,
                "release_time": NOW - timedelta(days=20), "impact": "UNKNOWN",
                "provider": "federal_reserve" if indicator == "fed_interest_rate" else (
                    "bls" if indicator in {"cpi_y_y", "nonfarm_payrolls"} else "bea"
                ), "data_status": "RELEASED",
            })
        result = build_currency_strength("USD", events, history_lookup=lambda _event: [], now=NOW)
        self.assertEqual(result["status"], "ACTIVE")
        self.assertGreater(result["confidence"], 0)
        self.assertIn("employment_score", result["active_factors"])


class OfficialIngestionTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)

    def tearDown(self):
        self.engine.dispose()

    def test_official_refresh_is_persisted_and_not_repeated_before_due(self):
        calls = []

        def fetcher(date_from, date_to, currencies, timeout=20):
            calls.append((date_from, date_to, tuple(currencies)))
            return {"normalized_events": [{
                "event_name": "US CPI y/y", "indicator": "cpi_y_y", "currency": "USD",
                "release_time": NOW - timedelta(days=1), "actual": "2.7%",
                "provider_event_id": "bls-current", "provider_dataset": "bls-api",
            }]}

        first = collect_official_provider_data(
            now=NOW, session_factory=self.sessions, fetchers={"bls": fetcher}
        )
        second = collect_official_provider_data(
            now=NOW + timedelta(hours=1), session_factory=self.sessions, fetchers={"bls": fetcher}
        )
        self.assertEqual(first["successful_providers"], ["bls"])
        self.assertEqual(first["event_count"], 1)
        self.assertEqual(second["successful_providers"], [])
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
