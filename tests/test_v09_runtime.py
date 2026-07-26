import json
import os
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from tradinglab_agents.agents.analysts import MacroAnalystAgent, NewsAnalystAgent
from tradinglab_agents.api.app import app
from tradinglab_agents.config import load_settings
from tradinglab_agents.paper.models import ApprovalPolicy
from tradinglab_agents.paper.scheduler import account_lock_path
from tradinglab_agents.paper.service import PaperTradingService
from tradinglab_agents.storage.paper_store import PaperTradingStore
from tradinglab_agents.workflows.daily import DailyWorkflow
from tradinglab_agents.workflows.state import WorkflowExecutionError


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data" / "multi_sample"
SETTINGS = load_settings(ROOT / "config" / "default.yaml")


class ApiAuthenticationTest(unittest.TestCase):
    def test_public_health_and_protected_routes(self):
        client = TestClient(app)
        with patch.dict(os.environ, {"TRADINGLAB_API_TOKEN": "operator-test-token"}):
            self.assertEqual(client.get("/health").status_code, 200)
            self.assertEqual(client.get("/workflow/plan").status_code, 401)
            authorized = client.get(
                "/workflow/plan",
                headers={"X-API-Key": "operator-test-token"},
            )
            self.assertEqual(authorized.status_code, 200)
            self.assertFalse(authorized.json()["external_requests_enabled"])

    def test_missing_server_token_fails_closed(self):
        client = TestClient(app)
        with patch.dict(os.environ, {}, clear=True):
            response = client.get("/workflow/plan")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["required_env"], "TRADINGLAB_API_TOKEN")


class DatabaseMigrationAndMemoryTest(unittest.TestCase):
    def test_legacy_database_migrates_and_future_schema_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy = Path(directory) / "legacy.db"
            with sqlite3.connect(legacy) as connection:
                connection.execute("PRAGMA user_version = 1")
            store = PaperTradingStore(legacy)
            self.assertEqual(store.schema_version, 3)
            with sqlite3.connect(legacy) as connection:
                row = connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name='paper_decision_memories'"
                ).fetchone()
                corporate_row = connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name='paper_corporate_action_events'"
                ).fetchone()
            self.assertIsNotNone(row)
            self.assertIsNotNone(corporate_row)

            future = Path(directory) / "future.db"
            with sqlite3.connect(future) as connection:
                connection.execute("PRAGMA user_version = 99")
            with self.assertRaisesRegex(RuntimeError, "newer than supported"):
                PaperTradingStore(future)

    def test_research_trace_is_preserved_and_attributed(self):
        with tempfile.TemporaryDirectory() as directory:
            store = PaperTradingStore(Path(directory) / "paper.db")
            service = PaperTradingService(store, SETTINGS)
            account_id = "trace-demo"
            service.initialize_account(
                account_id,
                name="Trace Demo",
                symbols=SETTINGS.workflow_symbols,
                initial_cash=100_000.0,
                approval_policy=ApprovalPolicy.NONE,
            )
            market, _ = service.load_market(DATA_DIR, SETTINGS.workflow_symbols)
            decision_timestamp = market.timestamps[60]
            outcome_timestamp = market.timestamps[61]
            run_id = "paper-trace-demo"
            store.begin_daily_run(
                run_id=run_id,
                account_id=account_id,
                session_date=decision_timestamp.date().isoformat(),
                open_at=market.open_snapshot(decision_timestamp).timestamp,
                close_at=market.close_snapshot(decision_timestamp).timestamp,
            )
            trace = {
                "graph_version": "tiered_research_graph_v1",
                "clients": {"quick": "mock:quick", "deep": "mock:deep"},
                "debate_rounds": [{"round_index": 1}, {"round_index": 2}],
                "preliminary_trader": {"action": "BUY"},
                "risk_reviews": [
                    {"persona": "balanced", "verdict": "REDUCE"},
                    {"persona": "conservative", "verdict": "VETO"},
                ],
                "portfolio_manager": {"action": "HOLD"},
            }
            store.record_decision_memories(
                account_id=account_id,
                run_id=run_id,
                decision_time=market.decision_time(decision_timestamp).isoformat(),
                market_timestamp=decision_timestamp.isoformat(),
                symbol_decisions={
                    "SPY": {
                        "final_action": "HOLD",
                        "confidence": 0.6,
                        "current_weight_at_decision": 0.0,
                        "proposed_target_weight": 0.0,
                        "evidence_ids": [],
                        "research_overlay": {"trace": trace},
                        "research_overlay_policy": "committee veto",
                    }
                },
                approved_targets={"SPY": 0.0},
                benchmark_symbol="QQQ",
                horizon_sessions=1,
            )
            pending = store.list_decision_memories(account_id)[0]
            self.assertEqual(
                pending["rationale"]["research_trace"]["graph_version"],
                "tiered_research_graph_v1",
            )
            self.assertEqual(
                service._mature_decision_memories(
                    account_id,
                    market,
                    outcome_timestamp,
                ),
                1,
            )
            matured = store.list_decision_memories(account_id)[0]
            attribution = matured["reflection"]["research_attribution"]
            self.assertTrue(attribution["decision_changed_by_committee"])
            self.assertEqual(attribution["veto_count"], 1)
            self.assertEqual(attribution["debate_rounds"], 2)

            workflow = DailyWorkflow(
                replace(
                    SETTINGS,
                    paper_database_path=str(store.path),
                    workflow_memory_feedback_limit=5,
                ),
                ROOT,
            )
            self.assertEqual(
                workflow._decision_memory_context(
                    account_id=account_id,
                    symbol="SPY",
                    decision_time=decision_timestamp,
                ),
                [],
            )
            visible = workflow._decision_memory_context(
                account_id=account_id,
                symbol="SPY",
                decision_time=outcome_timestamp,
            )
            self.assertEqual(len(visible), 1)
            self.assertEqual(visible[0]["memory_id"], matured["memory_id"])

    def test_decisions_are_recorded_and_matured_without_llm(self):
        with tempfile.TemporaryDirectory() as directory:
            store = PaperTradingStore(Path(directory) / "paper.db")
            service = PaperTradingService(store, SETTINGS)
            service.initialize_account(
                "memory-demo",
                name="Memory Demo",
                symbols=SETTINGS.workflow_symbols,
                initial_cash=100_000.0,
                approval_policy=ApprovalPolicy.NONE,
            )
            results = [
                service.run_next_session("memory-demo", data_dir=DATA_DIR)
                for _ in range(6)
            ]
            self.assertGreaterEqual(
                sum(int(result["payload"]["memories_matured"]) for result in results),
                1,
            )
            memories = store.list_decision_memories("memory-demo", limit=5000)
            self.assertGreaterEqual(len(memories), 5)
            matured = [row for row in memories if row["outcome_status"] == "MATURED"]
            self.assertTrue(matured)
            self.assertFalse(matured[0]["reflection"]["llm_generated"])
            self.assertTrue(matured[0]["reflection"]["source_memory_immutable"])
            self.assertIn(matured[0]["action"], {"BUY", "HOLD", "SELL"})


class WorkflowResumeTest(unittest.TestCase):
    def test_resume_skips_completed_research_agent_nodes(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = replace(
                SETTINGS,
                workflow_artifact_dir=directory,
                workflow_llm_candidate_limit=1,
                llm_max_calls_per_run=13,
            )
            workflow = DailyWorkflow(settings, ROOT)
            run_id = "workflow-resume-regression"
            with patch.object(
                MacroAnalystAgent,
                "analyze",
                side_effect=RuntimeError("injected macro failure"),
            ):
                with self.assertRaises(WorkflowExecutionError):
                    workflow.execute(mode="dry_run", run_id=run_id)

            state_path = Path(directory) / run_id / "state.json"
            failed = json.loads(state_path.read_text(encoding="utf-8"))
            candidates = json.loads(
                (Path(directory) / run_id / "nodes" / "candidate_screen.json").read_text(
                    encoding="utf-8"
                )
            )
            symbol = candidates[0]["symbol"]
            self.assertEqual(
                failed["failed_node"],
                f"research.{symbol}.macro_analyst",
            )
            self.assertIn(
                f"research.{symbol}.news_analyst",
                failed["completed_nodes"],
            )

            with patch.object(
                NewsAnalystAgent,
                "analyze",
                side_effect=AssertionError("completed news node must not rerun"),
            ):
                result = workflow.execute(
                    mode="dry_run",
                    run_id=run_id,
                    resume=True,
                )
            self.assertTrue(result["resumed"])
            self.assertFalse(result["paper"]["mutated"])
            completed = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(completed["status"], "COMPLETE")
            self.assertIn(f"research.{symbol}.portfolio_manager", completed["completed_nodes"])

    def test_account_lock_paths_are_isolated_and_sanitized(self):
        base = Path("artifacts/paper_scheduler.lock")
        first = account_lock_path(base, "account-a")
        second = account_lock_path(base, "account/b")
        self.assertNotEqual(first, second)
        self.assertNotIn("/", second.name)


if __name__ == "__main__":
    unittest.main()
