import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from fundamentals.confidence import calculate_confidence
from fundamentals.config import FACTOR_WEIGHTS, MINIMUM_ACTIVE_COVERAGE
from fundamentals.engine import build_currency_strength, calculate_fundamental_state
from fundamentals.explanations import select_top_reasons
from fundamentals.factors.central_bank import calculate_policy_factor
from fundamentals.factors.employment import calculate_employment_factor
from fundamentals.factors.growth import calculate_growth_factor
from fundamentals.factors.inflation import calculate_inflation_factor
from fundamentals.ingestion import run_fundamental_ingestion_if_due
from fundamentals.insight_service import get_fundamental_insight
from fundamentals.pair_bias import synthesize_pair_bias
from fundamentals.repositories.economic_events import persist_calendar_batch, record_failed_fetch
from fundamentals.repositories.observations import provider_health
from models import Base


NOW = datetime(2026, 8, 7, 14, 0, tzinfo=timezone.utc)


def macro_event(
    event_id,
    currency,
    indicator,
    actual,
    forecast,
    previous,
    *,
    days=1,
    revised_previous=None,
    impact="HIGH",
    provider="fmp",
):
    return {
        "event_id": event_id,
        "event_name": f"{currency} {indicator}",
        "indicator": indicator,
        "currency": currency,
        "country": "United States" if currency == "USD" else "Euro Area",
        "actual": actual,
        "forecast": forecast,
        "previous": previous,
        "revised_previous": revised_previous,
        "release_time": NOW - timedelta(days=days),
        "impact": impact,
        "provider": provider,
        "data_status": "RELEASED",
    }


def currency_evidence(currency, bullish=True):
    if bullish:
        values = {
            "interest_rate": ("5.5%", "5.25%", "5.25%"),
            "cpi": ("3.5%", "3.1%", "3.2%"),
            "nonfarm_payrolls": ("250K", "180K", "170K"),
            "gdp": ("3.0%", "2.0%", "1.8%"),
        }
    else:
        values = {
            "interest_rate": ("3.5%", "3.75%", "3.75%"),
            "cpi": ("1.8%", "2.2%", "2.3%"),
            "nonfarm_payrolls": ("100K", "180K", "190K"),
            "gdp": ("0.5%", "1.5%", "1.7%"),
        }
    return [
        macro_event(f"{currency}-{name}", currency, name, actual, forecast, previous, days=index + 1)
        for index, (name, (actual, forecast, previous)) in enumerate(values.items())
    ]


class FactorTests(unittest.TestCase):
    def test_central_bank_factor(self):
        result = calculate_policy_factor(currency_evidence("USD"), "USD", now=NOW)
        self.assertEqual(result["status"], "ACTIVE")
        self.assertGreater(result["score"], 0)
        self.assertTrue(result["evidence"][0]["event_id"])

    def test_inflation_factor_uses_policy_context(self):
        event = macro_event("usd-cpi", "USD", "cpi", "3.5%", "3.0%", "3.1%")
        hawkish = calculate_inflation_factor([event], "USD", now=NOW, policy_context={"score": 50})
        dovish = calculate_inflation_factor([event], "USD", now=NOW, policy_context={"score": -50})
        self.assertGreater(hawkish["score"], dovish["score"])

    def test_employment_factor_direction(self):
        strong = calculate_employment_factor([
            macro_event("nfp", "USD", "nonfarm_payrolls", "250K", "180K", "170K")
        ], "USD", now=NOW)
        weak_unemployment = calculate_employment_factor([
            macro_event("unemployment", "USD", "unemployment_rate", "4.5%", "4.1%", "4.0%")
        ], "USD", now=NOW)
        self.assertGreater(strong["score"], 0)
        self.assertLess(weak_unemployment["score"], 0)

    def test_growth_factor_pmi_expansion(self):
        result = calculate_growth_factor([
            macro_event("pmi", "EUR", "manufacturing_pmi", "54", "51", "50")
        ], "EUR", now=NOW)
        self.assertGreater(result["score"], 0)

    def test_quarterly_factor_remains_current_until_next_expected_release(self):
        result = calculate_growth_factor([
            macro_event("old-gdp", "EUR", "gdp", "2.0%", "1.5%", "1.4%", days=60)
        ], "EUR", now=NOW)
        self.assertEqual(result["status"], "ACTIVE")

    def test_recent_macro_evidence_dominates_old(self):
        recent = macro_event("recent", "USD", "gdp", "3%", "2%", "2%", days=2)
        old = macro_event("old", "USD", "gdp", "1%", "2%", "2%", days=60)
        result = calculate_growth_factor([recent, old], "USD", now=NOW)
        self.assertGreater(result["score"], 0)

    def test_material_revision_reduces_confidence(self):
        stable = macro_event("stable", "USD", "gdp", "3%", "2%", "2%")
        revised = macro_event(
            "revised", "USD", "gdp", "3%", "2%", "2%", revised_previous="1%"
        )
        stable_result = calculate_growth_factor([stable], "USD", now=NOW)
        revised_result = calculate_growth_factor([revised], "USD", now=NOW)
        self.assertLess(revised_result["confidence"], stable_result["confidence"])
        self.assertTrue(revised_result["evidence"][0]["revision_affected"])


class WeightingAndPairTests(unittest.TestCase):
    def test_weights_are_centralized_and_sum_to_one(self):
        self.assertAlmostEqual(sum(FACTOR_WEIGHTS.values()), 1.0)
        self.assertEqual(FACTOR_WEIGHTS["policy_score"], 0.30)

    def test_currency_strength_renormalizes_active_factors(self):
        result = build_currency_strength("USD", currency_evidence("USD"), history_lookup=lambda _event: [], now=NOW)
        self.assertEqual(result["status"], "ACTIVE")
        self.assertGreaterEqual(result["coverage"], MINIMUM_ACTIVE_COVERAGE)
        self.assertAlmostEqual(sum(result["normalized_weights"].values()), 1.0, places=3)

    def test_coverage_threshold_blocks_large_pair_score(self):
        result = synthesize_pair_bias("EURUSD", {
            "EUR": {"score": 70, "coverage": 0.35},
            "USD": {"score": -20, "coverage": 0.35},
        })
        self.assertEqual(result["pair_score"], 90)
        self.assertEqual(result["direction"], "NEUTRAL")
        self.assertEqual(result["status"], "INSUFFICIENT_DATA")

    def test_complete_pair_synthesis(self):
        observations = currency_evidence("EUR", bullish=True) + currency_evidence("USD", bullish=False)
        state = calculate_fundamental_state("EURUSD", observations, history_lookup=lambda _event: [], now=NOW)
        self.assertEqual(state["pair"]["status"], "ACTIVE")
        self.assertEqual(state["pair"]["direction"], "BUY")

    def test_conflicting_factors_reduce_confidence(self):
        def currency(scores):
            factors = {
                name: {"score": score, "status": "ACTIVE", "confidence": 70, "evidence_count": 4, "provisional_count": 0, "evidence": []}
                for name, score in scores.items()
            }
            return {"score": 20, "coverage": 1, "confidence": 70, "active_factors": list(factors), "factors": factors}
        aligned = {"EUR": currency({"policy_score": 40, "growth_score": 30}), "USD": currency({"policy_score": -20, "growth_score": -10})}
        conflict = {"EUR": currency({"policy_score": 40, "growth_score": -30}), "USD": currency({"policy_score": -20, "growth_score": 10})}
        pair = {"pair_score": 40, "status": "ACTIVE"}
        self.assertGreater(calculate_confidence(aligned, pair), calculate_confidence(conflict, pair))

    def test_top_reasons_are_factor_level_and_evidence_backed(self):
        result = build_currency_strength("USD", currency_evidence("USD"), history_lookup=lambda _event: [], now=NOW)
        reasons = select_top_reasons({"USD": result})
        self.assertLessEqual(len(reasons), 3)
        self.assertTrue(all(reason["evidence_event_ids"] for reason in reasons))


class IngestionAndHealthTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)

    def tearDown(self):
        self.engine.dispose()

    def test_background_ingestion_fetches_when_due(self):
        fetcher = Mock(return_value=[{"event": "CPI"}])
        result = run_fundamental_ingestion_if_due(
            now=NOW,
            fetcher=fetcher,
            health_reader=lambda **_kwargs: {"last_successful_provider_fetch": None},
        )
        self.assertEqual(result["status"], "FETCHED")
        fetcher.assert_called_once()

    def test_background_ingestion_respects_durable_last_fetch(self):
        fetcher = Mock(return_value=[])
        result = run_fundamental_ingestion_if_due(
            now=NOW,
            fetcher=fetcher,
            health_reader=lambda **_kwargs: {"last_provider_fetch_attempt": NOW - timedelta(minutes=5)},
        )
        self.assertEqual(result["status"], "NOT_DUE")
        fetcher.assert_not_called()

    def test_provider_failure_health(self):
        record_failed_fetch("fmp", "timeout", started_at=NOW, session_factory=self.sessions)
        health = provider_health(now=NOW, session_factory=self.sessions)
        self.assertEqual(health["provider_failures_recent"], 1)
        self.assertEqual(health["providers"]["fmp"]["status"], "FAILED")

    def test_append_only_revision_health_data(self):
        raw = [{
            "event_name": "US GDP", "indicator": "gdp", "currency": "USD",
            "impact": "HIGH", "release_time": NOW.isoformat(), "actual": "2%",
            "forecast": "1.5%", "previous": "1%",
        }]
        persist_calendar_batch("fmp", raw, raw, completed_at=NOW, session_factory=self.sessions)
        persist_calendar_batch("fmp", [{**raw[0], "actual": "2.2%", "revised_previous": "0.8%"}], completed_at=NOW, session_factory=self.sessions)
        health = provider_health(now=NOW, session_factory=self.sessions)
        self.assertEqual(health["observation_count"], 2)


class ApiResponseTests(unittest.TestCase):
    def test_phase2_insight_is_informational(self):
        observations = currency_evidence("EUR", bullish=True) + currency_evidence("USD", bullish=False)
        with patch("fundamentals.insight_service.provider_health", return_value={
            "observation_count": len(observations), "providers": {},
            "provider_failures_recent": 0,
        }):
            insight = get_fundamental_insight(
                "EURUSD", now=NOW, observations=observations, next_event={},
                persist=False, ingest=False, history_lookup_override=lambda _event: [],
            )
        self.assertEqual(insight["overall_bias"]["status"], "ACTIVE")
        self.assertEqual(insight["trading_guidance"]["preference"], "PREFER_BUY")
        self.assertFalse(insight["trading_guidance"]["execution_connected"])
        self.assertTrue(insight["read_only"])

    def test_next_event_contract_contains_only_approved_fields(self):
        next_event = macro_event("next", "USD", "cpi", None, "3%", "2.8%", days=-1)
        with patch("fundamentals.insight_service.provider_health", return_value={"providers": {}}):
            insight = get_fundamental_insight(
                "EURUSD", now=NOW, observations=[], next_event=next_event,
                persist=False, ingest=False,
            )
        self.assertEqual(set(insight["next_high_impact_event"]), {
            "event_name", "currency", "release_time", "countdown", "previous",
            "forecast", "actual", "impact", "source", "data_status",
        })


if __name__ == "__main__":
    unittest.main()
