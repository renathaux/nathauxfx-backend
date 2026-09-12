import unittest

from routes.shadow import router as shadow_router
from services.v2_shadow_service import evaluate_cycle_safely, link_v1_execution_safely


class StrategyV2RemovalTests(unittest.TestCase):
    def test_removed_v2_evaluator_is_noop(self):
        result = evaluate_cycle_safely("EURUSD", {"signal": "BUY"})
        self.assertTrue(result["ok"])
        self.assertTrue(result["removed"])
        self.assertEqual(result["reason"], "STRATEGY_V2_REMOVED")

    def test_removed_v2_execution_link_is_noop(self):
        self.assertFalse(
            link_v1_execution_safely(
                "EURUSD",
                "setup",
                {"ok": True, "order_id": "should-not-be-stored"},
            )
        )

    def test_shadow_v2_api_routes_are_gone(self):
        self.assertEqual(list(shadow_router.routes), [])


if __name__ == "__main__":
    unittest.main()
