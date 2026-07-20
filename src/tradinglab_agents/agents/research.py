from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from tradinglab_agents.agents.analysts import (
    FundamentalAnalystAgent,
    MacroAnalystAgent,
    NewsAnalystAgent,
)
from tradinglab_agents.agents.llm import StructuredLLMClient
from tradinglab_agents.agents.schemas import (
    DebateView,
    EvidenceAnalysis,
    FundamentalAnalysis,
    MacroAnalysis,
    NewsAnalysis,
    ResearchDecision,
    ResearchPipelineResult,
    TradePlan,
)
from tradinglab_agents.models import EvidencePack


def _validate_evidence_ids(
    evidence_ids: tuple[str, ...],
    pack: EvidencePack,
    role: str,
) -> None:
    unknown = set(evidence_ids).difference(pack.ids)
    if unknown:
        raise ValueError(f"{role} referenced unavailable evidence: {sorted(unknown)}")


@dataclass
class DebateAgent:
    client: StructuredLLMClient
    stance: str

    def analyze(
        self,
        pack: EvidencePack,
        analyses: tuple[EvidenceAnalysis, ...],
    ) -> DebateView:
        task = f"{self.stance}_case"
        result = self.client.complete(
            task=task,
            system_prompt=(
                f"You are the {self.stance} researcher in an adversarial investment debate. "
                "Build the strongest evidence-grounded case, explicitly acknowledge contrary "
                "evidence, and never invent facts or evidence IDs."
            ),
            payload={
                "symbol": pack.symbol,
                "decision_time": pack.decision_time.isoformat(),
                "analyses": [item.model_dump(mode="json") for item in analyses],
            },
            response_model=DebateView,
        )
        if result.stance != self.stance:
            raise ValueError(
                f"{self.stance} debate agent returned stance={result.stance}"
            )
        _validate_evidence_ids(result.evidence_ids, pack, f"{self.stance} researcher")
        return result


class BullResearcherAgent(DebateAgent):
    def __init__(self, client: StructuredLLMClient):
        super().__init__(client=client, stance="bull")


class BearResearcherAgent(DebateAgent):
    def __init__(self, client: StructuredLLMClient):
        super().__init__(client=client, stance="bear")


@dataclass
class ResearchManagerAgent:
    client: StructuredLLMClient

    def decide(
        self,
        pack: EvidencePack,
        bull: DebateView,
        bear: DebateView,
        *,
        current_weight: float = 0.0,
    ) -> ResearchDecision:
        result = self.client.complete(
            task="research_manager",
            system_prompt=(
                "You are the research manager. Resolve the bull/bear debate using evidence "
                "quality, disagreement and uncertainty. Prefer HOLD when the case is weak. "
                "Never exceed a 20% target weight and cite only supplied evidence IDs."
            ),
            payload={
                "symbol": pack.symbol,
                "decision_time": pack.decision_time.isoformat(),
                "current_weight": current_weight,
                "bull": bull.model_dump(mode="json"),
                "bear": bear.model_dump(mode="json"),
            },
            response_model=ResearchDecision,
        )
        if result.target_weight > 0.20:
            raise ValueError("research manager exceeded the 20% P1 exposure ceiling")
        _validate_evidence_ids(result.evidence_ids, pack, "research manager")
        return result


@dataclass
class TraderAgent:
    client: StructuredLLMClient

    def plan(
        self,
        pack: EvidencePack,
        decision: ResearchDecision,
    ) -> TradePlan:
        result = self.client.complete(
            task="trader_plan",
            system_prompt=(
                "You are a paper-trading execution planner. Convert validated research into "
                "a next-open intent. Do not bypass risk controls, do not place real orders, "
                "and require human approval for every directional order."
            ),
            payload={
                "symbol": pack.symbol,
                "decision_time": pack.decision_time.isoformat(),
                "research_decision": decision.model_dump(mode="json"),
                "execution_policy": {
                    "market": "US equities and ETFs",
                    "long_only": True,
                    "execute_at": "next_open",
                    "max_target_weight": 0.20,
                    "real_money": False,
                },
            },
            response_model=TradePlan,
        )
        if result.target_weight > 0.20:
            raise ValueError("trader exceeded the 20% P1 exposure ceiling")
        if not result.requires_human_approval:
            raise ValueError("P1 trader must require human approval")
        _validate_evidence_ids(result.evidence_ids, pack, "trader")
        return result


@dataclass
class MultiAgentResearchPipeline:
    client: StructuredLLMClient

    def run(
        self,
        pack: EvidencePack,
        *,
        current_weight: float = 0.0,
    ) -> ResearchPipelineResult:
        return self.run_resumable(
            pack,
            current_weight=current_weight,
            node_runner=lambda _name, _model, function: function(),
        )

    def run_resumable(
        self,
        pack: EvidencePack,
        *,
        current_weight: float = 0.0,
        node_runner: Callable[[str, type[Any], Callable[[], Any]], Any],
    ) -> ResearchPipelineResult:
        """Run the seven research roles through an external checkpoint runner."""

        news = node_runner(
            "news_analyst",
            NewsAnalysis,
            lambda: NewsAnalystAgent(self.client).analyze(pack),
        )
        macro = node_runner(
            "macro_analyst",
            MacroAnalysis,
            lambda: MacroAnalystAgent(self.client).analyze(pack),
        )
        fundamental = node_runner(
            "fundamental_analyst",
            FundamentalAnalysis,
            lambda: FundamentalAnalystAgent(self.client).analyze(pack),
        )
        analyses: tuple[EvidenceAnalysis, ...] = (news, macro, fundamental)
        bull = node_runner(
            "bull_researcher",
            DebateView,
            lambda: BullResearcherAgent(self.client).analyze(pack, analyses),
        )
        bear = node_runner(
            "bear_researcher",
            DebateView,
            lambda: BearResearcherAgent(self.client).analyze(pack, analyses),
        )
        manager = node_runner(
            "research_manager",
            ResearchDecision,
            lambda: ResearchManagerAgent(self.client).decide(
                pack,
                bull,
                bear,
                current_weight=current_weight,
            ),
        )
        trader = node_runner(
            "trader",
            TradePlan,
            lambda: TraderAgent(self.client).plan(pack, manager),
        )
        return ResearchPipelineResult(
            symbol=pack.symbol,
            decision_time=pack.decision_time.isoformat(),
            news=news,
            macro=macro,
            fundamental=fundamental,
            bull=bull,
            bear=bear,
            manager=manager,
            trader=trader,
        )
