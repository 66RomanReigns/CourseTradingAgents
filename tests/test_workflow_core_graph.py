import tempfile
import threading
import unittest
from collections import Counter
from dataclasses import replace
from pathlib import Path

from tradinglab_agents.config import load_settings
from tradinglab_agents.storage.langgraph_checkpoints import (
    open_sqlite_checkpoint_saver,
)
from tradinglab_agents.workflows.daily import DailyWorkflow
from tradinglab_agents.workflows.workflow_core import LangGraphWorkflowCoreRuntime


ROOT = Path(__file__).resolve().parents[1]
SETTINGS = load_settings(ROOT / "config/default.yaml")


class LangGraphCheckpointConcurrencyTest(unittest.TestCase):
    def test_shared_sqlite_schema_setup_is_safe_under_parallel_subgraphs(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "shared.db"
            barrier = threading.Barrier(12)
            errors: list[Exception] = []

            def open_saver(index: int) -> None:
                try:
                    barrier.wait()
                    with open_sqlite_checkpoint_saver(database) as saver:
                        list(
                            saver.list(
                                {
                                    "configurable": {
                                        "thread_id": f"parallel-{index}"
                                    }
                                },
                                limit=1,
                            )
                        )
                except Exception as exc:  # pragma: no cover - diagnostic collection
                    errors.append(exc)

            threads = [
                threading.Thread(target=open_saver, args=(index,))
                for index in range(12)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(errors, [])
            self.assertTrue(database.is_file())


class WorkflowCoreRuntimeTest(unittest.TestCase):
    @staticmethod
    def _validation():
        return {
            "status": "completed",
            "market_files": 2,
            "data_dir": "fixture",
        }

    @staticmethod
    def _research():
        return {
            "status": "completed",
            "candidates": {
                "SPY": {"result": {}},
                "QQQ": {"result": {}},
            },
        }

    @staticmethod
    def _overlays(_research):
        return {
            "SPY": {
                "action": "HOLD",
                "target_weight": 0.0,
                "requires_human_approval": True,
            },
            "QQQ": {
                "action": "HOLD",
                "target_weight": 0.0,
                "requires_human_approval": True,
            },
        }

    @staticmethod
    def _preparation(_validation, _research, overlays):
        return {
            "status": "ready" if overlays else "no_research",
            "overlay_count": len(overlays),
            "safe_for_paper_input": True,
            "external_broker": False,
        }

    def test_core_graph_runs_all_stages_and_records_only_expected_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = LangGraphWorkflowCoreRuntime(
                event_path=root / "events.jsonl"
            )
            result = runtime.run(
                thread_id="core-basic",
                research_enabled=True,
                data_validator=self._validation,
                research_runner=self._research,
                overlay_assembler=self._overlays,
                decision_preparer=self._preparation,
                audit_runner=lambda _name, function: function(),
                checkpointer_path=root / "core.db",
            )
            self.assertEqual(
                result.execution_path,
                (
                    "core.data_validation",
                    "core.research_parent",
                    "core.overlay_assembly",
                    "core.decision_preparation",
                ),
            )
            self.assertEqual(sorted(result.research_overlays), ["QQQ", "SPY"])
            self.assertTrue(result.decision_preparation["safe_for_paper_input"])
            self.assertGreater(result.checkpoint_count, 0)
            self.assertTrue((root / "core.db").is_file())
            mermaid = runtime.mermaid()
            for node in (
                "data_validation",
                "research_parent",
                "overlay_assembly",
                "decision_preparation",
            ):
                self.assertIn(node, mermaid)

    def test_resume_reruns_only_failed_overlay_stage(self):
        counts: Counter[str] = Counter()
        fail_once = {"overlay": True}

        def validation():
            counts["validation"] += 1
            return self._validation()

        def research():
            counts["research"] += 1
            return self._research()

        def overlay(payload):
            counts["overlay"] += 1
            if fail_once["overlay"]:
                fail_once["overlay"] = False
                raise RuntimeError("injected overlay failure")
            return self._overlays(payload)

        def preparation(validation_payload, research_payload, overlays):
            counts["preparation"] += 1
            return self._preparation(
                validation_payload,
                research_payload,
                overlays,
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = LangGraphWorkflowCoreRuntime(
                event_path=root / "events.jsonl"
            )
            arguments = dict(
                thread_id="core-resume",
                research_enabled=True,
                data_validator=validation,
                research_runner=research,
                overlay_assembler=overlay,
                decision_preparer=preparation,
                audit_runner=lambda _name, function: function(),
                checkpointer_path=root / "core.db",
            )
            with self.assertRaisesRegex(RuntimeError, "injected overlay failure"):
                runtime.run(**arguments)
            resumed = runtime.run(**arguments, resume=True)
            self.assertEqual(counts["validation"], 1)
            self.assertEqual(counts["research"], 1)
            self.assertEqual(counts["overlay"], 2)
            self.assertEqual(counts["preparation"], 1)
            self.assertEqual(resumed.decision_preparation["status"], "ready")

    def test_fresh_run_replaces_stale_thread_and_research_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = LangGraphWorkflowCoreRuntime(
                event_path=root / "events.jsonl"
            )
            common = dict(
                thread_id="core-reused",
                research_enabled=False,
                data_validator=self._validation,
                research_runner=lambda: self.fail("research must be skipped"),
                overlay_assembler=lambda research: (
                    {} if research.get("status") == "disabled" else self.fail()
                ),
                decision_preparer=self._preparation,
                audit_runner=lambda _name, function: function(),
                checkpointer_path=root / "core.db",
            )
            first = runtime.run(**common)
            second = runtime.run(**common)
            self.assertEqual(first.research["status"], "disabled")
            self.assertEqual(second.research["status"], "disabled")
            self.assertEqual(second.research_overlays, {})
            self.assertEqual(second.decision_preparation["status"], "no_research")
            self.assertIn("core.research_disabled", second.execution_path)

    def test_unsafe_decision_preparation_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = LangGraphWorkflowCoreRuntime()
            with self.assertRaisesRegex(
                ValueError,
                "cannot enable an external broker",
            ):
                runtime.run(
                    thread_id="core-unsafe",
                    research_enabled=True,
                    data_validator=self._validation,
                    research_runner=self._research,
                    overlay_assembler=self._overlays,
                    decision_preparer=lambda _v, _r, _o: {
                        "status": "ready",
                        "safe_for_paper_input": True,
                        "external_broker": True,
                    },
                    audit_runner=lambda _name, function: function(),
                    checkpointer_path=Path(directory) / "core.db",
                )


class WorkflowCoreDailyWorkflowTest(unittest.TestCase):
    def test_daily_workflow_defaults_to_core_graph_and_prepares_hashed_overlays(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = replace(
                SETTINGS,
                workflow_artifact_dir=str(root / "workflows"),
                workflow_core_checkpoint_database=str(root / "core.db"),
                workflow_research_parent_checkpoint_database=str(root / "parent.db"),
                workflow_langgraph_checkpoint_database=str(root / "children.db"),
                workflow_langchain_trace_path=str(root / "langchain.jsonl"),
                workflow_langgraph_event_path=str(root / "langgraph.jsonl"),
            )
            result = DailyWorkflow(settings, ROOT).execute(mode="dry_run")
            core = result["workflow_core"]
            preparation = result["decision_preparation"]
            self.assertTrue(core["enabled"])
            self.assertEqual(core["status"], "completed")
            self.assertTrue(core["thread_id"].endswith(":WORKFLOW_CORE"))
            self.assertEqual(
                core["execution_path"],
                [
                    "core.data_validation",
                    "core.research_parent",
                    "core.overlay_assembly",
                    "core.decision_preparation",
                ],
            )
            self.assertEqual(preparation["overlay_count"], 2)
            self.assertEqual(len(preparation["overlay_payload_sha256"]), 64)
            self.assertTrue(preparation["safe_for_paper_input"])
            self.assertTrue(preparation["all_require_human_approval"])
            self.assertTrue(preparation["all_non_expansion_verified"])
            self.assertFalse(core["provider_refresh_in_graph"])
            self.assertFalse(core["paper_execution_in_graph"])
            self.assertFalse(result["paper"]["mutated"])
            self.assertFalse(result["plan"]["external_requests_enabled"])


if __name__ == "__main__":
    unittest.main()
