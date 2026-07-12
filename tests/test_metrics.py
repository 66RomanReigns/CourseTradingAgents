import unittest

from tradinglab_agents.evaluation.metrics import compute_metrics, max_drawdown


class MetricsTest(unittest.TestCase):
    def test_max_drawdown(self):
        self.assertAlmostEqual(max_drawdown([100.0, 120.0, 90.0, 110.0]), 0.25)

    def test_metrics_include_cost_and_turnover(self):
        curve = [
            {"timestamp": "2025-01-02", "equity": 101.0},
            {"timestamp": "2025-01-03", "equity": 99.0},
            {"timestamp": "2025-01-04", "equity": 103.0},
        ]
        fills = [{"quantity": 2, "price": 10.0, "fee": 1.0}]
        metrics = compute_metrics(curve, 100.0, fills)
        self.assertAlmostEqual(metrics["total_return"], 0.03)
        self.assertEqual(metrics["trade_count"], 1)
        self.assertEqual(metrics["fees"], 1.0)
        self.assertGreater(metrics["turnover"], 0.0)


if __name__ == "__main__":
    unittest.main()
