import tempfile
import unittest
from collections import Counter
from dataclasses import replace
from pathlib import Path

from tradinglab_agents.agents.llm import MockLLM
from tradinglab_agents.agents.portfolio_supervisor import (
    LangGraphPortfolioSupervisorRuntime,
    build_portfolio_market_context,
    guard_portfolio_allocation,
)
from tradinglab_agents.agents.schemas import (
    PortfolioAllocationItem,
    PortfolioCommitteeReview,
    PortfolioSupervisorDecision,
    PortfolioSymbolCap,
)
from tradinglab_agents.config import load_settings
from tradinglab_agents.data.csv_provider import LocalCsvProvider
from tradinglab_agents.workflows.daily import DailyWorkflow


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data" / "multi_sample"


class PortfolioAllocationGuardTest(unittest.TestCase):
    @staticmethod
    def _review(
        reviewer: str,
        caps: dict[str, float],
        gross: float = 0.90,
    ) -> PortfolioCommitteeReview:
        return PortfolioCommitteeReview(
            reviewer=reviewer,
            confidence=0.8,
            max_gross_target=gross,
            symbol_caps=tuple(
                PortfolioSymbolCap(
                    symbol=symbol,
                    max_target_weight=target,
                    rationale="test cap",
                )
                for symbol, target in caps.items()
            ),
            rationale="test review",
        )

    def test_guard_never_increases_and_scales_high_correlation_cluster(self):
        plans = [
            {
                "symbol": "SPY",
                "action": "BUY",
                "target_weight": 0.18,
                "confidence": 0.8,
            },
            {
                "symbol": "QQQ",
                "action": "BUY",
                "target_weight": 0.17,
                "confidence": 0.75,
            },
        ]
        reviews = (
            self._review("correlation", {"SPY": 0.18, "QQQ": 0.17}),
            self._review("concentration", {"SPY": 0.18, "QQQ": 0.17}),
        )
        proposal = PortfolioSupervisorDecision(
            allocations=(
                PortfolioAllocationItem(
                    symbol="SPY",
                    action="BUY",
                    target_weight=0.20,
                    confidence=0.9,
                    rationale="attempted increase",
                ),
                PortfolioAllocationItem(
                    symbol="QQQ",
                    action="BUY",
                    target_weight=0.20,
                    confidence=0.9,
                    rationale="attempted increase",
                ),
            ),
            gross_target=0.40,
            confidence=0.9,
            rationale="test proposal",
        )
        result = guard_portfolio_allocation(
            plans,
            reviews,
            proposal,
            {
                "correlations": {"QQQ|SPY": 0.95},
            },
            max_gross_target=0.90,
            max_positions=5,
            high_correlation_threshold=0.80,
            cluster_gross_cap=0.25,
        )
        allocations = {item.symbol: item for item in result.allocations}
        self.assertAlmostEqual(result.gross_target, 0.25)
        self.assertLessEqual(allocations["SPY"].target_weight, 0.18)
        self.assertLessEqual(allocations["QQQ"].target_weight, 0.17)
        self.assertEqual(result.high_correlation_clusters, (("QQQ", "SPY"),))
        self.assertTrue(
            any("high-correlation cluster" in note for note in result.constraint_notes)
        )

    def test_guard_restores_sell_and_blocks_hold_upgrade(self):
        plans = [
            {
                "symbol": "AAPL",
                "action": "SELL",
                "target_weight": 0.0,
                "confidence": 0.9,
            },
            {
                "symbol": "MSFT",
                "action": "HOLD",
                "target_weight": 0.10,
                "confidence": 0.7,
            },
        ]
        reviews = (
            self._review("correlation", {"AAPL": 0.0, "MSFT": 0.10}),
            self._review("concentration", {"AAPL": 0.0, "MSFT": 0.10}),
        )
        proposal = PortfolioSupervisorDecision(
            allocations=(
                PortfolioAllocationItem(
                    symbol="AAPL",
                    action="HOLD",
                    target_weight=0.0,
                    confidence=0.8,
                    rationale="incorrectly weakened sell",
                ),
                PortfolioAllocationItem(
                    symbol="MSFT",
                    action="BUY",
                    target_weight=0.15,
                    confidence=0.8,
                    rationale="incorrectly upgraded hold",
                ),
            ),
            gross_target=0.15,
            confidence=0.8,
            rationale="unsafe test proposal",
        )
        result = guard_portfolio_allocation(
            plans,
            reviews,
            proposal,
            {"correlations": {}},
            max_gross_target=0.90,
            max_positions=5,
            high_correlation_threshold=0.80,
            cluster_gross_cap=0.25,
        )
        allocations = {item.symbol: item for item in result.allocations}
        self.assertEqual(allocations["AAPL"].action, "SELL")
        self.assertEqual(allocations["AAPL"].target_weight, 0.0)
        self.assertEqual(allocations["MSFT"].action, "HOLD")
        self.assertLessEqual(allocations["MSFT"].target_weight, 0.10)


class PortfolioSupervisorGraphTest(unittest.TestCase):
    @staticmethod
    def _context() -> dict:
        providers = {
            "SPY": LocalCsvProvider(DATA_DIR / "SPY.csv", "SPY"),
            "QQQ": LocalCsvProvider(DATA_DIR / "QQQ.csv", "QQQ"),
        }
        return build_portfolio_market_context(
            providers,
            window_sessions=60,
            high_correlation_threshold=0.80,
        )

    @staticmethod
    def _plans() -> list[dict]:
        return [
            {
                "symbol": "SPY",
                "action": "BUY",
                "target_weight": 0.18,
                "confidence": 0.8,
            },
            {
                "symbol": "QQQ",
                "action": "BUY",
                "target_weight": 0.17,
                "confidence": 0.75,
            },
        ]

    def test_graph_runs_parallel_committee_and_guarded_supervisor(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = LangGraphPortfolioSupervisorRuntime(
                MockLLM(),
                MockLLM(),
                trace_path=root / "langchain.jsonl",
                event_path=root / "graph.jsonl",
                cluster_gross_cap=0.25,
            )
            execution = runtime.run(
                self._plans(),
                self._context(),
                thread_id="portfolio-graph",
                node_runner=lambda _name, _model, function: function(),
                checkpointer_path=root / "checkpoint.db",
            )
            self.assertEqual(len(execution.reviews), 2)
            self.assertEqual(execution.result.gross_target, 0.25)
            self.assertEqual(execution.checkpoint_count, 5)
            self.assertEqual(execution.event_count, 4)
            self.assertEqual(
                set(execution.execution_path[:2]),
                {
                    "portfolio.correlation_reviewer",
                    "portfolio.concentration_reviewer",
                },
            )
            self.assertEqual(execution.execution_path[-1], "portfolio.deterministic_guard")
            self.assertIn("portfolio_supervisor", runtime.mermaid())

    def test_resume_reruns_only_failed_parallel_reviewer(self):
        counts: Counter[str] = Counter()
        fail_once = {"correlation": True}

        def runner(name, model, function):
            del model
            counts[name] += 1
            if name == "portfolio.correlation_reviewer" and fail_once["correlation"]:
                fail_once["correlation"] = False
                raise RuntimeError("injected portfolio correlation failure")
            return function()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = LangGraphPortfolioSupervisorRuntime(
                MockLLM(),
                MockLLM(),
                trace_path=root / "langchain.jsonl",
                event_path=root / "graph.jsonl",
                retry_attempts=1,
            )
            database = root / "checkpoint.db"
            with self.assertRaisesRegex(RuntimeError, "injected portfolio correlation failure"):
                runtime.run(
                    self._plans(),
                    self._context(),
                    thread_id="portfolio-resume",
                    node_runner=runner,
                    checkpointer_path=database,
                )
            execution = runtime.run(
                self._plans(),
                self._context(),
                thread_id="portfolio-resume",
                node_runner=runner,
                checkpointer_path=database,
                resume=True,
            )
            self.assertEqual(counts["portfolio.correlation_reviewer"], 2)
            self.assertEqual(counts["portfolio.concentration_reviewer"], 1)
            self.assertIsNotNone(execution.result)


class PortfolioSupervisorWorkflowTest(unittest.TestCase):
    def test_daily_workflow_uses_29_calls_and_persists_adjustments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = replace(
                load_settings(ROOT / "config" / "default.yaml"),
                workflow_artifact_dir=str(root / "workflows"),
                workflow_langgraph_checkpoint_database=str(root / "graphs.db"),
                workflow_langchain_trace_path=str(root / "langchain.jsonl"),
                workflow_langgraph_event_path=str(root / "langgraph.jsonl"),
            )
            result = DailyWorkflow(settings, ROOT).execute(mode="dry_run")
            research = result["research"]["result"]
            self.assertEqual(research["planned_calls"], 29)
            self.assertEqual(research["symbol_research_calls"], 26)
            self.assertEqual(research["portfolio_supervisor_calls"], 3)
            supervisor = research["portfolio_supervisor"]
            self.assertEqual(supervisor["status"], "completed")
            self.assertTrue(supervisor["cannot_increase_symbol_targets"])
            self.assertTrue(supervisor["hard_risk_still_required"])
            self.assertEqual(supervisor["thread_id"].split(":")[-1], "PORTFOLIO")
            for row in research["candidates"].values():
                original = row["result"]["trader"]
                adjusted = row["portfolio_adjustment"]
                self.assertLessEqual(
                    adjusted["target_weight"],
                    original["target_weight"] + 1e-12,
                )
            self.assertFalse(result["paper"]["mutated"])
            self.assertFalse(result["plan"]["external_requests_enabled"])


if __name__ == "__main__":
    unittest.main()
