import unittest
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException

from tradinglab_agents.api.app import (
    PROJECT_ROOT,
    PaperAccountCreate,
    PaperReviewRequest,
    PaperSessionRequest,
    RunRequest,
    WorkflowDryRunRequest,
    _safe_path,
    approve_paper_order,
    backtest,
    create_paper_account,
    experiments,
    health,
    paper_account,
    paper_orders,
    research,
    root,
    workflow_dry_run,
    workflow_plan,
    run_paper_session,
    runs,
)


class ApiTest(unittest.TestCase):
    def test_root_and_health(self):
        self.assertEqual(root()["service"], "TradeLab-Agent")
        result = health()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["mode"], "paper-trading-only")
        self.assertEqual(result["version"], "0.9.0")

    def test_workflow_api_is_dry_run_only(self):
        plan = workflow_plan()
        self.assertFalse(plan["external_requests_enabled"])
        self.assertEqual(plan["remote_llm"]["provider"], "zhipu")
        result = workflow_dry_run(WorkflowDryRunRequest())
        self.assertEqual(result["mode"], "dry_run")
        self.assertFalse(result["paper"]["mutated"])

    def test_backtest_endpoint_function(self):
        result = backtest(RunRequest())
        self.assertEqual(result["symbol"], "DEMO")
        self.assertIn("sharpe", result["metrics"])
        self.assertGreater(result["decision_count"], 0)

    def test_research_endpoint_returns_approval_gated_plan(self):
        result = research(RunRequest())
        self.assertIn(result["manager"]["action"], {"BUY", "HOLD", "SELL"})
        self.assertTrue(result["trader"]["requires_human_approval"])
        self.assertLessEqual(result["trader"]["target_weight"], 0.20)

    def test_experiment_endpoint_returns_audit(self):
        result = experiments(RunRequest(persist=False))
        self.assertTrue(result["audit"]["passed"])
        self.assertIn("without_regime_guard", {row["name"] for row in result["summary"]})

    def test_runs_endpoint_is_bounded(self):
        result = runs(limit=5)
        self.assertIn("runs", result)
        self.assertLessEqual(len(result["runs"]), 5)

    def test_paper_api_lifecycle_is_internal_and_persistent(self):
        token = uuid4().hex
        account_id = f"api-{token[:10]}"
        relative_db = f"artifacts/test-paper-api-{token}.db"
        database = PROJECT_ROOT / relative_db
        try:
            created = create_paper_account(
                PaperAccountCreate(
                    account_id=account_id,
                    symbols=["SPY", "QQQ", "AAPL", "MSFT", "NVDA"],
                    database_path=relative_db,
                )
            )
            self.assertFalse(created["external_broker"])
            first = run_paper_session(
                account_id,
                PaperSessionRequest(database_path=relative_db),
            )
            self.assertFalse(first["external_broker"])
            queued = paper_orders(
                account_id,
                database_path=relative_db,
                config_path="config/default.yaml",
            )["orders"]
            self.assertTrue(queued)
            for order in queued:
                approve_paper_order(
                    order["order_id"],
                    PaperReviewRequest(
                        reviewer="api-test",
                        database_path=relative_db,
                    ),
                )
            second = run_paper_session(
                account_id,
                PaperSessionRequest(database_path=relative_db),
            )
            self.assertGreater(second["payload"]["orders_executed"], 0)
            restored = paper_account(
                account_id,
                database_path=relative_db,
                config_path="config/default.yaml",
            )
            self.assertTrue(restored["account"]["positions"])
        finally:
            for suffix in ("", "-wal", "-shm"):
                Path(str(database) + suffix).unlink(missing_ok=True)

    def test_path_escape_is_rejected(self):
        with self.assertRaises(HTTPException):
            _safe_path("../reference/TradingAgents/README.md")


if __name__ == "__main__":
    unittest.main()
