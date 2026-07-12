import unittest
from pathlib import Path

from tradinglab_agents.config import BacktestSettings
from tradinglab_agents.data.csv_provider import LocalCsvProvider
from tradinglab_agents.data.news_provider import LocalNewsProvider
from tradinglab_agents.engine.backtest import BacktestEngine
from tradinglab_agents.evaluation.experiments import run_experiment_suite


class BacktestTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[1]
        cls.provider = LocalCsvProvider(cls.root / "data" / "sample" / "demo.csv", "DEMO")
        cls.news = LocalNewsProvider(cls.root / "data" / "sample" / "demo_news.jsonl")

    def test_backtest_executes_on_next_open(self):
        result = BacktestEngine(BacktestSettings(warmup_bars=60)).run(self.provider, self.news)
        self.assertTrue(result["decisions"])
        self.assertTrue(
            all(d["execution_time"] > d["decision_time"] for d in result["decisions"])
        )
        expected_first_open = self.provider.bars[61].open_at.isoformat()
        self.assertEqual(result["decisions"][0]["execution_time"], expected_first_open)
        self.assertGreater(result["final_equity"], 0)

    def test_risk_governor_caps_target_weight(self):
        settings = BacktestSettings(warmup_bars=60, max_position_weight=0.20)
        result = BacktestEngine(settings).run(self.provider, self.news)
        self.assertTrue(
            all(decision["target_weight"] <= 0.2000001 for decision in result["decisions"])
        )

    def test_experiment_suite_contains_baselines_and_ablations(self):
        result = run_experiment_suite(
            self.provider,
            BacktestSettings(warmup_bars=60),
            self.news,
        )
        names = {row["name"] for row in result["summary"]}
        self.assertEqual(
            names,
            {
                "full_agent",
                "quant_plus_critic",
                "quant_only",
                "without_risk_governor",
                "without_regime_guard",
                "sma_cross",
                "buy_and_hold",
            },
        )
        self.assertTrue(all(row["trade_count"] >= 0 for row in result["summary"]))


if __name__ == "__main__":
    unittest.main()
