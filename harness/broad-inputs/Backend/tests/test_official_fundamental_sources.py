import os
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from fundamentals.authority import authority_rank, choose_field
from fundamentals.normalization.indicators import normalize_indicator
from fundamentals.official_preflight import run_official_preflight
from fundamentals.providers import bea, bls, ecb, eurostat, federal_reserve
from fundamentals.repositories.economic_events import persist_calendar_batch
from fundamentals.repositories.observations import latest_released_observations
from models import (
    Base, EconomicEvent, EconomicEventDisagreement, EconomicEventObservation,
    EconomicEventProviderLink,
)


class Response:
    def __init__(self, *, json_data=None, text="", status=200, headers=None):
        self._json = json_data
        self.text = text
        self.status_code = status
        self.headers = headers or {}

    def json(self):
        return self._json


SCHEDULE = """
<table>
<tr><td>Consumer Price Index</td><td>June 10</td><td>08:30 AM</td></tr>
<tr><td>Producer Price Index</td><td>June 11</td><td>08:30 AM</td></tr>
</table>
"""


def bls_payload():
    values = {
        "CUSR0000SA0": {(2026, 5): 320, (2025, 5): 310, (2026, 4): 318, (2025, 4): 309},
        "CUSR0000SA0L1E": {(2026, 5): 330, (2025, 5): 320, (2026, 4): 328, (2025, 4): 319},
        "WPSFD4": {(2026, 5): 150, (2025, 5): 145, (2026, 4): 149, (2025, 4): 144},
    }
    series = []
    for series_id, entries in values.items():
        series.append({"seriesID": series_id, "data": [
            {"year": str(year), "period": f"M{month:02d}", "value": str(value)}
            for (year, month), value in entries.items()
        ]})
    return {"status": "REQUEST_SUCCEEDED", "Results": {"series": series}}


class NormalizationAndAuthorityTests(unittest.TestCase):
    def test_canonical_qualifiers_are_not_collapsed(self):
        self.assertEqual(normalize_indicator("cpi_y_y"), "cpi_y_y")
        self.assertEqual(normalize_indicator("core_cpi_m_m"), "core_cpi_m_m")
        self.assertNotEqual(normalize_indicator("cpi_y_y"), normalize_indicator("core_cpi_y_y"))

    def test_field_level_authority(self):
        self.assertLess(authority_rank("actual", "bls", "cpi_y_y", "USD"),
                        authority_rank("actual", "jblanked_mql5", "cpi_y_y", "USD"))
        self.assertLess(authority_rank("forecast", "jblanked_mql5", "cpi_y_y", "USD"),
                        authority_rank("forecast", "bls", "cpi_y_y", "USD"))
        selected = choose_field("actual", [
            {"value": "3.2%", "provider": "jblanked_mql5"},
            {"value": "3.1%", "provider": "bls"},
        ], "cpi_y_y", "USD")
        self.assertEqual(selected["provider"], "bls")


class OfficialAdapterTests(unittest.TestCase):
    def test_bls_cpi_core_and_ppi_with_official_timestamp(self):
        result = bls.fetch_range(
            "2026-06-08", "2026-06-14", ("USD",),
            request_get=lambda *_args, **_kwargs: Response(text=SCHEDULE),
            request_post=lambda *_args, **_kwargs: Response(json_data=bls_payload()),
        )
        indicators = {item["indicator"] for item in result["normalized_events"]}
        self.assertTrue({"cpi_y_y", "core_cpi_y_y", "ppi_y_y"}.issubset(indicators))
        self.assertTrue(all(item["forecast"] is None for item in result["normalized_events"]))
        self.assertTrue(all(item["release_time"].tzinfo for item in result["normalized_events"]))

    def test_ecb_policy_decision(self):
        archive = '<a href="/press/pr/date/2026/html/ecb.mp260611~abc.en.html">Decision</a>'
        statement = "The interest rate on the deposit facility 2.00 percent. The main refinancing operations 2.15 percent. The marginal lending facility 2.40 percent."
        def getter(url, **_kwargs):
            return Response(text=statement if "mp260611" in url else archive)
        result = ecb.fetch_range("2026-06-08", "2026-06-14", ("EUR",), request_get=getter)
        self.assertEqual(len(result["normalized_events"]), 1)
        self.assertEqual(result["normalized_events"][0]["actual"], "2.15%")
        self.assertEqual(result["normalized_events"][0]["indicator"], "ecb_interest_rate")

    def test_ecb_combined_rate_sentence_preserves_rate_order(self):
        rates = ecb._rates(
            "The interest rates on the deposit facility, the main refinancing operations "
            "and the marginal lending facility will be increased to 2.25%, 2.40% and 2.65% respectively."
        )
        self.assertEqual(rates["deposit_facility"], "2.25%")
        self.assertEqual(rates["main_refinancing_operations"], "2.40%")
        self.assertEqual(rates["marginal_lending_facility"], "2.65%")

    def test_fed_policy_parser(self):
        calendar = '<a href="/newsevents/pressreleases/monetary20260610a.htm">Statement</a>'
        statement = "For release at 2:00 p.m. EDT. The Committee decided to maintain the target range for the federal funds rate at 3.5 to 3.75 percent."
        def getter(url, **_kwargs):
            return Response(text=statement if "a.htm" in url else calendar)
        result = federal_reserve.fetch_range("2026-06-08", "2026-06-14", ("USD",), request_get=getter)
        self.assertEqual(result["normalized_events"][0]["actual"], "3.5-3.75%")

    def test_policy_event_id_and_timestamp_are_stable(self):
        calendar = '<a href="/newsevents/pressreleases/monetary20260610a.htm">Statement</a>'
        statement = "For release at 2:00 p.m. EDT. The Committee decided to maintain the target range for the federal funds rate at 3.5 to 3.75 percent."
        getter = lambda url, **_kwargs: Response(text=statement if "a.htm" in url else calendar)
        first = federal_reserve.fetch_range("2026-06-08", "2026-06-14", ("USD",), request_get=getter)
        second = federal_reserve.fetch_range("2026-06-08", "2026-06-14", ("USD",), request_get=getter)
        self.assertEqual(first["normalized_events"][0]["provider_event_id"], second["normalized_events"][0]["provider_event_id"])
        self.assertEqual(first["normalized_events"][0]["release_time"], second["normalized_events"][0]["release_time"])

    def test_fed_mixed_fraction_rate_and_official_release_time(self):
        statement = "For release at 2:00 p.m. EDT. The Committee decided to maintain the target range for the federal funds rate at 3-1/2 to 3-3/4 percent."
        self.assertEqual(federal_reserve._target_rate(statement), "3.5-3.75%")
        release = federal_reserve._statement_release_time(statement, datetime(2026, 6, 17).date())
        self.assertEqual(release.astimezone(timezone.utc).isoformat(), "2026-06-17T18:00:00+00:00")

    def test_eurostat_period_values_do_not_invent_release_time(self):
        payload = {"dimension": {"time": {"category": {"index": {"2026-05": 0}}}}, "value": {"0": 2.1}, "updated": "2026-06-10T00:00:00Z"}
        result = eurostat.fetch_range(
            "2026-06-08", "2026-06-14", ("EUR",),
            request_get=lambda *_args, **_kwargs: Response(json_data=payload),
        )
        self.assertTrue(result["period_observations"])
        self.assertTrue(all(item["release_time"] is None for item in result["period_observations"]))

    def test_eurostat_uses_frequency_appropriate_period_bounds(self):
        calls = []
        payload = {"dimension": {"time": {"category": {"index": {}}}}, "value": {}}
        def getter(_url, **kwargs):
            calls.append(kwargs["params"])
            return Response(json_data=payload)
        eurostat.fetch_range("2026-06-08", "2026-06-14", ("EUR",), request_get=getter)
        self.assertTrue(any(call.get("sinceTimePeriod") == "2025-01" for call in calls))
        self.assertTrue(any(call.get("sinceTimePeriod") == "2025-Q1" for call in calls))

    def test_eurostat_calendar_date_only_does_not_become_release_event(self):
        ics = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:estat-inflation-20260617
DTSTART;VALUE=DATE:20260617
SUMMARY:Inflation (HICP)
CATEGORIES:Euro indicators release
END:VEVENT
END:VCALENDAR"""
        payload = {"dimension": {"time": {"category": {"index": {"2026-05": 0}}}}, "value": {"0": 2.1}}
        def getter(url, **_kwargs):
            return Response(text=ics) if "eventsIcal" in url else Response(json_data=payload)
        result = eurostat.fetch_range("2026-06-01", "2026-06-30", ("EUR",), request_get=getter)
        self.assertEqual(result["calendar_event_count"], 1)
        self.assertEqual(result["calendar_entries"][0]["timestamp_precision"], "DATE_ONLY")
        self.assertEqual(result["normalized_events"], [])

    def test_eurostat_exact_atom_timestamp_reconciles_official_hicp(self):
        ics = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:estat-hicp-20260617
DTSTART;VALUE=DATE:20260617
SUMMARY:Inflation (HICP)
X-CATEGORY:Data release\\,Euro indicators release
END:VEVENT
END:VCALENDAR"""
        atom = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry>
<title>Euro area annual inflation at 2.1%</title>
<id>https://ec.europa.eu/eurostat/product?code=2-17062026-ap</id>
<published>2026-06-17T09:00:00Z</published>
<summary>In May 2026, the euro area annual inflation rate was 2.1%.</summary>
</entry></feed>"""
        payload = {
            "dimension": {"time": {"category": {"index": {"2026-04": 0, "2026-05": 1}}}},
            "value": {"0": 2.0, "1": 2.1},
        }
        def getter(url, **_kwargs):
            if "eventsIcal" in url:
                return Response(text=ics)
            if "news/euro-indicators" in url:
                return Response(text=atom)
            return Response(json_data=payload)
        result = eurostat.fetch_range("2026-06-01", "2026-06-30", ("EUR",), request_get=getter)
        event = result["normalized_events"][0]
        self.assertEqual(event["indicator"], "hicp_y_y")
        self.assertEqual(event["actual"], 2.1)
        self.assertEqual(event["previous"], 2.0)
        self.assertEqual(event["release_time"].isoformat(), "2026-06-17T09:00:00+00:00")
        self.assertIn("2-17062026-ap", event["provider_event_id"])

    def test_eurostat_flash_hicp_is_not_filled_with_later_final_series(self):
        ics = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:estat-flash-20260602
DTSTART;VALUE=DATE:20260602
SUMMARY:Flash estimate inflation euro area
END:VEVENT
END:VCALENDAR"""
        atom = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry>
<title>Euro area annual inflation flash estimate</title><id>flash</id>
<published>2026-06-02T09:00:00Z</published><summary>In May 2026 inflation was 2.0%.</summary>
</entry></feed>"""
        payload = {"dimension": {"time": {"category": {"index": {"2026-05": 0}}}}, "value": {"0": 2.1}}
        def getter(url, **_kwargs):
            if "eventsIcal" in url:
                return Response(text=ics)
            if "news/euro-indicators" in url:
                return Response(text=atom)
            return Response(json_data=payload)
        result = eurostat.fetch_range("2026-06-01", "2026-06-07", ("EUR",), request_get=getter)
        self.assertEqual(result["normalized_events"], [])

    def test_eurostat_historical_release_actual_is_not_overwritten_by_latest_revision(self):
        ics = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:estat-gdp-20260605
DTSTART;VALUE=DATE:20260605
SUMMARY:GDP main aggregates and employment
END:VEVENT
END:VCALENDAR"""
        atom = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry>
<title>GDP down by 0.2% and employment up by 0.1% in the euro area</title>
<id>https://ec.europa.eu/eurostat/product?code=2-05062026-ap</id>
<published>2026-06-05T09:00:00Z</published>
<summary>In the first quarter of 2026, seasonally adjusted GDP decreased by 0.2% and employment increased by 0.1%.</summary>
</entry></feed>"""
        payload = {
            "dimension": {"time": {"category": {"index": {"2025-Q4": 0, "2026-Q1": 1}}}},
            "value": {"0": 0.2, "1": 0.0},
        }
        def getter(url, **_kwargs):
            if "eventsIcal" in url:
                return Response(text=ics)
            if "news/euro-indicators" in url:
                return Response(text=atom)
            return Response(json_data=payload)
        result = eurostat.fetch_range("2026-06-01", "2026-06-30", ("EUR",), request_get=getter)
        by_indicator = {item["indicator"]: item for item in result["normalized_events"]}
        self.assertEqual(by_indicator["gdp_q_q"]["actual"], -0.2)
        self.assertEqual(by_indicator["gdp_q_q"]["raw"]["latest_revised_period_value"], 0.0)
        self.assertEqual(by_indicator["employment_change_q_q"]["actual"], 0.1)

    def test_eurostat_uses_ea20_before_2026_and_ea21_from_2026(self):
        payload = {
            "dimension": {"time": {"category": {"index": {"2025-12": 0, "2026-01": 1}}}},
            "value": {"0": 2.0, "1": 2.1},
        }
        result = eurostat.fetch_range(
            "2025-12-01", "2026-01-31", ("EUR",),
            request_get=lambda url, **_kwargs: Response(text="") if "eventsIcal" in url else Response(json_data=payload),
        )
        geos_by_period = {(item["period"], item["geo"]) for item in result["period_observations"]}
        self.assertIn(("2025-12", "EA20"), geos_by_period)
        self.assertIn(("2026-01", "EA21"), geos_by_period)
        self.assertNotIn(("2026-01", "EA20"), geos_by_period)

    def test_bea_requires_free_key_and_does_not_invent_release_time(self):
        payload = {"BEAAPI": {"Results": {"Data": [{
            "LineDescription": "Gross domestic product", "LineNumber": "1",
            "TimePeriod": "2026Q2", "DataValue": "2.4", "UNIT_MULT": "0",
        }]}}}
        with patch.dict(os.environ, {"BEA_API_KEY": "test-key"}):
            result = bea.fetch_range(
                "2026-06-08", "2026-06-14", ("USD",),
                request_get=lambda *_args, **_kwargs: Response(json_data=payload),
            )
        self.assertTrue(any(item["indicator"] == "gdp" for item in result["period_observations"]))
        self.assertTrue(all(item["release_time"] is None for item in result["period_observations"]))

    def test_bea_emits_event_only_when_official_schedule_has_exact_time(self):
        schedule = """<table><tr><td>June 25 8:30 AM</td><td>GDP (Third Estimate), 1st Quarter 2026</td></tr></table>"""
        payload = {"BEAAPI": {"Results": {"Data": [
            {"LineDescription": "Gross domestic product", "LineNumber": "1", "TimePeriod": "2026Q1", "DataValue": "2.1", "UNIT_MULT": "0"},
            {"LineDescription": "Gross domestic product", "LineNumber": "1", "TimePeriod": "2025Q4", "DataValue": "0.5", "UNIT_MULT": "0"},
        ]}}}
        def getter(url, **_kwargs):
            return Response(text=schedule) if "bea.gov/news/schedule" in url else Response(json_data=payload)
        with patch.dict(os.environ, {"BEA_API_KEY": "test-key"}):
            result = bea.fetch_range("2026-06-01", "2026-06-30", ("USD",), request_get=getter)
        self.assertEqual(len(result["normalized_events"]), 1)
        self.assertEqual(result["normalized_events"][0]["actual"], "2.1")
        self.assertEqual(result["normalized_events"][0]["previous"], "0.5")
        self.assertEqual(result["normalized_events"][0]["release_time"].hour, 12)

    def test_bea_archive_preserves_original_timestamp_offset(self):
        html = """<table><tr class="release-row">
<td><a href="/news/2026/personal-income-and-outlays-may-2026">Personal Income and Outlays, May 2026</a></td>
<td><time datetime="2026-06-25T08:30:00-04:00">June 25, 2026</time></td>
</tr></table>"""
        rows = bea._archive_rows(html)
        self.assertEqual(rows[0]["release_time"].isoformat(), "2026-06-25T08:30:00-04:00")
        self.assertTrue(rows[0]["release_url"].startswith("https://www.bea.gov/news/2026/"))

    def test_bea_release_archive_preserves_original_gdp_and_pce_values(self):
        gdp = "Real gross domestic product (GDP) increased at an annual rate of 2.1 percent in the first quarter."
        pce = (
            "From the preceding month, the PCE price index for May increased 0.4 percent. "
            "Excluding food and energy, the PCE price index increased 0.3 percent."
        )
        self.assertEqual(bea._release_actual("gdp", gdp), 2.1)
        self.assertEqual(bea._release_actual("pce", pce), 0.4)
        self.assertEqual(bea._release_actual("core_pce", pce), 0.3)

    def test_bls_calendar_and_api_failures_are_independent_and_counted(self):
        calls = {"post": 0}
        def poster(*_args, **_kwargs):
            calls["post"] += 1
            return Response(status=503, json_data={})
        with self.assertRaises(Exception) as raised:
            bls.fetch_range(
                "2026-06-08", "2026-06-14", ("USD",),
                request_get=lambda *_args, **_kwargs: Response(status=403),
                request_post=poster,
                sleep=lambda _seconds: None,
            )
        self.assertEqual(calls["post"], 3)
        self.assertGreaterEqual(getattr(raised.exception, "request_count", 0), 5)
        self.assertIn("release calendar", str(raised.exception))
        self.assertIn("API", str(raised.exception))


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        self.release = datetime(2026, 6, 10, 12, 30, tzinfo=timezone.utc)

    def tearDown(self):
        self.engine.dispose()

    def test_official_actual_and_jblanked_forecast_share_event_without_overwrite(self):
        official = [{
            "event_name": "US CPI y/y", "indicator": "cpi_y_y", "currency": "USD",
            "country": "United States", "release_time": self.release,
            "actual": "3.1%", "forecast": None, "previous": "3.0%",
            "provider_event_id": "bls-cpi-2026-05", "provider_dataset": "bls-api",
        }]
        commercial = [{
            "event_name": "US CPI y/y", "indicator": "cpi_y_y", "currency": "USD",
            "country": "United States", "release_time": self.release,
            "actual": "3.2%", "forecast": "3.0%", "previous": "3.0%",
            "provider_event_id": "jb-123", "provider_dataset": "mql5", "impact": "HIGH",
        }]
        persist_calendar_batch("bls", official, official, session_factory=self.sessions)
        persist_calendar_batch("jblanked_mql5", commercial, commercial, session_factory=self.sessions)
        with self.sessions() as session:
            self.assertEqual(session.query(EconomicEvent).count(), 1)
            self.assertEqual(session.query(EconomicEventProviderLink).count(), 2)
            self.assertEqual(session.query(EconomicEventObservation).count(), 2)
            disagreement = session.query(EconomicEventDisagreement).filter_by(field_name="actual").one()
            self.assertEqual(disagreement.authoritative_provider, "bls")
        rows = latest_released_observations(
            ["USD"], now=datetime(2026, 6, 11, tzinfo=timezone.utc), session_factory=self.sessions
        )
        self.assertEqual(rows[0]["actual"], "3.1%")
        self.assertEqual(rows[0]["forecast"], "3.0%")
        self.assertEqual(rows[0]["field_sources"]["actual"], "bls")
        self.assertEqual(rows[0]["field_sources"]["forecast"], "jblanked_mql5")

    def test_same_provider_revision_is_append_only_not_cross_provider_disagreement(self):
        base = [{
            "event_name": "US CPI y/y", "indicator": "cpi_y_y", "currency": "USD",
            "release_time": self.release, "actual": "3.1%",
            "provider_event_id": "bls-cpi-2026-05", "provider_dataset": "bls-api",
        }]
        persist_calendar_batch("bls", base, base, session_factory=self.sessions)
        revised = [{**base[0], "actual": "3.2%", "revised_previous": "3.0%"}]
        persist_calendar_batch("bls", revised, revised, session_factory=self.sessions)
        with self.sessions() as session:
            self.assertEqual(session.query(EconomicEventObservation).count(), 2)
            self.assertEqual(session.query(EconomicEventDisagreement).count(), 0)

    def test_official_modules_do_not_import_trading_or_broker(self):
        forbidden = ("ctrader", "strategies", "risk_management", "api")
        modules = (bea, bls, ecb, eurostat, federal_reserve)
        for module in modules:
            source = Path(module.__file__).read_text(encoding="utf-8")
            self.assertFalse(any(f"import {name}" in source or f"from {name}" in source for name in forbidden))


class ReadOnlyPreflightTests(unittest.TestCase):
    def test_preflight_is_read_only(self):
        empty = lambda *_args, **_kwargs: {"source": "test", "request_count": 1, "normalized_events": [], "forecast_available": False}
        report = run_official_preflight(
            date_from="2026-06-08", date_to="2026-06-14", currencies="EUR,USD",
            fetchers={name: empty for name in ("bls", "bea", "federal_reserve", "eurostat", "ecb")},
        )
        self.assertTrue(report["read_only"])
        self.assertEqual(report["database_writes"], 0)
        self.assertEqual(report["trading_actions"], 0)


if __name__ == "__main__":
    unittest.main()
