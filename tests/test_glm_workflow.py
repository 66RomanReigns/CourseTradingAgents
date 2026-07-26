import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tradinglab_agents.agents.llm import (
    DryRunLLMClient,
    OpenAICompatibleClient,
    build_llm_client,
)
from tradinglab_agents.agents.schemas import TradePlan
from tradinglab_agents.config import load_settings
from tradinglab_agents.data.csv_provider import LocalCsvProvider
from tradinglab_agents.engine.market import AlignedMarketData
from tradinglab_agents.engine.portfolio_planner import PortfolioPlanner
from tradinglab_agents.models import Portfolio
from tradinglab_agents.workflows.daily import DailyWorkflow


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data" / "multi_sample"
SETTINGS = load_settings(ROOT / "config" / "default.yaml")
SYMBOLS = SETTINGS.workflow_symbols


class FakeCompletions:
    def __init__(self):
        self.request = None

    def create(self, **kwargs):
        self.request = kwargs
        content = json.dumps(
            {
                "action": "HOLD",
                "confidence": 0.5,
                "target_weight": 0.0,
                "order_type": "NO_ORDER",
                "rationale": "validated fake response",
                "evidence_ids": [],
                "requires_human_approval": True,
            }
        )
        message = SimpleNamespace(content=content)
        usage = SimpleNamespace(
            prompt_tokens=120,
            completion_tokens=40,
            total_tokens=160,
        )
        return SimpleNamespace(
            id="glm-smoke-response",
            model="glm-4.7-flash",
            usage=usage,
            choices=[
                SimpleNamespace(
                    message=message,
                    finish_reason="stop",
                )
            ],
        )


class GlmProviderContractTest(unittest.TestCase):
    def test_default_client_is_no_network_glm_dry_run(self):
        client = build_llm_client(SETTINGS, ROOT)
        self.assertEqual(client.identity, "dry-run:zhipu:glm-4.7-flash")
        self.assertIsInstance(client.delegate, DryRunLLMClient)

    def test_explicit_quick_and_deep_models_keep_distinct_identities(self):
        settings = replace(SETTINGS, llm_execution_mode="dry_run")
        quick = build_llm_client(
            settings,
            ROOT,
            model="quick-test-model",
            cache_namespace="quick-test",
        )
        deep = build_llm_client(
            settings,
            ROOT,
            model="deep-test-model",
            cache_namespace="deep-test",
        )
        self.assertEqual(quick.identity, "dry-run:zhipu:quick-test-model")
        self.assertEqual(deep.identity, "dry-run:zhipu:deep-test-model")

    def test_live_glm_requires_key_before_network_use(self):
        settings = replace(SETTINGS, llm_execution_mode="live")
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(ValueError, "API key"):
                build_llm_client(settings, ROOT)

    def test_zhipu_request_contract_includes_structured_json_and_thinking(self):
        completions = FakeCompletions()
        fake_client = SimpleNamespace(
            chat=SimpleNamespace(completions=completions)
        )
        client = OpenAICompatibleClient(
            "glm-4.7-flash",
            "unit-test-placeholder",
            base_url="https://open.bigmodel.cn/api/paas/v4",
            provider_name="zhipu",
            extra_body={"thinking": {"type": "disabled"}},
        )
        client._client = fake_client
        result = client.complete(
            task="trader_plan",
            system_prompt="Return validated JSON only.",
            payload={"research_decision": {"action": "HOLD"}},
            response_model=TradePlan,
        )
        self.assertEqual(result.action, "HOLD")
        self.assertEqual(completions.request["model"], "glm-4.7-flash")
        self.assertEqual(
            completions.request["response_format"],
            {"type": "json_object"},
        )
        self.assertEqual(
            completions.request["extra_body"],
            {"thinking": {"type": "disabled"}},
        )
        self.assertEqual(client.last_metadata["total_tokens"], 160)
        self.assertEqual(client.last_metadata["finish_reason"], "stop")
        self.assertEqual(client.last_metadata["response_id"], "glm-smoke-response")


class WorkflowSafetyTest(unittest.TestCase):
    def test_dry_run_executes_research_without_network_or_account_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = replace(
                SETTINGS,
                workflow_artifact_dir=directory,
            )
            workflow = DailyWorkflow(settings, ROOT)
            with patch.object(
                workflow,
                "_refresh_live_data",
                side_effect=AssertionError("network refresh must not run"),
            ):
                result = workflow.execute(mode="dry_run")
            self.assertFalse(result["plan"]["external_requests_enabled"])
            self.assertEqual(
                result["research"]["result"]["planned_calls"],
                29,
            )
            self.assertFalse(result["paper"]["mutated"])
            self.assertTrue(Path(result["artifact"]).is_file())
            identities = {
                identity
                for row in result["research"]["result"]["candidates"].values()
                for identity in row["clients"].values()
            }
            self.assertEqual(identities, {"dry-run:zhipu:glm-4.7-flash"})
            candidate = next(iter(result["research"]["result"]["candidates"].values()))
            self.assertEqual(len(candidate["result"]["risk_reviews"]), 3)
            self.assertEqual(len(candidate["result"]["debate_rounds"]), 2)
            graph = next(
                step
                for step in result["plan"]["steps"]
                if step["name"] == "structured_research"
            )["graph"]
            self.assertEqual(len(graph), 13)

    def test_live_workflow_requires_explicit_confirmation(self):
        workflow = DailyWorkflow(SETTINGS, ROOT)
        with self.assertRaisesRegex(ValueError, "confirm_live"):
            workflow.execute(mode="live", confirm_live=False)

    def test_call_budget_is_enforced_before_execution(self):
        settings = replace(
            SETTINGS,
            llm_max_calls_per_run=21,
            workflow_llm_candidate_limit=2,
        )
        with self.assertRaisesRegex(ValueError, "exceed"):
            DailyWorkflow(settings, ROOT).plan()

    def test_research_sell_overlay_reduces_held_position_before_risk(self):
        providers = {
            symbol: LocalCsvProvider(DATA_DIR / f"{symbol}.csv", symbol)
            for symbol in SYMBOLS
        }
        market = AlignedMarketData(providers)
        timestamp = market.timestamps[max(20, SETTINGS.warmup_bars)]
        snapshot = market.close_snapshot(timestamp)
        held_symbol = SYMBOLS[0]
        quantity = 100
        portfolio = Portfolio(
            cash=100_000.0,
            positions={held_symbol: quantity},
            peak_equity=100_000.0 + quantity * snapshot.price(held_symbol),
        )
        overlays = {
            held_symbol: {
                "action": "SELL",
                "confidence": 0.9,
                "target_weight": 0.0,
                "evidence_ids": [],
            }
        }
        plan = PortfolioPlanner(SETTINGS).plan(
            market,
            timestamp,
            portfolio,
            research_overlays=overlays,
        )
        self.assertEqual(plan.proposed_targets[held_symbol], 0.0)
        self.assertIn(
            "protective zero",
            plan.symbol_decisions[held_symbol]["research_overlay_policy"],
        )


if __name__ == "__main__":
    unittest.main()
