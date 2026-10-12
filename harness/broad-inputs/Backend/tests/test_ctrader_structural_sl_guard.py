import unittest
from unittest.mock import patch

import ctrader_connector


class CTraderStructuralSlGuardTests(unittest.TestCase):
    def test_broker_minimum_rejects_instead_of_rewriting_buy_sl(self):
        with patch.dict(ctrader_connector.TRADE_LEVEL_RULES, {
            "EURUSD": {"precision": 5, "min_distance": 0.001, "pip_size": 0.0001}
        }):
            result = ctrader_connector.normalize_trade_levels(
                "EURUSD", "BUY", 1.16600, 1.16550, 1.16800, 1.17000
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["sl"], 1.16550)
        self.assertIn("below the broker minimum", result["reason"])

    def test_broker_minimum_rejects_instead_of_rewriting_sell_sl(self):
        with patch.dict(ctrader_connector.TRADE_LEVEL_RULES, {
            "EURUSD": {"precision": 5, "min_distance": 0.001, "pip_size": 0.0001}
        }):
            result = ctrader_connector.normalize_trade_levels(
                "EURUSD", "SELL", 1.16600, 1.16650, 1.16400, 1.16200
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["sl"], 1.16650)


if __name__ == "__main__":
    unittest.main()
