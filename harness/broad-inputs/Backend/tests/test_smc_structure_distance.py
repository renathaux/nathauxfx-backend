import unittest

from indicators.smc.structure_distance import StructureDistanceGate


class SmcStructureDistanceTests(unittest.TestCase):
    def test_15m_eurusd_tracks_distance_without_filtering_indicator(self):
        gate = StructureDistanceGate(timeframe="15m", point_size=0.00001)
        self.assertTrue(gate.accept(1.10000))
        self.assertTrue(gate.accept(1.10099))
        self.assertAlmostEqual(gate.last_distance_points, 99.0)
        self.assertEqual(gate.last_accepted_level, 1.10099)
        self.assertTrue(gate.accept(1.10075))
        self.assertEqual(gate.last_accepted_level, 1.10075)
        self.assertTrue(gate.accept(1.10100))
        self.assertEqual(gate.last_accepted_level, 1.10100)
        self.assertTrue(gate.accept(1.10201))
        self.assertEqual(gate.config()["minimum_structure_points"], 0)

    def test_15m_xauusd_uses_symbol_tick_size(self):
        gate = StructureDistanceGate(timeframe="15m", point_size=0.01)
        self.assertTrue(gate.accept(2500.00))
        self.assertTrue(gate.accept(2500.99))
        self.assertAlmostEqual(gate.last_distance_points, 99.0)
        self.assertTrue(gate.accept(2501.00))

    def test_other_timeframes_preserve_existing_acceptance(self):
        for timeframe in ("5m", "30m", "1h", "4h"):
            with self.subTest(timeframe=timeframe):
                gate = StructureDistanceGate(timeframe=timeframe, point_size=0.00001)
                self.assertTrue(gate.accept(1.20000))
                self.assertTrue(gate.accept(1.20001))

    def test_unspecified_context_preserves_strategy_engine_behavior(self):
        gate = StructureDistanceGate()
        self.assertTrue(gate.accept(2500.00))
        self.assertTrue(gate.accept(2500.01))
        self.assertEqual(gate.minimum_distance, 0.0)


if __name__ == "__main__":
    unittest.main()
