import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from tradinglab_agents.agents.context import ContextAnalystAgent
from tradinglab_agents.agents.llm import (
    CachedLLMClient,
    MockLLM,
    OpenAICompatibleClient,
)
from tradinglab_agents.agents.research import (
    MultiAgentResearchPipeline,
    PortfolioManagerAgent,
    research_graph_spec,
)
from tradinglab_agents.agents.schemas import RiskReview, TradePlan
from tradinglab_agents.broker.paper import PaperBroker
from tradinglab_agents.config import BacktestSettings, load_settings
from tradinglab_agents.models import (
    Action,
    Evidence,
    EvidencePack,
    Portfolio,
    TradeIntent,
)
from tradinglab_agents.risk.governor import RiskGovernor, RiskState


ROOT = Path(__file__).resolve().parents[1]


class RiskCircuitBreakerTest(unittest.TestCase):
    def setUp(self):
        self.intent = TradeIntent(
            symbol="X",
            action=Action.BUY,
            confidence=0.9,
            target_weight=0.2,
            rationale="test",
            evidence_ids=(),
        )

    def test_drawdown_forces_liquidation_then_halts(self):
        portfolio = Portfolio(cash=0.0, positions={"X": 100}, peak_equity=20_000.0)
        governor = RiskGovernor(max_drawdown=0.15)
        decision = governor.review(self.intent, portfolio, {"X": 100.0})

        self.assertFalse(decision.approved)
        self.assertTrue(decision.force_execution)
        self.assertEqual(decision.target_weight, 0.0)
        self.assertEqual(decision.state, RiskState.LIQUIDATING.value)

        fill = PaperBroker(commission_bps=0, slippage_bps=0).rebalance(
            portfolio,
            "X",
            decision.target_weight,
            100.0,
            datetime(2025, 1, 2, 9, 30),
        )
        self.assertIsNotNone(fill)
        self.assertEqual(fill.quantity, -100)
        self.assertEqual(portfolio.positions["X"], 0)

        halted = governor.review(self.intent, portfolio, {"X": 100.0})
        self.assertFalse(halted.approved)
        self.assertFalse(halted.force_execution)
        self.assertEqual(halted.state, RiskState.HALTED.value)
        self.assertEqual(halted.target_weight, 0.0)


class StrictConfigurationTest(unittest.TestCase):
    def test_default_config_is_strict_and_loadable(self):
        settings = load_settings(ROOT / "config/default.yaml")
        self.assertEqual(settings.llm_execution_mode, "dry_run")
        self.assertEqual(settings.llm_provider, "zhipu")
        self.assertEqual(settings.llm_model, "glm-4.7-flash")
        self.assertEqual(settings.max_position_weight, 0.20)

    def test_unknown_or_unimplemented_config_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.yaml"
            path.write_text(
                "features:\n  rsi_window: 14\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "features"):
                load_settings(path)

            path.write_text(
                "risk:\n  max_position_weight: 0.2\n  max_daily_turnover: 0.25\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "risk.max_daily_turnover"):
                load_settings(path)


class RecordingReasoner:
    def __init__(self):
        self.calls = []

    @staticmethod
    def _result(kind, items):
        return {
            "analysis_type": kind,
            "score": 0.1,
            "confidence": 0.6,
            "rationale": f"{kind} specialized",
            "evidence_ids": [item["evidence_id"] for item in items],
        }

    def analyze_news(self, items):
        self.calls.append("news")
        return self._result("news", items)

    def analyze_macro(self, items):
        self.calls.append("macro")
        return self._result("macro", items)

    def analyze_fundamentals(self, items):
        self.calls.append("fundamental")
        return self._result("fundamental", items)


class PayloadRecordingClient:
    def __init__(self):
        self.delegate = MockLLM()
        self.calls = []

    @property
    def identity(self):
        return "recording:mock"

    def complete(self, *, task, system_prompt, payload, response_model):
        self.calls.append((task, payload))
        return self.delegate.complete(
            task=task,
            system_prompt=system_prompt,
            payload=payload,
            response_model=response_model,
        )


class StructuredAgentPipelineTest(unittest.TestCase):
    @staticmethod
    def _pack():
        now = datetime(2025, 1, 2, 16)
        pack = EvidencePack(symbol="X", decision_time=now)
        for evidence_id, kind, value, detail in (
            ("news.1", "news", "strong growth partnership", "positive headline"),
            ("macro.1", "macro", "cooling inflation and rate cut", "macro release"),
            ("fund.1", "fundamental", "record profit growth", "filed earnings"),
        ):
            pack.add(
                Evidence(
                    evidence_id=evidence_id,
                    kind=kind,
                    timestamp=now,
                    available_at=now,
                    value=value,
                    source="fixture",
                    detail=detail,
                )
            )
        return pack

    def test_context_uses_separate_reasoning_methods(self):
        reasoner = RecordingReasoner()
        opinion = ContextAnalystAgent(reasoner).analyze(self._pack())
        self.assertEqual(reasoner.calls, ["news", "macro", "fundamental"])
        self.assertTrue(set(opinion.evidence_ids).issubset(self._pack().ids))

    def test_offline_pipeline_is_schema_validated_cached_and_auditable(self):
        pack = self._pack()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = CachedLLMClient(
                delegate=MockLLM(),
                cache_dir=root / "cache",
                log_path=root / "calls.jsonl",
            )
            pipeline = MultiAgentResearchPipeline(client)
            first = pipeline.run(pack)
            second = pipeline.run(pack)

            self.assertEqual(first, second)
            self.assertTrue(first.trader.requires_human_approval)
            self.assertEqual(len(first.risk_reviews), 3)
            self.assertEqual(len(first.debate_rounds), 1)
            self.assertTrue(first.preliminary_trader.requires_human_approval)
            self.assertLessEqual(first.trader.target_weight, 0.20)
            self.assertTrue(set(first.trader.evidence_ids).issubset(pack.ids))
            self.assertEqual(len(list((root / "cache").glob("*.json"))), 11)

            records = [
                json.loads(line)
                for line in (root / "calls.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(records), 22)
            self.assertEqual(sum(bool(row["cache_hit"]) for row in records), 11)
            self.assertTrue(all("api_key" not in json.dumps(row) for row in records))

    def test_risk_committee_can_veto_a_preliminary_buy(self):
        pack = self._pack()
        preliminary = TradePlan(
            action="BUY",
            confidence=0.55,
            target_weight=0.15,
            order_type="MARKET_NEXT_OPEN",
            rationale="preliminary buy",
            evidence_ids=("news.1",),
            requires_human_approval=True,
        )
        reviews = (
            RiskReview(
                persona="aggressive",
                verdict="APPROVE",
                confidence=0.7,
                max_target_weight=0.15,
                rationale="approve",
                evidence_ids=("news.1",),
            ),
            RiskReview(
                persona="balanced",
                verdict="REDUCE",
                confidence=0.7,
                max_target_weight=0.10,
                rationale="reduce",
                evidence_ids=("news.1",),
            ),
            RiskReview(
                persona="conservative",
                verdict="VETO",
                confidence=0.8,
                max_target_weight=0.0,
                rationale="veto",
                evidence_ids=("news.1",),
            ),
        )
        final = PortfolioManagerAgent(MockLLM()).finalize(
            pack,
            preliminary,
            reviews,
        )
        self.assertEqual(final.action, "HOLD")
        self.assertEqual(final.target_weight, 0.0)
        self.assertEqual(final.order_type, "NO_ORDER")

    def test_matured_memory_is_supplied_to_decision_roles(self):
        client = PayloadRecordingClient()
        memory = {
            "memory_id": "memory-1",
            "action": "BUY",
            "alpha_return": -0.04,
            "failure_type": "UNDERPERFORMED_BENCHMARK",
        }
        MultiAgentResearchPipeline(client).run(
            self._pack(),
            memory_context=[memory],
        )
        decision_tasks = {
            "bull_case",
            "bear_case",
            "research_manager",
            "risk_review",
            "portfolio_manager",
        }
        supplied = [
            payload["matured_decision_memories"]
            for task, payload in client.calls
            if task in decision_tasks
        ]
        self.assertTrue(supplied)
        self.assertTrue(all(rows == [memory] for rows in supplied))

    def test_configurable_graph_has_tiered_dependencies(self):
        graph = research_graph_spec(debate_rounds=2)
        self.assertEqual(len(graph), 13)
        by_id = {item["id"]: item for item in graph}
        self.assertEqual(by_id["news_analyst"]["tier"], "quick")
        self.assertEqual(by_id["debate_round_2.bull"]["tier"], "deep")
        self.assertIn(
            "risk_review.conservative",
            by_id["portfolio_manager"]["depends_on"],
        )

    def test_invalid_structured_output_is_rejected(self):
        with self.assertRaises(ValidationError):
            TradePlan.model_validate(
                {
                    "action": "HOLD",
                    "confidence": 0.5,
                    "target_weight": 0.0,
                    "order_type": "MARKET_NEXT_OPEN",
                    "rationale": "invalid order contract",
                    "evidence_ids": [],
                    "requires_human_approval": True,
                    "unexpected": "forbidden",
                }
            )

    def test_remote_client_requires_key_before_network_use(self):
        with self.assertRaises(ValueError):
            OpenAICompatibleClient(model="deepseek-chat", api_key="")

    def test_settings_reject_unknown_provider(self):
        with self.assertRaises(ValueError):
            replace(BacktestSettings(), llm_provider="unknown")


if __name__ == "__main__":
    unittest.main()
