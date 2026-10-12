import inspect
import unittest

from routes import diagnostics
from services import forex_observability_service
from services import strategy_diagnostics_service
from services.live_v3b_service import LIVE_V3B_MODEL


class ProductionV1RemovalTests(unittest.TestCase):
    def test_v1_diagnostics_are_noop(self):
        result = strategy_diagnostics_service.persist_cycle_safely(
            "EURUSD", {"signal": "BUY"}
        )
        self.assertTrue(result["disabled"])
        self.assertEqual(result["reason"], "PRODUCTION_V1_RETIRED")
        self.assertFalse(strategy_diagnostics_service.record_execution_gate_safely())
        self.assertFalse(strategy_diagnostics_service.update_execution_outcome_safely())
        self.assertEqual(strategy_diagnostics_service.query_cycles(), [])

    def test_v1_diagnostics_route_is_empty(self):
        self.assertEqual(diagnostics.router.routes, [])

    def test_lifecycle_observer_is_retired_without_removing_v3b_audit(self):
        result = forex_observability_service.persist_lifecycle_evaluation_safely({})
        self.assertTrue(result["disabled"])
        source = inspect.getsource(forex_observability_service)
        self.assertNotIn("ForexLifecycleEvaluation", source)
        self.assertIn("ForexExecutionSnapshot", source)

    def test_v3b_remains_the_production_model(self):
        self.assertEqual(LIVE_V3B_MODEL, "LIVE_V3B_M5_FROZEN")


if __name__ == "__main__":
    unittest.main()
