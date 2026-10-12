import hashlib
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import create_engine, event as sqlalchemy_event
from sqlalchemy.orm import sessionmaker

from fundamentals.insight_service import (
    _history_lookup_from_observations,
    get_fundamental_insight,
)
from fundamentals.gold_insight_service import get_xauusd_fundamental_insight
from fundamentals.gold_config import XAUUSD_ENGINE_INDICATOR_BASES
from fundamentals.insight_cache import _reset_for_tests as reset_insight_cache
from fundamentals.normalization.indicators import EURUSD_ENGINE_INDICATOR_BASES
from fundamentals.repositories import observations as observation_repository
from fundamentals.repositories.observations import (
    _serialize_authoritative,
    historical_surprises_for_series,
    latest_released_observations,
    next_high_impact_event,
    provider_health,
    relevant_reconciled_observation_history,
)
from models import (
    Base,
    CurrencyStrengthSnapshot,
    EconomicEvent,
    EconomicEventObservation,
    EconomicProviderFetch,
    FundamentalFactorInput,
    FundamentalInsightSnapshot,
)
from routes import fundamentals as fundamentals_route


NOW = datetime(2026, 8, 10, 15, 0, tzinfo=timezone.utc)


class FundamentalInsightReliabilityTests(unittest.TestCase):
    def setUp(self):
        reset_insight_cache()
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)

    def tearDown(self):
        reset_insight_cache()
        self.engine.dispose()

    def _seed_history(self, count=240):
        with self.sessions() as session:
            for index in range(count):
                currency = "USD" if index % 2 else "EUR"
                released = NOW - timedelta(days=index + 1)
                economic_event = EconomicEvent(
                    event_id=f"macro-{index}",
                    event_name=f"{currency} CPI",
                    indicator="cpi_y_y",
                    country="United States" if currency == "USD" else "Euro Area",
                    currency=currency,
                    impact="HIGH",
                    release_time=released,
                    provider="bls" if currency == "USD" else "eurostat",
                    provider_event_id=f"provider-{index}",
                    data_status="RELEASED",
                    first_seen_at=released,
                    last_seen_at=released,
                )
                session.add(economic_event)
                session.flush()
                session.add(EconomicEventObservation(
                    observation_hash=f"{index:064x}",
                    economic_event_id=economic_event.id,
                    actual=str(2.0 + index / 1000),
                    forecast="2.0",
                    previous="1.9",
                    provider=economic_event.provider,
                    provider_timestamp=released,
                    fetched_at=released,
                    data_status="RELEASED",
                    raw_payload={},
                ))
            session.add(EconomicProviderFetch(
                provider="bls",
                started_at=NOW - timedelta(minutes=2),
                completed_at=NOW - timedelta(minutes=1),
                status="SUCCESS",
                raw_event_count=count,
                normalized_event_count=count,
                error=None,
            ))
            session.commit()

    def _seed_production_scale_history(self, relevant_count=300, irrelevant_count=491):
        relevant_indicators = (
            "cpi_y_y", "core_cpi_y_y", "nonfarm_payrolls",
            "unemployment_rate", "gdp_q_q", "retail_sales_m_m",
            "fed_interest_rate", "ecb_interest_rate",
        )
        irrelevant_indicators = (
            "us_10y_real_yield", "us_10y_treasury_yield",
            "baker_hughes_us_oil_rig_count", "financial_stress_index",
        )
        with self.sessions() as session:
            for index in range(relevant_count + irrelevant_count):
                relevant = index < relevant_count
                indicator = (
                    relevant_indicators[index % len(relevant_indicators)]
                    if relevant
                    else irrelevant_indicators[index % len(irrelevant_indicators)]
                )
                currency = "USD" if index % 2 else "EUR"
                released = NOW - timedelta(days=(index % 730) + 1)
                event_id = f"scale-{index}"
                economic_event = EconomicEvent(
                    event_id=event_id,
                    event_name=f"{currency} {indicator}",
                    indicator=indicator,
                    country="United States" if currency == "USD" else "Euro Area",
                    currency=currency,
                    impact="HIGH",
                    release_time=released,
                    provider="bls" if currency == "USD" else "eurostat",
                    provider_event_id=f"scale-provider-{index}",
                    data_status="RELEASED",
                    first_seen_at=released,
                    last_seen_at=released,
                )
                session.add(economic_event)
                session.flush()
                session.add(EconomicEventObservation(
                    observation_hash=hashlib.sha256(event_id.encode()).hexdigest(),
                    economic_event_id=economic_event.id,
                    actual=str(2.0 + (index % 25) / 10),
                    forecast=str(1.9 + (index % 25) / 10),
                    previous=str(1.8 + (index % 25) / 10),
                    provider=economic_event.provider,
                    provider_timestamp=released,
                    fetched_at=released,
                    data_status="RELEASED",
                    raw_payload={"source": economic_event.provider, "padding": "x" * 512},
                ))
            session.commit()

    def test_request_path_has_bounded_select_count_and_no_provider_call(self):
        self._seed_history()
        statements = []

        def capture(_connection, _cursor, statement, _parameters, _context, _many):
            if statement.lstrip().upper().startswith("SELECT"):
                statements.append(statement)

        sqlalchemy_event.listen(self.engine, "before_cursor_execute", capture)
        started = time.perf_counter()
        try:
            with patch("services.news_service.fetch_calendar_events") as provider_fetch:
                response = get_fundamental_insight(
                    "EURUSD",
                    now=NOW,
                    session_factory=self.sessions,
                    persist=False,
                    ingest=False,
                )
        finally:
            sqlalchemy_event.remove(self.engine, "before_cursor_execute", capture)

        self.assertEqual(response["symbol"], "EURUSD")
        self.assertLessEqual(len(statements), 8)
        observation_loads = [
            statement for statement in statements
            if "economic_event_observations" in statement
            and "economic_events.release_time DESC" in statement
        ]
        self.assertEqual(len(observation_loads), 1)
        self.assertLess(time.perf_counter() - started, 2.0)
        provider_fetch.assert_not_called()

    def test_eurusd_load_filters_irrelevant_series_without_shortening_history(self):
        self._seed_production_scale_history(relevant_count=400, irrelevant_count=40)
        timings = {}
        current, history = relevant_reconciled_observation_history(
            ("EUR", "USD"),
            EURUSD_ENGINE_INDICATOR_BASES,
            now=NOW,
            session_factory=self.sessions,
            timing=timings,
        )
        loaded_indicators = {item["indicator"] for item in history}
        self.assertTrue(loaded_indicators)
        self.assertNotIn("us_10y_real_yield", loaded_indicators)
        self.assertNotIn("us_10y_treasury_yield", loaded_indicators)
        self.assertNotIn("baker_hughes_us_oil_rig_count", loaded_indicators)
        self.assertNotIn("financial_stress_index", loaded_indicators)
        def aware(value):
            return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value

        self.assertTrue(any(
            aware(item["release_time"]) < NOW - timedelta(days=365)
            for item in history
        ))
        self.assertTrue(all(
            aware(item["release_time"]) >= NOW - timedelta(days=365)
            for item in current
        ))
        self.assertEqual(timings["historical_surprise_query_ms"], 0.0)
        self.assertEqual(timings["historical_surprise_reconciliation_ms"], 0.0)

    def test_optimized_result_matches_legacy_result_semantics(self):
        self._seed_history()
        with self.sessions() as session:
            released = NOW - timedelta(days=2)
            event = EconomicEvent(
                event_id="irrelevant-yield", event_name="US 10Y real yield",
                indicator="us_10y_real_yield", country="United States",
                currency="USD", impact="LOW", release_time=released,
                provider="treasury", provider_event_id="treasury-yield",
                data_status="RELEASED", first_seen_at=released,
                last_seen_at=released,
            )
            session.add(event)
            session.flush()
            session.add(EconomicEventObservation(
                observation_hash="f" * 64, economic_event_id=event.id,
                actual="1.7", forecast=None, previous="1.5",
                provider="treasury", provider_timestamp=released,
                fetched_at=released, data_status="RELEASED", raw_payload={},
            ))
            session.commit()

        legacy_observations = latest_released_observations(
            ("EUR", "USD"), now=NOW, session_factory=self.sessions
        )
        legacy_history = historical_surprises_for_series(
            {(item["currency"], item["indicator"]) for item in legacy_observations},
            session_factory=self.sessions,
        )
        upcoming = next_high_impact_event(
            ("EUR", "USD"), now=NOW, session_factory=self.sessions
        )
        legacy = get_fundamental_insight(
            "EURUSD", now=NOW, observations=legacy_observations,
            next_event=upcoming or {}, persist=False, ingest=False,
            session_factory=self.sessions,
            history_lookup_override=_history_lookup_from_observations(legacy_history),
        )
        optimized = get_fundamental_insight(
            "EURUSD", now=NOW, persist=False, ingest=False,
            session_factory=self.sessions,
        )
        self.assertEqual(optimized["overall_bias"], legacy["overall_bias"])
        self.assertEqual(optimized["currency_strength"], legacy["currency_strength"])
        self.assertEqual(optimized["top_reasons"], legacy["top_reasons"])
        self.assertEqual(optimized["next_high_impact_event"], legacy["next_high_impact_event"])
        self.assertEqual(optimized["trading_guidance"], legacy["trading_guidance"])
        for key in (
            "coverage_percent", "active_factors", "missing_factors",
            "provisional_factor_count", "engine_readiness", "status",
        ):
            self.assertEqual(optimized["data_quality"][key], legacy["data_quality"][key])

    def test_raw_payload_validation_metadata_is_computed_once_per_observation(self):
        released = NOW - timedelta(days=1)
        event = EconomicEvent(
            event_id="payload-event", event_name="US CPI", indicator="cpi_y_y",
            country="United States", currency="USD", impact="HIGH",
            release_time=released, provider="bls", provider_event_id="payload-provider",
            data_status="RELEASED", first_seen_at=released, last_seen_at=released,
        )
        observation = EconomicEventObservation(
            observation_hash="a" * 64, actual="3.0", forecast="2.9",
            previous="2.8", revised_previous="2.85", provider="bls",
            provider_timestamp=released, fetched_at=released,
            data_status="RELEASED",
            raw_payload={"nested": {"release_time": released.isoformat()}},
        )
        with patch(
            "fundamentals.repositories.observations._observation_validation_metadata",
            wraps=observation_repository._observation_validation_metadata,
        ) as metadata:
            _serialize_authoritative(event, [observation])
        self.assertEqual(metadata.call_count, 1)

    def test_timing_record_uses_production_visible_logger(self):
        with self.assertLogs("uvicorn.error", level="INFO") as records:
            get_fundamental_insight(
                "EURUSD", now=NOW, observations=[], next_event={},
                persist=False, ingest=False, session_factory=self.sessions,
            )
        self.assertTrue(any(
            "FUNDAMENTAL_INSIGHT_TIMING" in line and '"symbol": "EURUSD"' in line
            for line in records.output
        ))

    def test_production_scale_request_is_below_two_seconds(self):
        self._seed_production_scale_history()
        started = time.perf_counter()
        get_fundamental_insight(
            "EURUSD", now=NOW, session_factory=self.sessions,
            persist=False, ingest=False,
        )
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 2.0)

    def test_in_memory_history_is_series_scoped_and_strictly_earlier(self):
        observations = [
            {
                "event_id": f"usd-{index}",
                "currency": "USD",
                "indicator": "cpi_y_y",
                "actual": str(2 + index),
                "forecast": "2",
                "release_time": NOW - timedelta(days=3 - index),
            }
            for index in range(3)
        ] + [{
            "event_id": "eur-1",
            "currency": "EUR",
            "indicator": "cpi_y_y",
            "actual": "2.1",
            "forecast": "2",
            "release_time": NOW - timedelta(days=4),
        }]
        lookup = _history_lookup_from_observations(observations)
        history = lookup(observations[2])
        self.assertEqual([item["event_id"] for item in history], ["usd-0", "usd-1"])

    def test_health_keeps_official_provider_visible_beyond_latest_100_rows(self):
        with self.sessions() as session:
            session.add(EconomicProviderFetch(
                provider="bls", started_at=NOW - timedelta(days=1),
                completed_at=NOW - timedelta(days=1), status="SUCCESS",
                raw_event_count=6, normalized_event_count=6,
            ))
            for index in range(101):
                completed = NOW - timedelta(minutes=index)
                session.add(EconomicProviderFetch(
                    provider="fmp", started_at=completed - timedelta(seconds=1),
                    completed_at=completed, status="FAILED",
                    raw_event_count=0, normalized_event_count=0, error="HTTP 402",
                ))
            session.commit()
        result = provider_health(now=NOW, session_factory=self.sessions)
        self.assertIn("bls", result["providers"])
        self.assertEqual(result["providers"]["bls"]["status"], "SUCCESS")

    def test_route_disables_snapshot_writes(self):
        expected = {"symbol": "EURUSD", "read_only": True}
        with patch.object(
            fundamentals_route, "get_fundamental_insight", return_value=expected
        ) as insight:
            self.assertEqual(fundamentals_route.fundamental_insight("EURUSD"), expected)
        insight.assert_called_once_with("EURUSD", persist=False)

    def test_read_only_get_creates_no_fundamental_snapshot_rows(self):
        self._seed_history(count=12)
        before = {}
        with self.sessions() as session:
            for model in (
                FundamentalFactorInput,
                CurrencyStrengthSnapshot,
                FundamentalInsightSnapshot,
            ):
                before[model] = session.query(model).count()
        get_fundamental_insight(
            "EURUSD", now=NOW, session_factory=self.sessions,
            persist=False, ingest=False,
        )
        with self.sessions() as session:
            for model, count in before.items():
                self.assertEqual(session.query(model).count(), count)

    def test_xauusd_does_not_promote_stale_inflation_or_employment(self):
        stale_time = NOW - timedelta(days=70)
        observations = [
            {
                "event_id": "old-cpi", "event_name": "US CPI y/y",
                "indicator": "cpi_y_y", "currency": "USD",
                "actual": "3.5", "forecast": "3.4", "previous": "3.3",
                "revised_previous": None, "release_time": stale_time,
                "impact": "HIGH", "provider": "bls", "data_status": "RELEASED",
            },
            {
                "event_id": "old-nfp", "event_name": "US Non-Farm Payrolls",
                "indicator": "nonfarm_payrolls", "currency": "USD",
                "actual": "150", "forecast": "140", "previous": "130",
                "revised_previous": None, "release_time": stale_time,
                "impact": "HIGH", "provider": "bls", "data_status": "RELEASED",
            },
        ]
        with patch(
            "fundamentals.gold_insight_service.provider_health",
            return_value={"observation_count": len(observations), "providers": {}},
        ):
            response = get_xauusd_fundamental_insight(
                now=NOW, observations=observations, next_event={}
            )
        self.assertEqual(response["drivers"]["inflation"]["status"], "STALE")
        self.assertEqual(response["drivers"]["employment"]["status"], "STALE")
        self.assertEqual(response["overall_bias"]["status"], "INSUFFICIENT_DATA")

    def test_xauusd_filtered_load_excludes_unrelated_series_with_result_parity(self):
        self._seed_production_scale_history(relevant_count=300, irrelevant_count=491)
        with self.sessions() as session:
            released = NOW - timedelta(days=2)
            unrelated = EconomicEvent(
                event_id="usd-unrelated-oil-rigs",
                event_name="Baker Hughes US Oil Rig Count",
                indicator="baker_hughes_us_oil_rig_count",
                country="United States",
                currency="USD",
                impact="LOW",
                release_time=released,
                provider="fmp",
                provider_event_id="usd-unrelated-oil-rigs",
                data_status="RELEASED",
                first_seen_at=released,
                last_seen_at=released,
            )
            session.add(unrelated)
            session.flush()
            session.add(EconomicEventObservation(
                observation_hash="e" * 64,
                economic_event_id=unrelated.id,
                actual="590",
                forecast="588",
                previous="585",
                provider="fmp",
                provider_timestamp=released,
                fetched_at=released,
                data_status="RELEASED",
                raw_payload={"series": "oil-rigs"},
            ))
            session.commit()
        legacy_observations = latest_released_observations(
            ("USD",), now=NOW, session_factory=self.sessions
        )
        filtered_observations = latest_released_observations(
            ("USD",), now=NOW, session_factory=self.sessions,
            indicators=XAUUSD_ENGINE_INDICATOR_BASES,
        )
        filtered_indicators = {item["indicator"] for item in filtered_observations}
        self.assertNotIn("baker_hughes_us_oil_rig_count", filtered_indicators)
        self.assertLess(len(filtered_observations), len(legacy_observations))
        ignored_observations = [
            item for item in legacy_observations
            if item["indicator"] == "baker_hughes_us_oil_rig_count"
        ]
        self.assertTrue(ignored_observations)
        parity_baseline = filtered_observations + ignored_observations
        with patch(
            "fundamentals.gold_insight_service.provider_health",
            return_value={"observation_count": len(parity_baseline), "providers": {}},
        ):
            legacy = get_xauusd_fundamental_insight(
                now=NOW, observations=parity_baseline, next_event={}
            )
            filtered = get_xauusd_fundamental_insight(
                now=NOW, observations=filtered_observations, next_event={}
            )
        for key in (
            "overall_bias", "gold_support_score", "usd_macro_score", "drivers",
            "top_reasons", "trading_guidance",
        ):
            self.assertEqual(filtered[key], legacy[key])

    def test_eurusd_xauusd_route_isolation(self):
        eur = {"symbol": "EURUSD"}
        xau = {"symbol": "XAUUSD"}
        with patch.object(
            fundamentals_route, "get_fundamental_insight", return_value=eur
        ), patch.object(
            fundamentals_route, "get_xauusd_fundamental_insight", return_value=xau
        ):
            self.assertEqual(fundamentals_route.fundamental_insight("EURUSD"), eur)
            self.assertEqual(fundamentals_route.fundamental_insight("XAUUSD"), xau)


if __name__ == "__main__":
    unittest.main()
