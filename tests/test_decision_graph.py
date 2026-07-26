import tempfile
import unittest
from collections import Counter
from dataclasses import replace
from pathlib import Path

from tradinglab_agents.agents.langgraph_research import inspect_research_thread
from tradinglab_agents.config import load_settings
from tradinglab_agents.data.csv_provider import LocalCsvProvider
from tradinglab_agents.engine.decision_graph import (
    LangGraphDeterministicDecisionRuntime,
    portfolio_plan_to_dict,
)
from tradinglab_agents.engine.market import AlignedMarketData
from tradinglab_agents.engine.portfolio_planner import PortfolioPlanner
from tradinglab_agents.models import Portfolio
from tradinglab_agents.paper.models import ApprovalPolicy
from tradinglab_agents.paper.service import PaperTradingService
from tradinglab_agents.storage.paper_store import PaperTradingStore


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data/multi_sample"
SETTINGS = load_settings(ROOT / "config/default.yaml")
SYMBOLS = ("SPY", "QQQ", "AAPL")


def _market(symbols=SYMBOLS) -> AlignedMarketData:
    return AlignedMarketData(
        {
            symbol: LocalCsvProvider(DATA_DIR / f"{symbol}.csv", symbol)
            for symbol in symbols
        }
    )


def _context(
    planner: PortfolioPlanner,
    *,
    portfolio: Portfolio | None = None,
    overlays=None,
):
    market = _market()
    timestamp = market.timestamps[max(20, planner.settings.warmup_bars)]
    portfolio = portfolio or Portfolio(cash=100_000.0, peak_equity=100_000.0)
    context = planner.prepare_context(
        market,
        timestamp,
        portfolio,
        research_overlays=overlays,
    )
    return market, timestamp, portfolio, context


class DeterministicDecisionGraphTest(unittest.TestCase):
    def test_graph_matches_sequential_planner_and_does_not_mutate_input(self):
        planner = PortfolioPlanner(SETTINGS)
        market, timestamp, portfolio, context = _context(planner)
        before = (
            portfolio.cash,
            dict(portfolio.positions),
            portfolio.peak_equity,
        )
        legacy = planner.plan(
            market,
            timestamp,
            Portfolio(
                cash=portfolio.cash,
                positions=dict(portfolio.positions),
                peak_equity=portfolio.peak_equity,
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = LangGraphDeterministicDecisionRuntime(
                planner,
                event_path=root / "events.jsonl",
            )
            execution = runtime.run(
                thread_id="decision-parity",
                context=context,
                audit_runner=lambda _name, function: function(),
                checkpointer_path=root / "decision.db",
            )
            self.assertEqual(
                portfolio_plan_to_dict(execution.plan),
                portfolio_plan_to_dict(legacy),
            )
            self.assertEqual(
                before,
                (
                    portfolio.cash,
                    dict(portfolio.positions),
                    portfolio.peak_equity,
                ),
            )
            self.assertEqual(len(execution.plan_sha256), 64)
            self.assertEqual(len(execution.plan.graph_runtime["input_sha256"]), 64)
            self.assertFalse(
                execution.plan.graph_runtime["account_mutation_in_graph"]
            )
            self.assertFalse(
                execution.plan.graph_runtime["order_persistence_in_graph"]
            )
            for node in (
                "decision.SPY.evidence_pack",
                "decision.SPY.quant_signal",
                "decision.SPY.fusion_critic",
                "decision.SPY.regime_guard",
                "decision.SPY.target_overlay",
                "decision.portfolio_risk",
                "decision.finalize",
            ):
                self.assertIn(node, execution.execution_path)
            status = inspect_research_thread(root / "decision.db", "decision-parity")
            self.assertEqual(status["graph_type"], "deterministic_decision")
            self.assertEqual(len(status["decision_input_sha256"]), 64)
            self.assertEqual(status["decision_plan_sha256"], execution.plan_sha256)
            self.assertEqual(status["decision_symbol_count"], 3)
            self.assertEqual(
                sorted(status["decision_target_weights"]),
                ["AAPL", "QQQ", "SPY"],
            )

    def test_resume_retries_only_failed_nested_node(self):
        planner = PortfolioPlanner(SETTINGS)
        _, _, _, context = _context(planner)
        counts: Counter[str] = Counter()
        fail_once = {"enabled": True}

        def audit(name, function):
            counts[name] += 1
            if name == "decision.QQQ.fusion_critic" and fail_once["enabled"]:
                fail_once["enabled"] = False
                raise RuntimeError("injected QQQ fusion failure")
            return function()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = LangGraphDeterministicDecisionRuntime(
                planner,
                event_path=root / "events.jsonl",
            )
            arguments = dict(
                thread_id="decision-resume",
                context=context,
                audit_runner=audit,
                checkpointer_path=root / "decision.db",
            )
            with self.assertRaisesRegex(RuntimeError, "injected QQQ fusion failure"):
                runtime.run(**arguments)
            resumed = runtime.run(**arguments, resume=True)
            self.assertEqual(counts["decision.SPY.evidence_pack"], 1)
            self.assertEqual(counts["decision.AAPL.evidence_pack"], 1)
            self.assertEqual(counts["decision.QQQ.evidence_pack"], 1)
            self.assertEqual(counts["decision.QQQ.quant_signal"], 1)
            self.assertEqual(counts["decision.QQQ.regime_guard"], 1)
            self.assertEqual(counts["decision.QQQ.fusion_critic"], 2)
            self.assertEqual(counts["decision.portfolio_risk"], 1)
            self.assertEqual(counts["decision.finalize"], 1)
            self.assertEqual(len(resumed.plan.target_weights), 3)

    def test_resume_rejects_changed_portfolio_or_overlay_input(self):
        planner = PortfolioPlanner(SETTINGS)
        _, _, _, context = _context(planner)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = LangGraphDeterministicDecisionRuntime(planner)
            runtime.run(
                thread_id="decision-hash",
                context=context,
                audit_runner=lambda _name, function: function(),
                checkpointer_path=root / "decision.db",
            )
            _, _, _, changed = _context(
                planner,
                portfolio=Portfolio(cash=99_000.0, peak_equity=100_000.0),
                overlays={
                    "SPY": {
                        "action": "SELL",
                        "confidence": 0.9,
                        "target_weight": 0.0,
                    }
                },
            )
            with self.assertRaisesRegex(ValueError, "input hash mismatch"):
                runtime.run(
                    thread_id="decision-hash",
                    context=changed,
                    audit_runner=lambda _name, function: function(),
                    checkpointer_path=root / "decision.db",
                    resume=True,
                )

    def test_mermaid_exposes_nested_deterministic_stages(self):
        runtime = LangGraphDeterministicDecisionRuntime(PortfolioPlanner(SETTINGS))
        mermaid = runtime.mermaid()
        for node in (
            "evidence_pack",
            "quant_signal",
            "fusion_critic",
            "regime_guard",
            "target_overlay",
            "portfolio_risk",
            "finalize",
        ):
            self.assertIn(node, mermaid)


class DecisionGraphPaperIntegrationTest(unittest.TestCase):
    def test_paper_session_persists_decision_graph_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = replace(
                SETTINGS,
                workflow_decision_graph_enabled=True,
                workflow_decision_checkpoint_database=(
                    "artifacts/langgraph/decision_checkpoints.db"
                ),
                workflow_langgraph_event_path="artifacts/langgraph_events.jsonl",
            )
            store = PaperTradingStore(root / "paper.db")
            service = PaperTradingService(store, settings)
            service.initialize_account(
                "decision-demo",
                name="Decision Graph Demo",
                symbols=("SPY", "QQQ", "AAPL", "MSFT", "NVDA"),
                approval_policy=ApprovalPolicy.ALL,
            )
            result = service.run_next_session(
                "decision-demo",
                data_dir=DATA_DIR,
            )
            runtime = result["payload"]["plan"]["graph_runtime"]
            self.assertEqual(runtime["graph_type"], "deterministic_decision")
            self.assertTrue(runtime["thread_id"].endswith(":DECISION"))
            self.assertEqual(len(runtime["input_sha256"]), 64)
            self.assertEqual(len(runtime["plan_sha256"]), 64)
            self.assertFalse(runtime["account_mutation_in_graph"])
            self.assertFalse(runtime["order_persistence_in_graph"])
            database = root / "langgraph/decision_checkpoints.db"
            self.assertTrue(database.is_file())
            status = inspect_research_thread(database, runtime["thread_id"])
            self.assertEqual(status["graph_type"], "deterministic_decision")
            memories = store.list_decision_memories("decision-demo")
            self.assertGreater(len(memories), 0)
            self.assertEqual(
                memories[0]["rationale"]["decision_graph"]["plan_sha256"],
                runtime["plan_sha256"],
            )


if __name__ == "__main__":
    unittest.main()
