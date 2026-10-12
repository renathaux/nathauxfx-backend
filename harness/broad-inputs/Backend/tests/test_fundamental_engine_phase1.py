import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from fundamentals.confidence import calculate_confidence
from fundamentals.engine import calculate_fundamental_state
from fundamentals.explanations import select_top_reasons
from fundamentals.factors.surprises import (
    recency_weight,
    score_currency_surprises,
    score_event_surprise,
)
from fundamentals.insight_service import get_fundamental_insight
from fundamentals.normalization.events import normalize_economic_event
from fundamentals.pair_bias import synthesize_pair_bias
from fundamentals.repositories.economic_events import persist_calendar_batch
from models import Base, EconomicEventObservation
from routes import fundamentals as fundamentals_route
from services.news_service import build_manual_calendar_events


NOW = datetime(2026, 8, 7, 14, 0, tzinfo=timezone.utc)


def event(
    *,
    event_id="event-1",
    name="US CPI",
    indicator="cpi",
    currency="USD",
    actual="0.5%",
    forecast="0.3%",
    release_time=None,
    impact="HIGH",
    provider="fmp",
):
    return {
        "event_id": event_id,
        "event_name": name,
        "indicator": indicator,
        "currency": currency,
        "country": "United States" if currency == "USD" else "Euro Area",
        "actual": actual,
        "forecast": forecast,
        "previous": "0.2%",
        "revised_previous": None,
        "release_time": release_time or NOW - timedelta(hours=1),
        "impact": impact,
        "provider": provider,
        "data_status": "RELEASED",
    }


class FundamentalNormalizationTests(unittest.TestCase):
    def test_event_normalization(self):
        result = normalize_economic_event({
            "name": "US CPI",
            "country": "United States",
            "impact": "high",
            "date": "2026-08-07T12:30:00Z",
            "actual": "0.4%",
            "forecast": "0.3%",
            "previous": "0.2%",
            "id": "provider-1",
        }, provider="fmp", fetched_at=NOW)
        self.assertEqual(result.currency, "USD")
        self.assertEqual(result.indicator, "cpi")
        self.assertEqual(result.impact, "HIGH")
        self.assertEqual(result.provider_event_id, "provider-1")
        self.assertEqual(result.data_status, "RELEASED")

    def test_unknown_impact_is_explicit(self):
        result = normalize_economic_event({
            "name": "US CPI",
            "currency": "USD",
            "date": "2026-08-07T12:30:00Z",
            "impact": "",
        }, provider="fmp", fetched_at=NOW)
        self.assertEqual(result.impact, "UNKNOWN")

    def test_manual_fallback_does_not_fabricate_generic_event(self):
        sunday = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(build_manual_calendar_events(now=sunday), [])

    def test_manual_static_event_is_unreliable_and_not_high_impact(self):
        tuesday = datetime(2026, 8, 11, 10, 0, tzinfo=timezone.utc)
        events = build_manual_calendar_events(now=tuesday)
        self.assertTrue(events)
        self.assertTrue(all(item["impact"] == "UNKNOWN" for item in events))
        self.assertTrue(all(item["data_status"] == "UNRELIABLE_STATIC" for item in events))


class SurpriseFactorTests(unittest.TestCase):
    def test_surprise_direction(self):
        result = score_event_surprise(
            event(name="US Unemployment Rate", indicator="unemployment_rate", actual="4.4%", forecast="4.1%"),
            now=NOW,
        )
        self.assertLess(result["score"], 0)

    def test_provisional_surprise_scoring_is_labeled(self):
        result = score_event_surprise(event(), historical_surprises=[], now=NOW)
        self.assertEqual(result["status"], "PROVISIONAL")
        self.assertEqual(result["method"], "PROVISIONAL_NORMALIZED_SURPRISE")
        self.assertGreater(result["score"], 0)

    def test_historical_z_score_scoring(self):
        history = [-0.1, 0.1, -0.2, 0.2, -0.15, 0.15, -0.05, 0.05]
        result = score_event_surprise(event(), historical_surprises=history, now=NOW)
        self.assertEqual(result["status"], "STANDARDIZED")
        self.assertEqual(result["method"], "HISTORICAL_Z_SCORE")
        self.assertIsNotNone(result["standardized_surprise"])

    def test_recency_decay(self):
        recent = recency_weight(NOW - timedelta(days=1), now=NOW)
        old = recency_weight(NOW - timedelta(days=60), now=NOW)
        self.assertGreater(recent, old)
        self.assertAlmostEqual(old, 0.25, places=3)

    def test_currency_score(self):
        result = score_currency_surprises([event()], "USD", now=NOW)
        self.assertEqual(result["status"], "ACTIVE")
        self.assertGreater(result["score"], 0)


class FundamentalSynthesisTests(unittest.TestCase):
    def test_pair_score(self):
        result = synthesize_pair_bias("EURUSD", {
            "EUR": {"score": 35, "coverage": 0.6},
            "USD": {"score": 5, "coverage": 0.6},
        })
        self.assertEqual(result["pair_score"], 30)
        self.assertEqual(result["direction"], "BUY")

    def test_confidence_degrades_with_missing_data(self):
        currency_results = {
            "EUR": {"score": 30, "coverage": 0.2, "confidence": 80, "factors": {}},
            "USD": {"score": 0, "coverage": 0.2, "confidence": 80, "factors": {}},
        }
        confidence = calculate_confidence(
            currency_results,
            {"pair_score": 30, "direction": "NEUTRAL", "status": "INSUFFICIENT_DATA"},
        )
        self.assertLessEqual(confidence, 20)

    def test_top_reason_selection_uses_evidence(self):
        reasons = select_top_reasons({
            "USD": {
                "factors": {
                    "surprise_score": {
                        "evidence": [{
                            "event_id": "event-1",
                            "event_name": "US CPI",
                            "score": 75,
                        }]
                    }
                }
            }
        })
        self.assertEqual(reasons[0]["evidence_event_ids"], ["event-1"])
        self.assertEqual(reasons[0]["direction"], "BULLISH")

    def test_insufficient_data_behavior(self):
        state = calculate_fundamental_state("EURUSD", [], history_lookup=lambda _event: [], now=NOW)
        self.assertEqual(state["pair"]["status"], "INSUFFICIENT_DATA")
        self.assertEqual(state["pair"]["direction"], "NEUTRAL")

    def test_phase1_never_claims_complete_bias_with_only_surprises(self):
        insight = get_fundamental_insight(
            "EURUSD",
            now=NOW,
            observations=[
                event(currency="USD", event_id="usd-1"),
                event(currency="EUR", event_id="eur-1", name="Euro CPI"),
            ],
            next_event={},
            persist=False,
            ingest=False,
            history_lookup_override=lambda _event: [],
        )
        self.assertEqual(insight["overall_bias"]["direction"], "NEUTRAL")
        self.assertEqual(insight["overall_bias"]["status"], "INSUFFICIENT_DATA")
        self.assertEqual(insight["data_quality"]["coverage_percent"], 35.0)
        self.assertTrue(insight["read_only"])


class FundamentalApiTests(unittest.TestCase):
    def test_fundamentals_api(self):
        expected = {
            "symbol": "EURUSD",
            "overall_bias": {"direction": "NEUTRAL"},
            "currency_strength": {},
            "top_reasons": [],
            "next_high_impact_event": None,
            "trading_guidance": {"execution_connected": False},
            "data_quality": {"status": "INSUFFICIENT_DATA"},
            "read_only": True,
        }
        with patch.object(fundamentals_route, "get_fundamental_insight", return_value=expected):
            response = fundamentals_route.fundamental_insight("EURUSD")
        self.assertEqual(response, expected)
        with self.assertRaises(HTTPException) as invalid:
            fundamentals_route.fundamental_insight("GBPUSD")
        self.assertEqual(invalid.exception.status_code, 422)


class FundamentalPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)

    def tearDown(self):
        self.engine.dispose()

    def test_phase1_tables_exist(self):
        tables = set(inspect(self.engine).get_table_names())
        self.assertTrue({
            "economic_events",
            "economic_event_observations",
            "economic_provider_fetches",
            "fundamental_factor_inputs",
            "currency_strength_snapshots",
            "fundamental_insight_snapshots",
        }.issubset(tables))

    def test_observations_are_append_only_for_revisions(self):
        raw = [{
            "event_name": "US CPI",
            "currency": "USD",
            "impact": "HIGH",
            "release_time": "2026-08-07T12:30:00Z",
            "actual": "0.4%",
            "forecast": "0.3%",
            "previous": "0.2%",
            "source": "fmp",
        }]
        persist_calendar_batch("fmp", raw, raw, completed_at=NOW, session_factory=self.sessions)
        persist_calendar_batch("fmp", raw, raw, completed_at=NOW, session_factory=self.sessions)
        revised = [{**raw[0], "actual": "0.5%", "revised_previous": "0.25%"}]
        persist_calendar_batch("fmp", revised, revised, completed_at=NOW, session_factory=self.sessions)
        with self.sessions() as session:
            rows = session.query(EconomicEventObservation).order_by(EconomicEventObservation.id).all()
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0].actual, "0.4%")
            self.assertEqual(rows[1].actual, "0.5%")
            self.assertEqual(rows[1].revised_previous, "0.25%")


if __name__ == "__main__":
    unittest.main()
