import tempfile
import unittest
from collections import Counter
from dataclasses import replace
from pathlib import Path

from tradinglab_agents.config import load_settings
from tradinglab_agents.workflows.daily import DailyWorkflow
from tradinglab_agents.workflows.research_parent import (
    LangGraphResearchParentRuntime,
)


ROOT = Path(__file__).resolve().parents[1]
SETTINGS = load_settings(ROOT / "config" / "default.yaml")


class ResearchParentGraphTest(unittest.TestCase):
    def test_dynamic_send_fan_out_and_portfolio_fan_in(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = LangGraphResearchParentRuntime(
                event_path=root / "events.jsonl"
            )
            execution = runtime.run(
                thread_id="parent-dynamic",
                candidate_selector=lambda: [
                    {"symbol": "SPY", "priority": 0.9},
                    {"symbol": "QQQ", "priority": 0.8},
                    {"symbol": "AAPL", "priority": 0.7},
                ],
                symbol_runner=lambda candidate: {
                    "symbol": candidate["symbol"],
                    "priority": candidate["priority"],
                },
                portfolio_runner=lambda rows: {
                    "status": "completed",
                    "symbols": [row["symbol"] for row in rows],
                },
                audit_runner=lambda _name, function: function(),
                checkpointer_path=root / "parent.db",
            )
            self.assertEqual(
                [row["symbol"] for row in execution.candidate_results],
                ["AAPL", "QQQ", "SPY"],
            )
            self.assertEqual(
                execution.portfolio_summary["symbols"],
                ["AAPL", "QQQ", "SPY"],
            )
            self.assertEqual(execution.checkpoint_count, 5)
            self.assertGreaterEqual(execution.event_count, 4)
            self.assertIn("parent.portfolio_supervisor", execution.execution_path)
            mermaid = runtime.mermaid()
            self.assertIn("candidate_screen", mermaid)
            self.assertIn("symbol_research", mermaid)
            self.assertIn("portfolio_supervisor", mermaid)

    def test_fresh_run_replaces_stale_parent_thread_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = LangGraphResearchParentRuntime(
                event_path=root / "events.jsonl"
            )
            database = root / "parent.db"
            common = {
                "thread_id": "reused-parent-thread",
                "symbol_runner": lambda candidate: {
                    "symbol": candidate["symbol"],
                    "status": "completed",
                },
                "portfolio_runner": lambda rows: {
                    "status": "completed",
                    "symbols": [row["symbol"] for row in rows],
                },
                "audit_runner": lambda _name, function: function(),
                "checkpointer_path": database,
            }
            runtime.run(
                **common,
                candidate_selector=lambda: [
                    {"symbol": "SPY", "priority": 0.9},
                    {"symbol": "QQQ", "priority": 0.8},
                ],
            )
            second = runtime.run(
                **common,
                candidate_selector=lambda: [
                    {"symbol": "AAPL", "priority": 0.7},
                ],
            )
            self.assertEqual(
                [row["symbol"] for row in second.candidate_results],
                ["AAPL"],
            )
            self.assertEqual(second.portfolio_summary["symbols"], ["AAPL"])

    def test_resume_reruns_only_failed_dynamic_candidate(self):
        counts: Counter[str] = Counter()
        fail_once = {"QQQ": True}

        def symbol_runner(candidate):
            symbol = str(candidate["symbol"])
            counts[symbol] += 1
            if symbol == "QQQ" and fail_once["QQQ"]:
                fail_once["QQQ"] = False
                raise RuntimeError("injected QQQ parent failure")
            return {"symbol": symbol, "call": counts[symbol]}

        def portfolio_runner(rows):
            counts["PORTFOLIO"] += 1
            return {
                "status": "completed",
                "symbols": [row["symbol"] for row in rows],
            }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = LangGraphResearchParentRuntime(
                event_path=root / "events.jsonl"
            )
            arguments = {
                "thread_id": "parent-resume",
                "candidate_selector": lambda: [
                    {"symbol": "SPY", "priority": 0.9},
                    {"symbol": "QQQ", "priority": 0.8},
                    {"symbol": "AAPL", "priority": 0.7},
                ],
                "symbol_runner": symbol_runner,
                "portfolio_runner": portfolio_runner,
                "audit_runner": lambda _name, function: function(),
                "checkpointer_path": root / "parent.db",
            }
            with self.assertRaisesRegex(RuntimeError, "injected QQQ"):
                runtime.run(**arguments)
            resumed = runtime.run(**arguments, resume=True)
            self.assertEqual(counts["SPY"], 1)
            self.assertEqual(counts["AAPL"], 1)
            self.assertEqual(counts["QQQ"], 2)
            self.assertEqual(counts["PORTFOLIO"], 1)
            self.assertEqual(len(resumed.candidate_results), 3)

    def test_daily_workflow_defaults_to_parent_graph(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = replace(
                SETTINGS,
                workflow_artifact_dir=str(root / "workflows"),
                workflow_research_parent_checkpoint_database=str(
                    root / "parent.db"
                ),
                workflow_langgraph_checkpoint_database=str(root / "children.db"),
                workflow_langchain_trace_path=str(root / "langchain.jsonl"),
                workflow_langgraph_event_path=str(root / "langgraph.jsonl"),
            )
            result = DailyWorkflow(settings, ROOT).execute(mode="dry_run")
            research = result["research"]["result"]
            parent = research["parent_graph"]
            self.assertTrue(parent["enabled"])
            self.assertTrue(parent["dynamic_send_fan_out"])
            self.assertTrue(parent["thread_id"].endswith(":RESEARCH_PARENT"))
            self.assertGreater(parent["checkpoint_count"], 0)
            self.assertEqual(research["planned_calls"], 29)
            selected = sorted(research["candidates"])
            self.assertEqual(len(selected), settings.workflow_llm_candidate_limit)
            self.assertTrue(set(selected).issubset(settings.workflow_symbols))
            for symbol in selected:
                self.assertIn(
                    f"parent.symbol.{symbol}",
                    parent["execution_path"],
                )
            self.assertEqual(research["portfolio_supervisor"]["status"], "completed")
            self.assertFalse(result["paper"]["mutated"])
            self.assertFalse(result["plan"]["external_requests_enabled"])
            self.assertTrue((root / "parent.db").is_file())
            self.assertTrue((root / "children.db").is_file())


if __name__ == "__main__":
    unittest.main()
