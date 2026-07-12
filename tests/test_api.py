import unittest

from fastapi import HTTPException

from tradinglab_agents.api.app import RunRequest, _safe_path, backtest, health


class ApiTest(unittest.TestCase):
    def test_health(self):
        result = health()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["mode"], "paper-trading-only")

    def test_backtest_endpoint_function(self):
        result = backtest(RunRequest())
        self.assertEqual(result["symbol"], "DEMO")
        self.assertIn("sharpe", result["metrics"])
        self.assertGreater(result["decision_count"], 0)

    def test_path_escape_is_rejected(self):
        with self.assertRaises(HTTPException):
            _safe_path("../reference/TradingAgents/README.md")


if __name__ == "__main__":
    unittest.main()
