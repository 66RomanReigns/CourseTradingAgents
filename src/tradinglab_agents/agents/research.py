from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tradinglab_agents.agents.analysts import (
    FundamentalAnalystAgent,
    MacroAnalystAgent,
    NewsAnalystAgent,
)
from tradinglab_agents.agents.llm import StructuredLLMClient
from tradinglab_agents.agents.schemas import (
    DebateRound,
    DebateView,
    EvidenceAnalysis,
    FundamentalAnalysis,
    MacroAnalysis,
    NewsAnalysis,
    ResearchDecision,
    ResearchPipelineResult,
    RiskReview,
    TradePlan,
)
from tradinglab_agents.models import EvidencePack


RISK_PERSONAS = ("aggressive", "balanced", "conservative")


def research_graph_spec(
    *,
    debate_rounds: int = 1,
    risk_personas: tuple[str, ...] = RISK_PERSONAS,
) -> list[dict[str, Any]]:
    """Describe the original research DAG without coupling execution to LangGraph."""

    nodes: list[dict[str, Any]] = [
        {"id": "news_analyst", "tier": "quick", "depends_on": []},
        {"id": "macro_analyst", "tier": "quick", "depends_on": []},
        {"id": "fundamental_analyst", "tier": "quick", "depends_on": []},
    ]
    analyst_ids = [item["id"] for item in nodes]
    previous_round: list[str] = []
    for round_index in range(1, debate_rounds + 1):
        dependencies = analyst_ids if round_index == 1 else previous_round
        bull = f"debate_round_{round_index}.bull"
        bear = f"debate_round_{round_index}.bear"
        nodes.extend(
            [
                {"id": bull, "tier": "deep", "depends_on": list(dependencies)},
                {"id": bear, "tier": "deep", "depends_on": list(dependencies)},
            ]
        )
        previous_round = [bull, bear]
    nodes.extend(
        [
            {"id": "research_manager", "tier": "deep", "depends_on": previous_round},
            {
                "id": "preliminary_trader",
                "tier": "deep",
                "depends_on": ["research_manager"],
            },
        ]
    )
    risk_ids: list[str] = []
    for persona in risk_personas:
        node_id = f"risk_review.{persona}"
        risk_ids.append(node_id)
        nodes.append(
            {
                "id": node_id,
                "tier": "quick",
                "depends_on": ["research_manager", "preliminary_trader", *previous_round],
            }
        )
    nodes.append(
        {
            "id": "portfolio_manager",
            "tier": "deep",
            "depends_on": ["preliminary_trader", *risk_ids],
        }
    )
    return nodes


def research_calls_per_symbol(
    *,
    debate_rounds: int = 1,
    risk_personas: tuple[str, ...] = RISK_PERSONAS,
) -> int:
    """Return the exact structured-call count for one symbol.

    Three analysts + two debate calls per round + manager + preliminary trader
    + one call per risk persona + final portfolio manager.
    """

    return len(
        research_graph_spec(
            debate_rounds=debate_rounds,
            risk_personas=risk_personas,
        )
    )


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
        *,
        round_index: int,
        previous_self: DebateView | None = None,
        previous_opponent: DebateView | None = None,
        memory_context: Sequence[Mapping[str, Any]] = (),
    ) -> DebateView:
        task = f"{self.stance}_case"
        result = self.client.complete(
            task=task,
            system_prompt=(
                f"You are the {self.stance} researcher in round {round_index} of an "
                "adversarial investment debate. Build the strongest evidence-grounded "
                "case, directly answer the prior opposing case when supplied, acknowledge "
                "material uncertainty, and never invent facts or evidence IDs."
            ),
            payload={
                "symbol": pack.symbol,
                "decision_time": pack.decision_time.isoformat(),
                "round_index": round_index,
                "analyses": [item.model_dump(mode="json") for item in analyses],
                "previous_self": (
                    previous_self.model_dump(mode="json") if previous_self else None
                ),
                "previous_opponent": (
                    previous_opponent.model_dump(mode="json")
                    if previous_opponent
                    else None
                ),
                "matured_decision_memories": [dict(item) for item in memory_context],
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
        rounds: tuple[DebateRound, ...],
        *,
        current_weight: float = 0.0,
        memory_context: Sequence[Mapping[str, Any]] = (),
    ) -> ResearchDecision:
        bull = rounds[-1].bull
        bear = rounds[-1].bear
        result = self.client.complete(
            task="research_manager",
            system_prompt=(
                "You are the research manager. Resolve all debate rounds using evidence "
                "quality, convergence, disagreement and uncertainty. Prefer HOLD when the "
                "case is weak. Never exceed a 20% target weight and cite only supplied IDs."
            ),
            payload={
                "symbol": pack.symbol,
                "decision_time": pack.decision_time.isoformat(),
                "current_weight": current_weight,
                "debate_rounds": [item.model_dump(mode="json") for item in rounds],
                "bull": bull.model_dump(mode="json"),
                "bear": bear.model_dump(mode="json"),
                "matured_decision_memories": [dict(item) for item in memory_context],
            },
            response_model=ResearchDecision,
        )
        if result.target_weight > 0.20:
            raise ValueError("research manager exceeded the 20% exposure ceiling")
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
            raise ValueError("trader exceeded the 20% exposure ceiling")
        if not result.requires_human_approval:
            raise ValueError("trader must require human approval")
        _validate_evidence_ids(result.evidence_ids, pack, "trader")
        return result


@dataclass
class RiskReviewerAgent:
    client: StructuredLLMClient
    persona: str

    def review(
        self,
        pack: EvidencePack,
        manager: ResearchDecision,
        preliminary: TradePlan,
        rounds: tuple[DebateRound, ...],
        memory_context: Sequence[Mapping[str, Any]] = (),
    ) -> RiskReview:
        result = self.client.complete(
            task="risk_review",
            system_prompt=(
                f"You are the {self.persona} member of a three-person portfolio risk "
                "committee. Review the proposed paper-trading intent, identify uncertainty "
                "and concentration risk, and return APPROVE, REDUCE or VETO. You may never "
                "increase the proposed target and must cite only supplied evidence IDs."
            ),
            payload={
                "persona": self.persona,
                "symbol": pack.symbol,
                "decision_time": pack.decision_time.isoformat(),
                "manager": manager.model_dump(mode="json"),
                "preliminary_trade": preliminary.model_dump(mode="json"),
                "debate_rounds": [item.model_dump(mode="json") for item in rounds],
                "matured_decision_memories": [dict(item) for item in memory_context],
            },
            response_model=RiskReview,
        )
        if result.persona != self.persona:
            raise ValueError(
                f"risk reviewer {self.persona} returned persona={result.persona}"
            )
        if result.max_target_weight > preliminary.target_weight:
            raise ValueError("risk reviewer increased the preliminary target")
        _validate_evidence_ids(result.evidence_ids, pack, f"{self.persona} risk reviewer")
        return result


@dataclass
class PortfolioManagerAgent:
    client: StructuredLLMClient

    def finalize(
        self,
        pack: EvidencePack,
        preliminary: TradePlan,
        reviews: tuple[RiskReview, ...],
        memory_context: Sequence[Mapping[str, Any]] = (),
    ) -> TradePlan:
        result = self.client.complete(
            task="portfolio_manager",
            system_prompt=(
                "You are the final portfolio manager for a paper-only system. Reconcile the "
                "preliminary intent with all structured risk reviews. A BUY veto forces HOLD; "
                "REDUCE caps exposure; protective SELL decisions must not be weakened. Never "
                "increase target weight, never place a real order, and always require human "
                "approval for directional actions."
            ),
            payload={
                "symbol": pack.symbol,
                "decision_time": pack.decision_time.isoformat(),
                "preliminary_trade": preliminary.model_dump(mode="json"),
                "risk_reviews": [item.model_dump(mode="json") for item in reviews],
                "matured_decision_memories": [dict(item) for item in memory_context],
            },
            response_model=TradePlan,
        )
        if result.target_weight > preliminary.target_weight:
            raise ValueError("portfolio manager increased the preliminary target")
        if preliminary.action == "SELL" and result.action != "SELL":
            raise ValueError("portfolio manager weakened a protective SELL")
        if preliminary.action == "BUY" and any(
            item.verdict == "VETO" for item in reviews
        ) and result.action != "HOLD":
            raise ValueError("portfolio manager ignored a BUY veto")
        approved_cap = min(
            [preliminary.target_weight, *[item.max_target_weight for item in reviews]]
        )
        if result.target_weight > approved_cap:
            raise ValueError("portfolio manager exceeded the committee exposure cap")
        if not result.requires_human_approval:
            raise ValueError("portfolio manager must preserve human approval")
        _validate_evidence_ids(result.evidence_ids, pack, "portfolio manager")
        return result


@dataclass
class MultiAgentResearchPipeline:
    quick_client: StructuredLLMClient
    deep_client: StructuredLLMClient | None = None
    debate_rounds: int = 1
    risk_personas: tuple[str, ...] = RISK_PERSONAS
    use_langgraph: bool = True
    langchain_trace_path: str | Path = "artifacts/langchain_events.jsonl"
    langgraph_event_path: str | Path = "artifacts/langgraph_events.jsonl"
    langgraph_retry_attempts: int = 2
    langgraph_node_timeout_seconds: float = 120.0
    langgraph_cost_aware_routing: bool = False
    langgraph_hold_skip_confidence: float = 0.40
    langgraph_human_review_mode: str = "paper_queue"
    _last_graph_execution: Any = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if not 1 <= self.debate_rounds <= 3:
            raise ValueError("debate_rounds must be between 1 and 3")
        if not self.risk_personas:
            raise ValueError("risk_personas cannot be empty")
        if set(self.risk_personas).difference(RISK_PERSONAS):
            raise ValueError("unsupported risk persona")
        if len(set(self.risk_personas)) != len(self.risk_personas):
            raise ValueError("risk_personas cannot contain duplicates")
        if self.deep_client is None:
            self.deep_client = self.quick_client

    @property
    def calls_per_symbol(self) -> int:
        return research_calls_per_symbol(
            debate_rounds=self.debate_rounds,
            risk_personas=self.risk_personas,
        )

    def run(
        self,
        pack: EvidencePack,
        *,
        current_weight: float = 0.0,
        memory_context: Sequence[Mapping[str, Any]] = (),
    ) -> ResearchPipelineResult:
        return self.run_resumable(
            pack,
            current_weight=current_weight,
            memory_context=memory_context,
            node_runner=lambda _name, _model, function: function(),
        )

    def run_resumable(
        self,
        pack: EvidencePack,
        *,
        current_weight: float = 0.0,
        memory_context: Sequence[Mapping[str, Any]] = (),
        node_runner: Callable[[str, type[Any], Callable[[], Any]], Any],
        thread_id: str | None = None,
        checkpointer_path: str | Path | None = None,
        resume: bool = False,
        human_response: Mapping[str, Any] | None = None,
    ) -> ResearchPipelineResult:
        """Run the research workflow, using LangGraph by default.

        The legacy deterministic traversal remains available only for controlled
        ablations. Production, CLI and API paths use native LangGraph execution.
        """

        assert self.deep_client is not None
        if self.use_langgraph:
            from tradinglab_agents.agents.langgraph_research import (
                LangGraphResearchRuntime,
                ResearchGraphInterrupted,
            )

            resolved_thread = thread_id or (
                f"research:{pack.symbol}:{pack.decision_time.isoformat()}"
            )
            runtime = LangGraphResearchRuntime(
                quick_client=self.quick_client,
                deep_client=self.deep_client,
                debate_rounds=self.debate_rounds,
                risk_personas=self.risk_personas,
                trace_path=Path(self.langchain_trace_path),
                event_path=Path(self.langgraph_event_path),
                retry_attempts=self.langgraph_retry_attempts,
                node_timeout_seconds=self.langgraph_node_timeout_seconds,
                cost_aware_routing=self.langgraph_cost_aware_routing,
                hold_skip_confidence=self.langgraph_hold_skip_confidence,
                human_review_mode=self.langgraph_human_review_mode,
            )
            execution = runtime.run(
                pack,
                thread_id=resolved_thread,
                node_runner=node_runner,
                current_weight=current_weight,
                memory_context=memory_context,
                checkpointer_path=checkpointer_path,
                resume=resume,
                human_response=human_response,
            )
            self._last_graph_execution = execution
            if execution.interrupted or execution.result is None:
                raise ResearchGraphInterrupted(execution)
            return execution.result

        memories = tuple(dict(item) for item in memory_context)
        news = node_runner(
            "news_analyst",
            NewsAnalysis,
            lambda: NewsAnalystAgent(self.quick_client).analyze(pack),
        )
        macro = node_runner(
            "macro_analyst",
            MacroAnalysis,
            lambda: MacroAnalystAgent(self.quick_client).analyze(pack),
        )
        fundamental = node_runner(
            "fundamental_analyst",
            FundamentalAnalysis,
            lambda: FundamentalAnalystAgent(self.quick_client).analyze(pack),
        )
        analyses: tuple[EvidenceAnalysis, ...] = (news, macro, fundamental)

        rounds: list[DebateRound] = []
        previous_bull: DebateView | None = None
        previous_bear: DebateView | None = None
        for round_index in range(1, self.debate_rounds + 1):
            bull = node_runner(
                f"debate_round_{round_index}.bull",
                DebateView,
                lambda round_index=round_index, previous_bull=previous_bull, previous_bear=previous_bear: BullResearcherAgent(
                    self.deep_client
                ).analyze(
                    pack,
                    analyses,
                    round_index=round_index,
                    previous_self=previous_bull,
                    previous_opponent=previous_bear,
                    memory_context=memories,
                ),
            )
            bear = node_runner(
                f"debate_round_{round_index}.bear",
                DebateView,
                lambda round_index=round_index, previous_bull=previous_bull, previous_bear=previous_bear: BearResearcherAgent(
                    self.deep_client
                ).analyze(
                    pack,
                    analyses,
                    round_index=round_index,
                    previous_self=previous_bear,
                    previous_opponent=previous_bull,
                    memory_context=memories,
                ),
            )
            debate_round = DebateRound(
                round_index=round_index,
                bull=bull,
                bear=bear,
            )
            rounds.append(debate_round)
            previous_bull, previous_bear = bull, bear

        debate_rounds = tuple(rounds)
        manager = node_runner(
            "research_manager",
            ResearchDecision,
            lambda: ResearchManagerAgent(self.deep_client).decide(
                pack,
                debate_rounds,
                current_weight=current_weight,
                memory_context=memories,
            ),
        )
        preliminary = node_runner(
            "preliminary_trader",
            TradePlan,
            lambda: TraderAgent(self.deep_client).plan(pack, manager),
        )
        reviews = tuple(
            node_runner(
                f"risk_review.{persona}",
                RiskReview,
                lambda persona=persona: RiskReviewerAgent(
                    self.quick_client,
                    persona,
                ).review(
                    pack,
                    manager,
                    preliminary,
                    debate_rounds,
                    memory_context=memories,
                ),
            )
            for persona in self.risk_personas
        )
        final = node_runner(
            "portfolio_manager",
            TradePlan,
            lambda: PortfolioManagerAgent(self.deep_client).finalize(
                pack,
                preliminary,
                reviews,
                memory_context=memories,
            ),
        )
        return ResearchPipelineResult(
            symbol=pack.symbol,
            decision_time=pack.decision_time.isoformat(),
            news=news,
            macro=macro,
            fundamental=fundamental,
            debate_rounds=debate_rounds,
            bull=debate_rounds[-1].bull,
            bear=debate_rounds[-1].bear,
            manager=manager,
            preliminary_trader=preliminary,
            risk_reviews=reviews,
            trader=final,
        )
