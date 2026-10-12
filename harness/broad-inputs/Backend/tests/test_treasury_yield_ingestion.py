import contextlib
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from fundamentals.factors.real_yields import calculate_real_yield_factor
from fundamentals.freshness import evidence_freshness
from fundamentals.gold_engine import calculate_xauusd_state
from fundamentals.market_calendar import market_days_elapsed
from fundamentals.official_backfill import run_official_backfill
from fundamentals.providers.treasury import parse_yield_xml
from models import Base, EconomicEvent, EconomicEventObservation


NOW = datetime(2026, 8, 10, 14, 0, tzinfo=timezone.utc)
ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
 xmlns:d="http://schemas.microsoft.com/ado/2007/08/dataservices"
 xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata">
 <entry><content type="application/xml"><m:properties>
  <d:NEW_DATE m:type="Edm.DateTime">2026-08-06T00:00:00</d:NEW_DATE>
  <d:BC_10YEAR m:type="Edm.Double">4.63</d:BC_10YEAR>
  <d:TC_10YEAR m:type="Edm.Double">2.43</d:TC_10YEAR>
 </m:properties></content></entry>
 <entry><content type="application/xml"><m:properties>
  <d:NEW_DATE m:type="Edm.DateTime">2026-08-07T00:00:00</d:NEW_DATE>
  <d:BC_10YEAR m:type="Edm.Double">4.58</d:BC_10YEAR>
  <d:TC_10YEAR m:type="Edm.Double">2.40</d:TC_10YEAR>
 </m:properties></content></entry>
</feed>"""


def yield_event(indicator, value, when, index):
    return {
        "event_id": f"{indicator}-{index}", "event_name": indicator,
        "indicator": indicator, "currency": "USD", "country": "United States",
        "actual": str(value), "previous": None, "forecast": None,
        "release_time": when, "impact": "UNKNOWN", "provider": "treasury",
        "data_status": "RELEASED",
    }


def macro_factor(score, status="ACTIVE"):
    return {
        "factor": "test", "score": score, "confidence": 85.0 if score is not None else 0.0,
        "status": status, "coverage": 1.0 if score is not None else 0.0,
        "evidence_count": 4 if score is not None else 0, "provisional_count": 0,
        "revision_stability": 1.0 if score is not None else 0.0,
        "evidence": [], "updated_at": NOW if score is not None else None,
    }


@contextlib.contextmanager
def locks(_key):
    yield True


class TreasuryParserTests(unittest.TestCase):
    def test_nominal_10y_series_selection(self):
        rows = parse_yield_xml(ATOM, indicator="us_10y_treasury_yield", source_identity="official-url")
        self.assertEqual([row["value"] for row in rows], [4.63, 4.58])
        self.assertEqual(rows[-1]["observation_date"].isoformat(), "2026-08-07")

    def test_real_10y_series_selection(self):
        rows = parse_yield_xml(ATOM, indicator="us_10y_real_yield", source_identity="official-url")
        self.assertEqual([row["value"] for row in rows], [2.43, 2.40])
        self.assertEqual(len(rows[-1]["content_hash"]), 64)

    def test_invalid_xml_fails_safely(self):
        with self.assertRaisesRegex(Exception, "invalid XML"):
            parse_yield_xml("not xml", indicator="us_10y_real_yield", source_identity="official-url")


class YieldFreshnessTests(unittest.TestCase):
    def event(self, released):
        return yield_event("us_10y_real_yield", 2.4, released, 1)

    def test_friday_observation_is_current_on_weekend(self):
        friday = datetime(2026, 8, 7, tzinfo=timezone.utc)
        sunday = datetime(2026, 8, 9, 20, tzinfo=timezone.utc)
        result = evidence_freshness(self.event(friday), now=sunday)
        self.assertEqual(result["status"], "ACTIVE")
        self.assertEqual(result["market_days_elapsed"], 0)

    def test_holiday_does_not_consume_market_day(self):
        friday = datetime(2026, 9, 4, tzinfo=timezone.utc)
        tuesday_after_labor_day = datetime(2026, 9, 8, 14, tzinfo=timezone.utc)
        self.assertEqual(market_days_elapsed(friday.date(), tuesday_after_labor_day.date()), 1)
        self.assertEqual(evidence_freshness(self.event(friday), now=tuesday_after_labor_day)["status"], "ACTIVE")

    def test_old_daily_yield_is_stale(self):
        released = datetime(2026, 8, 3, tzinfo=timezone.utc)
        self.assertEqual(evidence_freshness(self.event(released), now=NOW)["status"], "STALE")


class YieldTrendTests(unittest.TestCase):
    def observations(self, latest):
        dates = [
            datetime(2026, 8, 3, tzinfo=timezone.utc),
            datetime(2026, 8, 4, tzinfo=timezone.utc),
            datetime(2026, 8, 5, tzinfo=timezone.utc),
            datetime(2026, 8, 6, tzinfo=timezone.utc),
            datetime(2026, 8, 7, tzinfo=timezone.utc),
            datetime(2026, 8, 10, tzinfo=timezone.utc),
        ]
        real_values = [2.00, 2.01, 1.99, 2.02, 2.00, latest]
        nominal_values = [4.20, 4.21, 4.19, 4.22, 4.20, 4.35]
        return [
            yield_event("us_10y_real_yield", value, dates[index], index)
            for index, value in enumerate(real_values)
        ] + [
            yield_event("us_10y_treasury_yield", value, dates[index], index)
            for index, value in enumerate(nominal_values)
        ]

    def test_trend_uses_multi_day_baseline(self):
        result = calculate_real_yield_factor(self.observations(2.20), now=NOW)
        self.assertEqual(result["status"], "ACTIVE")
        self.assertGreater(result["score"], 0)
        self.assertEqual(result["method"], "LATEST_VS_PRIOR_MARKET_OBSERVATION_BASELINE")
        self.assertEqual(result["evidence"][0]["baseline_observation_dates"], [
            "2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07"
        ])

    def test_gold_direction_is_inverted_later_by_gold_engine(self):
        result = calculate_real_yield_factor(self.observations(2.20), now=NOW)
        self.assertGreater(result["score"], 0, "positive factor means USD yield pressure")

    def test_one_observation_cannot_score(self):
        result = calculate_real_yield_factor([
            yield_event("us_10y_real_yield", 2.4, NOW, 1)
        ], now=NOW)
        self.assertEqual(result["status"], "INSUFFICIENT_DATA")

    def test_xauusd_becomes_ready_with_current_real_yield_history(self):
        observations = self.observations(2.20)
        with patch("fundamentals.gold_engine.calculate_policy_factor", return_value=macro_factor(20)), patch(
            "fundamentals.gold_engine.calculate_inflation_factor", return_value=macro_factor(15)
        ), patch("fundamentals.gold_engine.calculate_employment_factor", return_value=macro_factor(10)), patch(
            "fundamentals.gold_engine.calculate_growth_factor", return_value=macro_factor(10)
        ), patch(
            "fundamentals.gold_engine.calculate_risk_sentiment_factor",
            return_value=macro_factor(None, "INSUFFICIENT_DATA"),
        ):
            state = calculate_xauusd_state(observations, now=NOW)
        self.assertEqual(state["status"], "ACTIVE")
        self.assertLess(state["drivers"]["real_yields"]["score"], 0)
        self.assertIn("real_yields", state["active_factors"])
        self.assertIn("risk_sentiment", state["missing_factors"])


class TreasuryBackfillTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)

    def tearDown(self):
        self.engine.dispose()

    def test_backfill_is_idempotent(self):
        item = {
            "event_name": "US 10-Year Real Yield", "indicator": "us_10y_real_yield",
            "currency": "USD", "country": "United States", "impact": "UNKNOWN",
            "release_time": datetime(2026, 8, 7, tzinfo=timezone.utc),
            "actual": "2.40", "previous": "2.43", "provider": "treasury",
            "provider_event_id": "treasury:real:2026-08-07",
            "provider_dataset": "daily_treasury_real_yield_curve", "data_status": "RELEASED",
        }
        args = dict(
            provider="treasury", date_from="2026-08-01", date_to="2026-08-31",
            chunk_days=31, currencies="USD", session_factory=self.sessions,
            bind=self.engine, lock_manager=locks, sleeper=lambda _seconds: None,
            fetcher=lambda *_args: {"request_count": 2, "normalized_events": [item]},
        )
        first = run_official_backfill(**args)
        second = run_official_backfill(**args, resume=True)
        self.assertEqual(first["status"], "COMPLETED")
        self.assertEqual(second["status"], "COMPLETED")
        with self.sessions() as session:
            self.assertEqual(session.query(EconomicEvent).count(), 1)
            self.assertEqual(session.query(EconomicEventObservation).count(), 1)


if __name__ == "__main__":
    unittest.main()
