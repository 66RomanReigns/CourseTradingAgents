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
    market_calendar,
    market_data_quality,
    market_semantics,
    paper_account,
    paper_corporate_actions,
    paper_orders,
    portfolio_research_graph,
    provider_capabilities,
    provider_conflicts,
    provider_events,
    provider_health,
    provider_usage,
    research,
    research_graph,
    research_parent_graph,
    research_thread_status,
    root,
    data_provider_graph,
    workflow_core_graph,
    workflow_decision_graph,
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
        self.assertEqual(result["version"], "0.18.0")
        orchestration = result["orchestration"]
        self.assertEqual(orchestration["market_calendar"], "XNYS")
        self.assertTrue(orchestration["strict_market_sessions"])
        self.assertTrue(orchestration["corporate_action_ledger"])
        self.assertEqual(orchestration["paper_schema_version"], 3)
        self.assertIn("exchange_calendars", orchestration)
        self.assertTrue(orchestration["data_provider_graph"])
        self.assertTrue(orchestration["provider_fallback_router"])
        self.assertTrue(orchestration["provider_quality_gate"])
        self.assertTrue(orchestration["fallback_only_reduces_risk"])
        self.assertFalse(orchestration["blocked_provider_data_persisted"])

    def test_workflow_api_is_dry_run_only(self):
        plan = workflow_plan()
        self.assertFalse(plan["external_requests_enabled"])
        self.assertEqual(plan["remote_llm"]["provider"], "zhipu")
        market = plan["market_runtime"]
        self.assertEqual(market["calendar"], "XNYS")
        self.assertTrue(market["strict_sessions"])
        self.assertTrue(market["require_complete_alignment"])
        self.assertTrue(market["corporate_actions_enabled"])
        self.assertTrue(market["adjust_history_for_dividends"])
        self.assertTrue(market["raw_prices_used_for_execution"])
        provider_runtime = plan["provider_runtime"]
        self.assertTrue(provider_runtime["graph_enabled"])
        self.assertEqual(provider_runtime["thread_suffix"], "DATA_PROVIDER")
        self.assertTrue(provider_runtime["fallback_only_reduces_risk"])
        self.assertFalse(provider_runtime["blocked_data_persisted"])
        self.assertGreaterEqual(len(provider_runtime["capabilities"]), 9)
        structured = next(
            step for step in plan["steps"] if step["name"] == "structured_research"
        )
        core = structured["langgraph"]["workflow_core_graph"]
        self.assertTrue(core["enabled"])
        self.assertFalse(core["provider_refresh_in_graph"])
        self.assertFalse(core["paper_execution_in_graph"])
        decision = structured["langgraph"]["deterministic_decision_graph"]
        self.assertTrue(decision["enabled"])
        self.assertFalse(decision["account_mutation_in_graph"])
        self.assertFalse(decision["order_persistence_in_graph"])
        result = workflow_dry_run(WorkflowDryRunRequest())
        self.assertEqual(result["mode"], "dry_run")
        self.assertFalse(result["paper"]["mutated"])
        self.assertEqual(result["workflow_core"]["status"], "completed")
        self.assertTrue(result["decision_preparation"]["safe_for_paper_input"])

    def test_market_semantics_endpoints_are_local_and_bounded(self):
        calendar = market_calendar(
            start="2025-11-24",
            end="2025-12-01",
        )
        self.assertEqual(calendar["calendar"], "XNYS")
        self.assertEqual(calendar["early_close_count"], 1)
        self.assertFalse(calendar["external_request"])

        semantics = market_semantics()
        self.assertEqual(semantics["status"], "completed")
        self.assertEqual(semantics["dropped_timestamp_count"], 0)
        self.assertGreaterEqual(semantics["corporate_action_count"], 5)
        self.assertFalse(semantics["external_request"])
        self.assertFalse(semantics["external_broker"])

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
        self.assertIn("thread_id", result["graph_runtime"])
        self.assertGreater(result["graph_runtime"]["checkpoint_count"], 0)
        self.assertFalse(result["external_broker"])

    def test_research_graph_and_missing_thread_are_exposed_without_secrets(self):
        graph = research_graph(symbol="DEMO")
        self.assertEqual(graph["runtime"], "langgraph")
        self.assertIn("bull_researcher", graph["mermaid"])
        self.assertIn("human_review_gate", graph["mermaid"])
        self.assertFalse(graph["external_broker"])

        parent_graph = research_parent_graph()
        self.assertEqual(parent_graph["graph_type"], "research_parent")
        self.assertTrue(parent_graph["dynamic_send_fan_out"])
        self.assertIn("candidate_screen", parent_graph["mermaid"])
        self.assertIn("symbol_research", parent_graph["mermaid"])
        self.assertIn("portfolio_supervisor", parent_graph["mermaid"])
        self.assertFalse(parent_graph["external_broker"])

        portfolio_graph = portfolio_research_graph()
        self.assertEqual(portfolio_graph["graph_type"], "portfolio_supervisor")
        self.assertIn("correlation_reviewer", portfolio_graph["mermaid"])
        self.assertIn("deterministic_guard", portfolio_graph["mermaid"])
        self.assertTrue(portfolio_graph["deterministic_non_expansion_guard"])
        self.assertFalse(portfolio_graph["external_broker"])

        provider_graph = data_provider_graph()
        self.assertEqual(provider_graph["runtime"], "langgraph")
        self.assertIn("route_request", provider_graph["mermaid"])
        self.assertIn("persist_and_gate", provider_graph["mermaid"])
        self.assertTrue(provider_graph["dynamic_send_fan_out"])
        self.assertTrue(provider_graph["fallback_only_reduces_risk"])
        self.assertFalse(provider_graph["blocked_data_persisted"])
        self.assertFalse(provider_graph["external_request"])

        core_graph = workflow_core_graph()
        self.assertEqual(core_graph["graph_type"], "workflow_core")
        self.assertIn("data_validation", core_graph["mermaid"])
        self.assertIn("overlay_assembly", core_graph["mermaid"])
        self.assertIn("decision_preparation", core_graph["mermaid"])
        self.assertFalse(core_graph["provider_refresh_in_graph"])
        self.assertFalse(core_graph["paper_execution_in_graph"])
        self.assertFalse(core_graph["external_broker"])

        decision_graph = workflow_decision_graph()
        self.assertEqual(
            decision_graph["graph_type"],
            "deterministic_decision",
        )
        self.assertIn("quant_signal", decision_graph["mermaid"])
        self.assertIn("fusion_critic", decision_graph["mermaid"])
        self.assertIn("regime_guard", decision_graph["mermaid"])
        self.assertIn("portfolio_risk", decision_graph["mermaid"])
        self.assertFalse(decision_graph["account_mutation_in_graph"])
        self.assertFalse(decision_graph["order_persistence_in_graph"])
        self.assertFalse(decision_graph["external_broker"])

        status = research_thread_status(f"missing-{uuid4().hex}")
        self.assertFalse(status["exists"])
        self.assertNotIn("api_key", str(status).lower())

    def test_experiment_endpoint_returns_audit(self):
        result = experiments(RunRequest(persist=False))
        self.assertTrue(result["audit"]["passed"])
        self.assertIn("without_regime_guard", {row["name"] for row in result["summary"]})

    def test_runs_endpoint_is_bounded(self):
        result = runs(limit=5)
        self.assertIn("runs", result)
        self.assertLessEqual(len(result["runs"]), 5)

    def test_provider_usage_endpoint_is_secret_free(self):
        result = provider_usage(run_id="missing-run")
        self.assertEqual(result["run_id"], "missing-run")
        self.assertEqual(result["providers"], [])
        self.assertNotIn("api_key", str(result).lower())

    def test_provider_graph_endpoints_are_secret_free_and_local(self):
        capabilities = provider_capabilities()
        self.assertGreaterEqual(len(capabilities["providers"]), 9)
        self.assertFalse(capabilities["credentials_included"])
        self.assertFalse(capabilities["external_request"])
        alpha = [
            item
            for item in capabilities["providers"]
            if item["rate_limit_group"] == "alpha_vantage"
        ]
        self.assertGreaterEqual(len(alpha), 2)
        self.assertTrue(all(item["min_interval_seconds"] >= 15 for item in alpha))

        health_payload = provider_health()
        self.assertFalse(health_payload["credentials_included"])
        self.assertFalse(health_payload["external_request"])
        events_payload = provider_events(run_id="missing-run")
        self.assertEqual(events_payload["events"], [])
        conflicts_payload = provider_conflicts(run_id="missing-run")
        self.assertEqual(conflicts_payload["events"], [])
        quality = market_data_quality(run_id="missing-run")
        self.assertTrue(quality["fallback_only_reduces_risk"])
        self.assertFalse(quality["external_request"])
        self.assertFalse(quality["thread"]["exists"])
        for payload in (
            capabilities,
            health_payload,
            events_payload,
            conflicts_payload,
            quality,
        ):
            self.assertNotIn("api_key", str(payload).lower())

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
            actions = paper_corporate_actions(
                account_id,
                database_path=relative_db,
                config_path="config/default.yaml",
            )
            self.assertEqual(actions["events"], [])
            self.assertFalse(actions["external_broker"])
        finally:
            for suffix in ("", "-wal", "-shm"):
                Path(str(database) + suffix).unlink(missing_ok=True)

    def test_path_escape_is_rejected(self):
        with self.assertRaises(HTTPException):
            _safe_path("../reference/TradingAgents/README.md")


if __name__ == "__main__":
    unittest.main()
