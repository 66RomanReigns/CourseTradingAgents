import json
import tempfile
import threading
import time
import unittest
from collections import Counter
from datetime import datetime
from pathlib import Path

from tradinglab_agents.agents.langgraph_research import LangGraphResearchRuntime
from tradinglab_agents.agents.llm import MockLLM
from tradinglab_agents.agents.research import MultiAgentResearchPipeline
from tradinglab_agents.models import Evidence, EvidencePack
from tradinglab_agents.workflows.state import WorkflowStateStore


class LangGraphRuntimeTest(unittest.TestCase):
    @staticmethod
    def _pack() -> EvidencePack:
        now = datetime(2025, 1, 2, 16)
        pack = EvidencePack(symbol="X", decision_time=now)
        for evidence_id, kind, value in (
            ("news.1", "news", "strong growth partnership"),
            ("macro.1", "macro", "cooling inflation and rate cut"),
            ("fund.1", "fundamental", "record profit growth"),
        ):
            pack.add(
                Evidence(
                    evidence_id=evidence_id,
                    kind=kind,
                    timestamp=now,
                    available_at=now,
                    value=value,
                    source="fixture",
                    detail=f"{kind} fixture",
                )
            )
        return pack

    def test_native_graph_runs_parallel_branches_loop_and_langchain_roles(self):
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory) / "langchain.jsonl"
            event_log = Path(directory) / "langgraph.jsonl"
            pipeline = MultiAgentResearchPipeline(
                MockLLM(),
                debate_rounds=2,
                langchain_trace_path=trace,
                langgraph_event_path=event_log,
            )
            result = pipeline.run(self._pack())
            execution = pipeline._last_graph_execution

            self.assertIsNotNone(execution)
            self.assertEqual(len(result.debate_rounds), 2)
            self.assertEqual(len(result.risk_reviews), 3)
            self.assertGreaterEqual(execution.checkpoint_count, 10)
            self.assertIn("debate_dispatch", execution.execution_path)
            self.assertIn("human_review_gate", execution.execution_path)
            records = [
                json.loads(line)
                for line in trace.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(records), 26)
            self.assertEqual(sum(row["event"] == "role_start" for row in records), 13)
            self.assertEqual(sum(row["event"] == "role_end" for row in records), 13)
            serialized = json.dumps(records)
            self.assertNotIn("system_prompt", serialized)
            self.assertNotIn("api_key", serialized)
            graph_events = [
                json.loads(line)
                for line in event_log.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(graph_events[0]["event"], "graph_start")
            self.assertEqual(graph_events[-1]["event"], "graph_complete")
            self.assertEqual(
                sum(row["event"] == "graph_step" for row in graph_events),
                execution.event_count,
            )
            serialized_events = json.dumps(graph_events).lower()
            for forbidden in (
                "target_weight",
                "rationale",
                "system_prompt",
                "api_key",
                "evidence_ids",
            ):
                self.assertNotIn(forbidden, serialized_events)

            runtime = LangGraphResearchRuntime(
                MockLLM(),
                MockLLM(),
                debate_rounds=2,
                risk_personas=("aggressive", "balanced", "conservative"),
                trace_path=trace,
            )
            mermaid = runtime.mermaid(self._pack())
            self.assertIn("bull_researcher", mermaid)
            self.assertIn("human_review_gate", mermaid)

    def test_sqlite_resume_keeps_successful_parallel_sibling_writes(self):
        counts: Counter[str] = Counter()
        fail_once = {"macro": True}

        def runner(name, model, function):
            del model
            counts[name] += 1
            if name == "macro_analyst" and fail_once["macro"]:
                fail_once["macro"] = False
                raise RuntimeError("injected macro failure")
            return function()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = LangGraphResearchRuntime(
                MockLLM(),
                MockLLM(),
                debate_rounds=2,
                risk_personas=("aggressive", "balanced", "conservative"),
                trace_path=root / "trace.jsonl",
                retry_attempts=1,
            )
            database = root / "graph.db"
            with self.assertRaisesRegex(RuntimeError, "injected macro failure"):
                runtime.run(
                    self._pack(),
                    thread_id="resume-thread",
                    node_runner=runner,
                    checkpointer_path=database,
                )

            resumed = runtime.run(
                self._pack(),
                thread_id="resume-thread",
                node_runner=runner,
                checkpointer_path=database,
                resume=True,
            )
            self.assertFalse(resumed.interrupted)
            self.assertIsNotNone(resumed.result)
            self.assertEqual(counts["news_analyst"], 1)
            self.assertEqual(counts["fundamental_analyst"], 1)
            self.assertEqual(counts["macro_analyst"], 2)
            self.assertTrue(database.is_file())

    def test_directional_interrupt_can_resume_with_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = LangGraphResearchRuntime(
                MockLLM(),
                MockLLM(),
                debate_rounds=2,
                risk_personas=("aggressive", "balanced", "conservative"),
                trace_path=root / "trace.jsonl",
                retry_attempts=1,
                human_review_mode="interrupt_directional",
            )
            database = root / "hitl.db"
            first = runtime.run(
                self._pack(),
                thread_id="hitl-thread",
                node_runner=lambda _name, _model, function: function(),
                checkpointer_path=database,
            )
            self.assertTrue(first.interrupted)
            self.assertEqual(first.interrupt_payloads[0]["type"], "TRADING_RESEARCH_REVIEW")
            self.assertEqual(first.interrupt_payloads[0]["proposed_plan"]["action"], "BUY")

            second = runtime.run(
                self._pack(),
                thread_id="hitl-thread",
                node_runner=lambda _name, _model, function: function(),
                checkpointer_path=database,
                resume=True,
                human_response={"decision": "reject"},
            )
            self.assertFalse(second.interrupted)
            self.assertIsNotNone(second.result)
            self.assertEqual(second.result.trader.action, "HOLD")
            self.assertEqual(second.latest_state["human_review"]["status"], "REJECTED")

    def test_cost_aware_hold_route_skips_execution_committee(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            neutral_pack = EvidencePack(symbol="X", decision_time=datetime(2025, 1, 2, 16))
            runtime = LangGraphResearchRuntime(
                MockLLM(),
                MockLLM(),
                debate_rounds=1,
                risk_personas=("aggressive", "balanced", "conservative"),
                trace_path=root / "trace.jsonl",
                cost_aware_routing=True,
                hold_skip_confidence=1.0,
            )
            result = runtime.run(
                neutral_pack,
                thread_id="cost-route",
                node_runner=lambda _name, _model, function: function(),
            )
            self.assertIsNotNone(result.result)
            self.assertEqual(result.result.trader.action, "HOLD")
            self.assertEqual(result.result.risk_reviews, ())
            self.assertIn("cost_aware_hold", result.execution_path)


class WorkflowStateConcurrencyTest(unittest.TestCase):
    def test_parallel_json_audit_updates_do_not_lose_nodes(self):
        with tempfile.TemporaryDirectory() as directory:
            store = WorkflowStateStore(
                Path(directory) / "run",
                run_id="parallel-audit",
                mode="dry_run",
                settings_hash="settings",
                resume=False,
            )
            errors: list[Exception] = []

            def execute(index: int) -> None:
                try:
                    store.run_json_node(
                        f"parallel.{index}",
                        lambda index=index: (time.sleep(0.01), {"index": index})[1],
                    )
                except Exception as exc:  # pragma: no cover - diagnostic collection
                    errors.append(exc)

            threads = [threading.Thread(target=execute, args=(index,)) for index in range(12)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(errors, [])
            state = store.state
            self.assertEqual(len(state["completed_nodes"]), 12)
            self.assertEqual(state["active_nodes"], [])
            self.assertEqual(len(list(store.nodes_dir.glob("*.json"))), 12)


if __name__ == "__main__":
    unittest.main()
