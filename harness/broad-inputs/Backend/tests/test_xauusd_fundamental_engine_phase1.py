import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi import HTTPException

from fundamentals.factors.real_yields import calculate_real_yield_factor
from fundamentals.gold_engine import calculate_xauusd_state, inflation_gold_score
from fundamentals.gold_insight_service import get_xauusd_fundamental_insight
from fundamentals.pair_bias import synthesize_pair_bias
from routes import fundamentals as fundamentals_route


NOW = datetime(2026, 8, 10, 14, 0, tzinfo=timezone.utc)


def observation(indicator, actual, previous, *, provider="fred", days=0, event_id=None):
    return {
        "event_id": event_id or f"{indicator}-{days}",
        "event_name": indicator.replace("_", " ").title(),
        "indicator": indicator,
        "currency": "USD",
        "country": "United States",
        "actual": str(actual),
        "forecast": None,
        "previous": str(previous),
        "revised_previous": None,
        "release_time": NOW - timedelta(days=days),
        "impact": "UNKNOWN",
        "provider": provider,
        "data_status": "RELEASED",
    }


def factor(score, *, status="ACTIVE", evidence_count=4, provisional=0):
    evidence = [{
        "event_id": f"evidence-{score}-{index}",
        "reason": f"Evidence produced factor score {score}.",
        "provider_quality": 1.0,
        "release_time": NOW,
    } for index in range(evidence_count)]
    return {
        "factor": "test",
        "score": score,
        "confidence": 85.0 if status == "ACTIVE" else 0.0,
        "status": status,
        "coverage": 1.0 if status == "ACTIVE" else 0.0,
        "evidence_count": evidence_count if status == "ACTIVE" else 0,
        "provisional_count": provisional,
        "evidence": evidence if status == "ACTIVE" else [],
        "updated_at": NOW if status == "ACTIVE" else None,
    }


def engine_patches(scores):
    return (
        patch("fundamentals.gold_engine.calculate_policy_factor", return_value=factor(scores.get("policy"))),
        patch("fundamentals.gold_engine.calculate_real_yield_factor", return_value=factor(scores.get("real_yields"))),
        patch("fundamentals.gold_engine.calculate_inflation_factor", return_value=factor(scores.get("inflation"))),
        patch("fundamentals.gold_engine.calculate_employment_factor", return_value=factor(scores.get("employment"))),
        patch("fundamentals.gold_engine.calculate_growth_factor", return_value=factor(scores.get("growth"))),
        patch("fundamentals.gold_engine.calculate_risk_sentiment_factor", return_value=factor(scores.get("risk_sentiment"))),
    )


class GoldFactorTests(unittest.TestCase):
    def yield_series(self, indicator, values, days=(7, 6, 5, 4, 3, 0)):
        return [
            observation(indicator, value, values[index - 1] if index else value, days=days[index], event_id=f"{indicator}-{index}")
            for index, value in enumerate(values)
        ]

    def test_rising_real_yields_are_bearish_gold(self):
        result = calculate_real_yield_factor(
            self.yield_series("us_10y_real_yield", [1.80, 1.82, 1.81, 1.83, 1.84, 2.10])
            + self.yield_series("us_10y_treasury_yield", [4.10, 4.11, 4.12, 4.10, 4.13, 4.30]),
            now=NOW,
        )
        self.assertEqual(result["status"], "ACTIVE")
        self.assertGreater(result["score"], 0)

    def test_falling_real_yields_are_bullish_gold(self):
        result = calculate_real_yield_factor(
            self.yield_series("us_10y_real_yield", [2.00, 2.01, 1.99, 2.02, 2.00, 1.70]),
            now=NOW,
        )
        self.assertLess(result["score"], 0)

    def test_nominal_yield_cannot_replace_real_yield(self):
        result = calculate_real_yield_factor(
            self.yield_series("us_10y_treasury_yield", [4.10, 4.11, 4.12, 4.10, 4.13, 4.30]),
            now=NOW,
        )
        self.assertEqual(result["status"], "INSUFFICIENT_DATA")

    def test_stale_real_yield_is_not_active(self):
        result = calculate_real_yield_factor(
            self.yield_series("us_10y_real_yield", [1.8, 1.82, 1.81, 1.83, 1.84, 2.1], days=(17, 16, 15, 14, 13, 10)),
            now=NOW,
        )
        self.assertNotEqual(result["status"], "ACTIVE")

    def test_inflation_is_interpreted_in_policy_context(self):
        self.assertLess(inflation_gold_score(60, 40), 0)
        self.assertGreater(inflation_gold_score(60, -40), 0)
        self.assertGreater(inflation_gold_score(-40, 0), 0)


class GoldSynthesisTests(unittest.TestCase):
    def _state(self, scores):
        managers = engine_patches(scores)
        with managers[0], managers[1], managers[2], managers[3], managers[4], managers[5]:
            return calculate_xauusd_state([], now=NOW)

    def test_strong_us_macro_and_rising_yields_create_sell(self):
        state = self._state({
            "policy": 70, "real_yields": 70, "inflation": 50,
            "employment": 60, "growth": 50, "risk_sentiment": -20,
        })
        self.assertEqual(state["direction"], "SELL")
        self.assertLessEqual(state["score"], -20)

    def test_hawkish_fed_policy_reduces_gold_score(self):
        state = self._state({
            "policy": 100, "real_yields": 0, "inflation": 0,
            "employment": 0, "growth": 0, "risk_sentiment": 0,
        })
        self.assertLess(state["drivers"]["policy"]["score"], 0)
        self.assertLess(state["score"], 0)

    def test_strong_employment_reduces_gold_score(self):
        state = self._state({
            "policy": 0, "real_yields": 0, "inflation": 0,
            "employment": 100, "growth": 0, "risk_sentiment": 0,
        })
        self.assertEqual(state["drivers"]["employment"]["score"], -100)
        self.assertLess(state["score"], 0)

    def test_strong_growth_reduces_gold_score(self):
        state = self._state({
            "policy": 0, "real_yields": 0, "inflation": 0,
            "employment": 0, "growth": 100, "risk_sentiment": 0,
        })
        self.assertEqual(state["drivers"]["growth"]["score"], -100)
        self.assertLess(state["score"], 0)

    def test_easing_falling_yields_and_stress_create_buy(self):
        state = self._state({
            "policy": -70, "real_yields": -70, "inflation": -40,
            "employment": -60, "growth": -50, "risk_sentiment": 60,
        })
        self.assertEqual(state["direction"], "BUY")
        self.assertGreaterEqual(state["score"], 20)

    def test_neutral_threshold_band(self):
        state = self._state({
            "policy": 5, "real_yields": -5, "inflation": 0,
            "employment": 4, "growth": -4, "risk_sentiment": 0,
        })
        self.assertEqual(state["direction"], "NEUTRAL")
        self.assertEqual(state["status"], "ACTIVE")

    def test_missing_real_yield_forces_insufficient_neutral_and_low_confidence(self):
        managers = engine_patches({
            "policy": 80, "real_yields": 0, "inflation": 70,
            "employment": 70, "growth": 70, "risk_sentiment": 70,
        })
        managers = list(managers)
        managers[1] = patch(
            "fundamentals.gold_engine.calculate_real_yield_factor",
            return_value=factor(None, status="INSUFFICIENT_DATA"),
        )
        with managers[0], managers[1], managers[2], managers[3], managers[4], managers[5]:
            state = calculate_xauusd_state([], now=NOW)
        self.assertEqual(state["direction"], "NEUTRAL")
        self.assertEqual(state["status"], "INSUFFICIENT_DATA")
        self.assertLessEqual(state["confidence"], 25)

    def test_coverage_and_confidence_degrade_with_missing_factors(self):
        complete = self._state({
            "policy": -10, "real_yields": -10, "inflation": -10,
            "employment": -10, "growth": -10, "risk_sentiment": 10,
        })
        managers = list(engine_patches({
            "policy": -10, "real_yields": -10, "inflation": -10,
            "employment": -10, "growth": 0, "risk_sentiment": 0,
        }))
        managers[4] = patch("fundamentals.gold_engine.calculate_growth_factor", return_value=factor(None, status="INSUFFICIENT_DATA"))
        managers[5] = patch("fundamentals.gold_engine.calculate_risk_sentiment_factor", return_value=factor(None, status="INSUFFICIENT_DATA"))
        with managers[0], managers[1], managers[2], managers[3], managers[4], managers[5]:
            partial = calculate_xauusd_state([], now=NOW)
        self.assertGreater(complete["coverage"], partial["coverage"])
        self.assertGreater(complete["confidence"], partial["confidence"])

    def test_top_reasons_are_ordered_by_weighted_contribution(self):
        state = self._state({
            "policy": 90, "real_yields": 20, "inflation": 10,
            "employment": 10, "growth": 10, "risk_sentiment": 10,
        })
        self.assertEqual(state["top_reasons"][0]["factor"], "policy")

    def test_xau_does_not_enter_eurusd_pair_subtraction(self):
        result = synthesize_pair_bias("XAUUSD", {"USD": {"score": 50, "coverage": 1}})
        self.assertEqual(result["status"], "UNSUPPORTED_SYMBOL")


class GoldInsightApiTests(unittest.TestCase):
    def test_read_only_response_contract(self):
        with patch("fundamentals.gold_insight_service.provider_health", return_value={"observation_count": 0, "providers": {}}):
            response = get_xauusd_fundamental_insight(
                now=NOW, observations=[], next_event={}
            )
        self.assertEqual(response["symbol"], "XAUUSD")
        self.assertEqual(response["overall_bias"]["direction"], "NEUTRAL")
        self.assertEqual(response["overall_bias"]["status"], "INSUFFICIENT_DATA")
        self.assertFalse(response["trading_guidance"]["execution_connected"])
        self.assertTrue(response["read_only"])

    def test_route_supports_xauusd_and_preserves_eurusd_path(self):
        xau = {"symbol": "XAUUSD", "read_only": True}
        eur = {"symbol": "EURUSD", "read_only": True}
        with patch.object(fundamentals_route, "get_xauusd_fundamental_insight", return_value=xau), patch.object(
            fundamentals_route, "get_fundamental_insight", return_value=eur
        ):
            self.assertEqual(fundamentals_route.fundamental_insight("XAUUSD"), xau)
            self.assertEqual(fundamentals_route.fundamental_insight("EURUSD"), eur)
        with self.assertRaises(HTTPException):
            fundamentals_route.fundamental_insight("GBPUSD")


if __name__ == "__main__":
    unittest.main()
