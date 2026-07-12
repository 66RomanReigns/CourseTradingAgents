import unittest
from pathlib import Path

from tradinglab_agents.data.csv_provider import LocalCsvProvider
from tradinglab_agents.engine.backtest import BacktestEngine


class BacktestTest(unittest.TestCase):
    def test_backtest_executes_on_next_bar(self):
        root = Path(__file__).resolve().parents[1]
        provider = LocalCsvProvider(root / "data" / "sample" / "demo.csv", "DEMO")
        result = BacktestEngine().run(provider)
        self.assertTrue(result["decisions"])
        self.assertTrue(all(d["execution_time"] > d["decision_time"] for d in result["decisions"]))
        self.assertGreater(result["final_equity"], 0)


if __name__ == "__main__":
    unittest.main()
