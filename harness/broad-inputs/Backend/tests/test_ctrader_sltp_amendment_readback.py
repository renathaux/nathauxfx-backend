import unittest
from unittest.mock import MagicMock, patch

import ctrader_connector


class CTraderSltpAmendmentReadbackTests(unittest.TestCase):
    def _run_ambiguous_amend(self, positions):
        socket = MagicMock()
        with (
            patch.object(
                ctrader_connector,
                "get_ctrader_config",
                return_value={"account_id": 47810571, "env": "demo"},
            ),
            patch.object(ctrader_connector, "open_ctrader_json_socket", return_value=socket),
            patch.object(ctrader_connector, "authorize_ctrader_socket"),
            patch.object(
                ctrader_connector,
                "send_ctrader_request",
                side_effect=TimeoutError("execution event timeout"),
            ),
            patch.object(
                ctrader_connector,
                "fetch_ctrader_open_positions",
                return_value=positions,
            ),
        ):
            result = ctrader_connector.modify_position_sltp(
                58177198,
                stop_loss_price=4647.76,
                take_profit_price=4563.39,
            )
        return result

    def test_timeout_is_success_when_broker_readback_matches(self):
        result = self._run_ambiguous_amend([
            {
                "position_id": 58177198,
                "stop_loss": 4647.76,
                "take_profit": 4563.39,
            }
        ])

        self.assertTrue(result["ok"])
        self.assertTrue(result["confirmed_by_readback"])

    def test_timeout_remains_failure_when_broker_readback_does_not_match(self):
        result = self._run_ambiguous_amend([
            {
                "position_id": 58177198,
                "stop_loss": 4656.83,
                "take_profit": 4572.46,
            }
        ])

        self.assertFalse(result["ok"])
        self.assertFalse(result["confirmed_by_readback"])


if __name__ == "__main__":
    unittest.main()
