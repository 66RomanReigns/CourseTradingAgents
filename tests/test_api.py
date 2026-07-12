import unittest

from fastapi import HTTPException

from tradinglab_agents.api.app import RunRequest, _safe_path, backtest, experiments, health, root, runs


class ApiTest(unittest.TestCase):
    def test_root_and_health(self):
        self.assertEqual(root()["service"], "TradeLab-Agent")
        result = health()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["mode"], "paper-trading-only")
        self.assertEqual(result["version"], "0.3.0")

    def test_backtest_endpoint_function(self):
        result = backtest(RunRequest())
        self.assertEqual(result["symbol"], "DEMO")
        self.assertIn("sharpe", result["metrics"])
        self.assertGreater(result["decision_count"], 0)

    def test_experiment_endpoint_returns_audit(self):
        result = experiments(RunRequest(persist=False))
        self.assertTrue(result["audit"]["passed"])
        self.assertIn("without_regime_guard", {row["name"] for row in result["summary"]})

    def test_runs_endpoint_is_bounded(self):
        result = runs(limit=5)
        self.assertIn("runs", result)
        self.assertLessEqual(len(result["runs"]), 5)

    def test_path_escape_is_rejected(self):
        with self.assertRaises(HTTPException):
            _safe_path("../reference/TradingAgents/README.md")


if __name__ == "__main__":
    unittest.main()
