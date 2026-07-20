import unittest
from datetime import datetime
from pathlib import Path

from tradinglab_agents.broker.paper import PaperBroker
from tradinglab_agents.config import BacktestSettings
from tradinglab_agents.data.csv_provider import LocalCsvProvider
from tradinglab_agents.engine.multi_backtest import MultiAssetBacktestEngine
from tradinglab_agents.models import MarketSnapshot, Portfolio
from tradinglab_agents.risk.governor import RiskState
from tradinglab_agents.risk.portfolio import PortfolioRiskGovernor


ROOT = Path(__file__).resolve().parents[1]


class StrictPortfolioValuationTest(unittest.TestCase):
    def test_missing_price_for_held_asset_is_rejected(self):
        portfolio = Portfolio(cash=100.0, positions={"AAA": 2, "BBB": 1})
        with self.assertRaisesRegex(ValueError, "BBB"):
            portfolio.equity({"AAA": 10.0})

    def test_snapshot_rejects_invalid_price(self):
        with self.assertRaises(ValueError):
            MarketSnapshot(datetime(2025, 1, 1), {"AAA": 0.0})

    def test_snapshot_prices_are_immutable(self):
        snapshot = MarketSnapshot(datetime(2025, 1, 1), {"AAA": 10.0})
        with self.assertRaises(TypeError):
            snapshot.prices["AAA"] = 11.0


class MultiAssetBrokerTest(unittest.TestCase):
    def test_rebalance_many_sells_before_buys(self):
        portfolio = Portfolio(cash=0.0, positions={"AAA": 100})
        snapshot = MarketSnapshot(
            datetime(2025, 1, 2, 9, 30),
            {"AAA": 10.0, "BBB": 20.0},
            field="open",
        )
        fills = PaperBroker(commission_bps=5.0, slippage_bps=5.0).rebalance_many(
            portfolio,
            {"AAA": 0.0, "BBB": 0.50},
            snapshot,
        )
        self.assertGreaterEqual(len(fills), 2)
        self.assertEqual(fills[0].symbol, "AAA")
        self.assertLess(fills[0].quantity, 0)
        self.assertEqual(fills[1].symbol, "BBB")
        self.assertGreater(fills[1].quantity, 0)
        self.assertGreaterEqual(portfolio.cash, 0.0)
        self.assertEqual(portfolio.positions["AAA"], 0)
        self.assertGreater(portfolio.positions["BBB"], 0)

    def test_single_symbol_rebalance_requires_other_holding_prices(self):
        portfolio = Portfolio(cash=100.0, positions={"AAA": 2, "BBB": 1})
        with self.assertRaisesRegex(ValueError, "BBB"):
            PaperBroker().rebalance(
                portfolio,
                "AAA",
                0.20,
                10.0,
                datetime(2025, 1, 2, 9, 30),
            )


class PortfolioRiskGovernorTest(unittest.TestCase):
    def test_position_count_position_cap_and_gross_cap(self):
        governor = PortfolioRiskGovernor(
            max_position_weight=0.30,
            max_gross_exposure=0.60,
            max_positions=2,
            max_drawdown=0.15,
        )
        portfolio = Portfolio(cash=100_000.0, peak_equity=100_000.0)
        snapshot = MarketSnapshot(
            datetime(2025, 1, 1, 16),
            {"AAA": 10.0, "BBB": 10.0, "CCC": 10.0},
        )
        decision = governor.review(
            {"AAA": 0.40, "BBB": 0.40, "CCC": 0.40},
            portfolio,
            snapshot,
            confidences={"AAA": 0.9, "BBB": 0.8, "CCC": 0.7},
        )
        self.assertTrue(decision.approved)
        self.assertAlmostEqual(sum(decision.target_weights.values()), 0.60)
        self.assertEqual(decision.target_weights["AAA"], 0.30)
        self.assertEqual(decision.target_weights["BBB"], 0.30)
        self.assertEqual(decision.target_weights["CCC"], 0.0)

    def test_drawdown_forces_full_portfolio_liquidation(self):
        governor = PortfolioRiskGovernor(
            max_position_weight=0.50,
            max_gross_exposure=0.90,
            max_positions=2,
            max_drawdown=0.15,
        )
        portfolio = Portfolio(
            cash=0.0,
            positions={"AAA": 100},
            peak_equity=1_000.0,
        )
        snapshot = MarketSnapshot(
            datetime(2025, 1, 1, 16),
            {"AAA": 5.0, "BBB": 10.0},
        )
        decision = governor.review(
            {"AAA": 0.50, "BBB": 0.40},
            portfolio,
            snapshot,
        )
        self.assertFalse(decision.approved)
        self.assertTrue(decision.force_execution)
        self.assertEqual(decision.state, RiskState.LIQUIDATING.value)
        self.assertTrue(all(weight == 0.0 for weight in decision.target_weights.values()))

        PaperBroker().rebalance_many(portfolio, decision.target_weights, snapshot)
        halted = governor.review(
            {"AAA": 0.50, "BBB": 0.40},
            portfolio,
            snapshot,
        )
        self.assertEqual(halted.state, RiskState.HALTED.value)
        self.assertFalse(halted.force_execution)


class MultiAssetBacktestTest(unittest.TestCase):
    def test_synchronized_portfolio_backtest(self):
        price_path = ROOT / "data/sample/demo.csv"
        providers = {
            symbol: LocalCsvProvider(price_path, symbol)
            for symbol in ("AAA", "BBB", "CCC")
        }
        settings = BacktestSettings(
            warmup_bars=60,
            enable_context=False,
            max_position_weight=0.20,
            max_gross_exposure=0.60,
            max_positions=3,
        )
        result = MultiAssetBacktestEngine(settings).run(providers)
        self.assertEqual(result["symbols"], ["AAA", "BBB", "CCC"])
        self.assertEqual(result["data_alignment"]["common_sessions"], 260)
        self.assertTrue(result["decisions"])
        self.assertTrue(result["portfolio_history"])
        self.assertTrue(
            all(
                row["execution_time"] > row["decision_time"]
                for row in result["decisions"]
            )
        )
        self.assertTrue(
            all(row["cash"] >= -1e-7 for row in result["portfolio_history"])
        )
        self.assertLessEqual(
            max(
                row["execution_gross_exposure"]
                for row in result["portfolio_history"]
            ),
            settings.max_gross_exposure + 0.002,
        )
        self.assertTrue(
            all(
                max(row["execution_weights"].values())
                <= settings.max_position_weight + 0.002
                for row in result["portfolio_history"]
            )
        )

    def test_risk_ablation_preserves_broker_cash_invariant(self):
        price_path = ROOT / "data/sample/demo.csv"
        providers = {
            symbol: LocalCsvProvider(price_path, symbol)
            for symbol in ("AAA", "BBB", "CCC")
        }
        settings = BacktestSettings(
            warmup_bars=60,
            enable_context=False,
            enable_risk=False,
            max_position_weight=0.45,
            max_gross_exposure=0.90,
            max_positions=3,
        )
        result = MultiAssetBacktestEngine(settings).run(providers)
        self.assertTrue(
            all(row["cash"] >= -1e-7 for row in result["portfolio_history"])
        )
        self.assertLessEqual(
            max(
                row["execution_gross_exposure"]
                for row in result["portfolio_history"]
            ),
            1.002,
        )


if __name__ == "__main__":
    unittest.main()
