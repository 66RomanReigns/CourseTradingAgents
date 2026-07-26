from __future__ import annotations

import json
import operator
import threading
from datetime import datetime, timezone
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal, TypedDict, TypeVar

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, RetryPolicy, interrupt
from pydantic import BaseModel

from tradinglab_agents.agents.langchain_runtime import LangChainStructuredClient
from tradinglab_agents.agents.research import (
    BearResearcherAgent,
    BullResearcherAgent,
    PortfolioManagerAgent,
    ResearchManagerAgent,
    RiskReviewerAgent,
    TraderAgent,
)
from tradinglab_agents.agents.analysts import (
    FundamentalAnalystAgent,
    MacroAnalystAgent,
    NewsAnalystAgent,
)
from tradinglab_agents.agents.schemas import (
    DebateRound,
    DebateView,
    FundamentalAnalysis,
    MacroAnalysis,
    NewsAnalysis,
    ResearchDecision,
    ResearchPipelineResult,
    RiskReview,
    TradePlan,
)
from tradinglab_agents.models import EvidencePack
from tradinglab_agents.storage.langgraph_checkpoints import (
    open_sqlite_checkpoint_saver,
)
from tradinglab_agents.workflows.state import WorkflowExecutionError


TModel = TypeVar("TModel", bound=BaseModel)
_GRAPH_EVENT_LOCK = threading.Lock()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_graph_event(path: Path | None, payload: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"timestamp_utc": _utc_now(), **payload}
    with _GRAPH_EVENT_LOCK:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
NodeRunner = Callable[[str, type[Any], Callable[[], Any]], Any]
HumanReviewMode = Literal["paper_queue", "interrupt_directional"]


class ResearchGraphState(TypedDict, total=False):
    symbol: str
    decision_time: str
    current_weight: float
    memory_context: list[dict[str, Any]]
    debate_round: int
    debate_rounds: list[dict[str, Any]]
    news: dict[str, Any]
    macro: dict[str, Any]
    fundamental: dict[str, Any]
    bull: dict[str, Any]
    bear: dict[str, Any]
    manager: dict[str, Any]
    preliminary_trader: dict[str, Any]
    risk_reviews: Annotated[list[dict[str, Any]], operator.add]
    trader: dict[str, Any]
    human_review: dict[str, Any]
    execution_path: Annotated[list[str], operator.add]


@dataclass(frozen=True)
class ResearchGraphExecution:
    result: ResearchPipelineResult | None
    thread_id: str
    interrupted: bool
    interrupt_payloads: tuple[dict[str, Any], ...]
    latest_state: dict[str, Any]
    checkpoint_count: int
    execution_path: tuple[str, ...]
    event_count: int


class ResearchGraphInterrupted(RuntimeError):
    def __init__(self, execution: ResearchGraphExecution):
        self.execution = execution
        super().__init__(f"research graph {execution.thread_id} is waiting for human review")


def _retryable(exc: Exception) -> bool:
    root: Exception = exc.cause if isinstance(exc, WorkflowExecutionError) else exc
    if isinstance(root, (TimeoutError, ConnectionError)):
        return True
    name = type(root).__name__.lower()
    return any(
        marker in name
        for marker in (
            "timeout",
            "ratelimit",
            "apiconnection",
            "serviceunavailable",
            "internalserver",
        )
    )


def inspect_research_thread(
    database_path: str | Path,
    thread_id: str,
) -> dict[str, Any]:
    """Return a bounded, secret-free snapshot for one LangGraph thread."""

    path = Path(database_path)
    if not path.is_file():
        return {
            "thread_id": thread_id,
            "exists": False,
            "database": str(path),
        }
    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    with open_sqlite_checkpoint_saver(path) as saver:
        latest = saver.get_tuple(config)
        history = list(saver.list(config, limit=500))
    if latest is None:
        return {
            "thread_id": thread_id,
            "exists": False,
            "database": str(path),
        }
    values = dict(latest.checkpoint.get("channel_values", {}))
    trader = values.get("trader")
    guarded = values.get("guarded_allocation")
    parent_candidates = values.get("candidates")
    parent_results = values.get("candidate_results")
    parent_portfolio = values.get("portfolio_summary")
    core_validation = values.get("data_validation")
    core_research = values.get("research")
    core_overlays = values.get("research_overlays")
    core_preparation = values.get("decision_preparation")
    deterministic_plan = values.get("portfolio_plan")
    deterministic_plan_sha256 = values.get("plan_sha256")
    deterministic_input_sha256 = values.get("input_sha256")
    deterministic_symbol_results = values.get("symbol_results")
    provider_requests = values.get("requests")
    provider_routes = values.get("route_results")
    provider_persisted = values.get("persisted_results")
    provider_quality = values.get("quality_summary")
    portfolio_allocations = (
        list(guarded.get("allocations", []))
        if isinstance(guarded, dict)
        else []
    )
    if isinstance(provider_routes, list) and isinstance(provider_quality, dict):
        graph_type = "data_provider"
    elif isinstance(deterministic_plan, dict) and deterministic_plan_sha256:
        graph_type = "deterministic_decision"
    elif isinstance(core_validation, dict) and isinstance(core_preparation, dict):
        graph_type = "workflow_core"
    elif isinstance(parent_candidates, list) and isinstance(parent_results, list):
        graph_type = "research_parent"
    elif isinstance(guarded, dict):
        graph_type = "portfolio_supervisor"
    else:
        graph_type = "symbol_research"
    return {
        "thread_id": thread_id,
        "exists": True,
        "database": str(path),
        "checkpoint_count": len(history),
        "checkpoint_id": latest.config.get("configurable", {}).get("checkpoint_id"),
        "metadata": {
            key: latest.metadata.get(key)
            for key in ("source", "step", "writes")
            if key in latest.metadata
        },
        "pending_write_count": len(latest.pending_writes or ()),
        "state_keys": sorted(values),
        "graph_type": graph_type,
        "symbol": values.get("symbol"),
        "decision_time": values.get("decision_time"),
        "debate_round": values.get("debate_round"),
        "debate_round_count": len(values.get("debate_rounds", [])),
        "risk_review_count": len(values.get("risk_reviews", [])),
        "final_action": trader.get("action") if isinstance(trader, dict) else None,
        "portfolio_gross_target": (
            guarded.get("gross_target") if isinstance(guarded, dict) else None
        ),
        "parent_candidate_count": (
            len(parent_candidates) if isinstance(parent_candidates, list) else None
        ),
        "parent_completed_symbols": sorted(
            str(item.get("symbol", "")).upper()
            for item in (parent_results if isinstance(parent_results, list) else [])
            if isinstance(item, dict) and item.get("symbol")
        ),
        "parent_portfolio_status": (
            parent_portfolio.get("status")
            if isinstance(parent_portfolio, dict)
            else None
        ),
        "core_data_status": (
            core_validation.get("status")
            if isinstance(core_validation, dict)
            else None
        ),
        "core_research_status": (
            core_research.get("status")
            if isinstance(core_research, dict)
            else None
        ),
        "core_overlay_count": (
            len(core_overlays) if isinstance(core_overlays, dict) else None
        ),
        "core_decision_status": (
            core_preparation.get("status")
            if isinstance(core_preparation, dict)
            else None
        ),
        "core_safe_for_paper_input": (
            core_preparation.get("safe_for_paper_input")
            if isinstance(core_preparation, dict)
            else None
        ),
        "core_overlay_payload_sha256": (
            core_preparation.get("overlay_payload_sha256")
            if isinstance(core_preparation, dict)
            else None
        ),
        "provider_input_sha256": (
            deterministic_input_sha256
            if graph_type == "data_provider"
            else None
        ),
        "provider_request_count": (
            len(provider_requests) if isinstance(provider_requests, list) else None
        ),
        "provider_route_count": (
            len(provider_routes) if isinstance(provider_routes, list) else None
        ),
        "provider_persisted_count": (
            len(provider_persisted) if isinstance(provider_persisted, list) else None
        ),
        "provider_quality_status": (
            provider_quality.get("status")
            if isinstance(provider_quality, dict)
            else None
        ),
        "provider_allow_position_increase": (
            provider_quality.get("allow_position_increase")
            if isinstance(provider_quality, dict)
            else None
        ),
        "provider_minimum_score": (
            provider_quality.get("minimum_score")
            if isinstance(provider_quality, dict)
            else None
        ),
        "provider_degraded_resources": (
            list(provider_quality.get("degraded_resources", []))
            if isinstance(provider_quality, dict)
            else None
        ),
        "provider_blocked_resources": (
            list(provider_quality.get("blocked_resources", []))
            if isinstance(provider_quality, dict)
            else None
        ),
        "decision_input_sha256": (
            deterministic_input_sha256
            if graph_type == "deterministic_decision"
            else None
        ),
        "decision_plan_sha256": deterministic_plan_sha256,
        "decision_symbol_count": (
            len(deterministic_symbol_results)
            if isinstance(deterministic_symbol_results, list)
            else None
        ),
        "decision_risk_state": (
            dict(deterministic_plan.get("risk_decision", {})).get("state")
            if isinstance(deterministic_plan, dict)
            else None
        ),
        "decision_force_execution": (
            dict(deterministic_plan.get("risk_decision", {})).get(
                "force_execution"
            )
            if isinstance(deterministic_plan, dict)
            else None
        ),
        "decision_target_weights": (
            {
                str(symbol).upper(): float(weight)
                for symbol, weight in dict(
                    deterministic_plan.get("target_weights", {})
                ).items()
            }
            if isinstance(deterministic_plan, dict)
            else None
        ),
        "portfolio_allocations": [
            {
                "symbol": item.get("symbol"),
                "action": item.get("action"),
                "target_weight": item.get("target_weight"),
            }
            for item in portfolio_allocations
            if isinstance(item, dict)
        ],
        "human_review": values.get("human_review", {}),
        "execution_path": list(values.get("execution_path", [])),
    }


def _hold_plan(
    manager: ResearchDecision,
    *,
    rationale: str,
) -> TradePlan:
    return TradePlan(
        action="HOLD",
        confidence=manager.confidence,
        target_weight=min(manager.target_weight, 0.20),
        order_type="NO_ORDER",
        rationale=rationale,
        evidence_ids=manager.evidence_ids,
        requires_human_approval=True,
    )


@dataclass
class LangGraphResearchRuntime:
    quick_client: Any
    deep_client: Any
    debate_rounds: int
    risk_personas: tuple[str, ...]
    trace_path: Path
    event_path: Path | None = None
    retry_attempts: int = 2
    node_timeout_seconds: float = 120.0
    cost_aware_routing: bool = False
    hold_skip_confidence: float = 0.40
    human_review_mode: HumanReviewMode = "paper_queue"

    def __post_init__(self) -> None:
        if not 1 <= self.debate_rounds <= 3:
            raise ValueError("debate_rounds must be between 1 and 3")
        if self.retry_attempts < 1 or self.retry_attempts > 5:
            raise ValueError("retry_attempts must be in [1, 5]")
        if self.node_timeout_seconds <= 0:
            raise ValueError("node_timeout_seconds must be positive")
        if not 0.0 <= self.hold_skip_confidence <= 1.0:
            raise ValueError("hold_skip_confidence must be in [0, 1]")
        if self.human_review_mode not in {"paper_queue", "interrupt_directional"}:
            raise ValueError("unsupported human_review_mode")

    def _clients(
        self,
        *,
        symbol: str,
        thread_id: str,
    ) -> tuple[LangChainStructuredClient, LangChainStructuredClient]:
        return (
            LangChainStructuredClient(
                self.quick_client,
                self.trace_path,
                tier="quick",
                symbol=symbol,
                thread_id=thread_id,
            ),
            LangChainStructuredClient(
                self.deep_client,
                self.trace_path,
                tier="deep",
                symbol=symbol,
                thread_id=thread_id,
            ),
        )

    @staticmethod
    def _model(
        state: ResearchGraphState,
        key: str,
        model: type[TModel],
    ) -> TModel:
        return model.model_validate(state[key])

    @staticmethod
    def _rounds(state: ResearchGraphState) -> tuple[DebateRound, ...]:
        return tuple(DebateRound.model_validate(item) for item in state["debate_rounds"])

    def build(
        self,
        pack: EvidencePack,
        *,
        thread_id: str,
        node_runner: NodeRunner,
        checkpointer: BaseCheckpointSaver[Any] | None = None,
    ) -> Any:
        quick, deep = self._clients(symbol=pack.symbol, thread_id=thread_id)
        retry = RetryPolicy(max_attempts=self.retry_attempts, retry_on=_retryable)
        builder = StateGraph(ResearchGraphState)

        def run_model(
            name: str,
            model: type[TModel],
            function: Callable[[], TModel],
        ) -> dict[str, Any]:
            result = node_runner(name, model, function)
            return result.model_dump(mode="json")

        def news_node(state: ResearchGraphState) -> ResearchGraphState:
            del state
            return {
                "news": run_model(
                    "news_analyst",
                    NewsAnalysis,
                    lambda: NewsAnalystAgent(quick).analyze(pack),
                ),
                "execution_path": ["news_analyst"],
            }

        def macro_node(state: ResearchGraphState) -> ResearchGraphState:
            del state
            return {
                "macro": run_model(
                    "macro_analyst",
                    MacroAnalysis,
                    lambda: MacroAnalystAgent(quick).analyze(pack),
                ),
                "execution_path": ["macro_analyst"],
            }

        def fundamental_node(state: ResearchGraphState) -> ResearchGraphState:
            del state
            return {
                "fundamental": run_model(
                    "fundamental_analyst",
                    FundamentalAnalysis,
                    lambda: FundamentalAnalystAgent(quick).analyze(pack),
                ),
                "execution_path": ["fundamental_analyst"],
            }

        def debate_dispatch(state: ResearchGraphState) -> ResearchGraphState:
            return {
                "debate_round": len(state.get("debate_rounds", [])) + 1,
                "execution_path": ["debate_dispatch"],
            }

        def bull_node(state: ResearchGraphState) -> ResearchGraphState:
            analyses = (
                self._model(state, "news", NewsAnalysis),
                self._model(state, "macro", MacroAnalysis),
                self._model(state, "fundamental", FundamentalAnalysis),
            )
            prior = self._rounds(state)
            round_index = int(state["debate_round"])
            previous_bull = prior[-1].bull if prior else None
            previous_bear = prior[-1].bear if prior else None
            name = f"debate_round_{round_index}.bull"
            return {
                "bull": run_model(
                    name,
                    DebateView,
                    lambda: BullResearcherAgent(deep).analyze(
                        pack,
                        analyses,
                        round_index=round_index,
                        previous_self=previous_bull,
                        previous_opponent=previous_bear,
                        memory_context=state.get("memory_context", []),
                    ),
                ),
                "execution_path": [name],
            }

        def bear_node(state: ResearchGraphState) -> ResearchGraphState:
            analyses = (
                self._model(state, "news", NewsAnalysis),
                self._model(state, "macro", MacroAnalysis),
                self._model(state, "fundamental", FundamentalAnalysis),
            )
            prior = self._rounds(state)
            round_index = int(state["debate_round"])
            previous_bull = prior[-1].bull if prior else None
            previous_bear = prior[-1].bear if prior else None
            name = f"debate_round_{round_index}.bear"
            return {
                "bear": run_model(
                    name,
                    DebateView,
                    lambda: BearResearcherAgent(deep).analyze(
                        pack,
                        analyses,
                        round_index=round_index,
                        previous_self=previous_bear,
                        previous_opponent=previous_bull,
                        memory_context=state.get("memory_context", []),
                    ),
                ),
                "execution_path": [name],
            }

        def debate_join(state: ResearchGraphState) -> ResearchGraphState:
            current = DebateRound(
                round_index=int(state["debate_round"]),
                bull=self._model(state, "bull", DebateView),
                bear=self._model(state, "bear", DebateView),
            )
            return {
                "debate_rounds": [
                    *state.get("debate_rounds", []),
                    current.model_dump(mode="json"),
                ],
                "execution_path": [f"debate_round_{current.round_index}.join"],
            }

        def debate_route(state: ResearchGraphState) -> str:
            return (
                "continue"
                if len(state.get("debate_rounds", [])) < self.debate_rounds
                else "manager"
            )

        def manager_node(state: ResearchGraphState) -> ResearchGraphState:
            return {
                "manager": run_model(
                    "research_manager",
                    ResearchDecision,
                    lambda: ResearchManagerAgent(deep).decide(
                        pack,
                        self._rounds(state),
                        current_weight=float(state.get("current_weight", 0.0)),
                        memory_context=state.get("memory_context", []),
                    ),
                ),
                "execution_path": ["research_manager"],
            }

        def manager_route(state: ResearchGraphState) -> str:
            manager = self._model(state, "manager", ResearchDecision)
            if (
                self.cost_aware_routing
                and manager.action == "HOLD"
                and manager.confidence < self.hold_skip_confidence
            ):
                return "low_confidence_hold"
            return "full_committee"

        def deterministic_hold_node(state: ResearchGraphState) -> ResearchGraphState:
            manager = self._model(state, "manager", ResearchDecision)
            plan = _hold_plan(
                manager,
                rationale=(
                    "LangGraph cost-aware route finalized a low-confidence HOLD before "
                    "execution and committee model calls"
                ),
            )
            payload = plan.model_dump(mode="json")
            return {
                "preliminary_trader": payload,
                "risk_reviews": [],
                "trader": payload,
                "execution_path": ["cost_aware_hold"],
            }

        def preliminary_node(state: ResearchGraphState) -> ResearchGraphState:
            manager = self._model(state, "manager", ResearchDecision)
            return {
                "preliminary_trader": run_model(
                    "preliminary_trader",
                    TradePlan,
                    lambda: TraderAgent(deep).plan(pack, manager),
                ),
                "execution_path": ["preliminary_trader"],
            }

        def risk_node(persona: str) -> Callable[[ResearchGraphState], ResearchGraphState]:
            def run(state: ResearchGraphState) -> ResearchGraphState:
                name = f"risk_review.{persona}"
                review = run_model(
                    name,
                    RiskReview,
                    lambda: RiskReviewerAgent(quick, persona).review(
                        pack,
                        self._model(state, "manager", ResearchDecision),
                        self._model(state, "preliminary_trader", TradePlan),
                        self._rounds(state),
                        memory_context=state.get("memory_context", []),
                    ),
                )
                return {
                    "risk_reviews": [review],
                    "execution_path": [name],
                }

            return run

        def portfolio_manager_node(state: ResearchGraphState) -> ResearchGraphState:
            preliminary = self._model(state, "preliminary_trader", TradePlan)
            reviews = tuple(
                RiskReview.model_validate(item)
                for item in state.get("risk_reviews", [])
            )
            return {
                "trader": run_model(
                    "portfolio_manager",
                    TradePlan,
                    lambda: PortfolioManagerAgent(deep).finalize(
                        pack,
                        preliminary,
                        reviews,
                        memory_context=state.get("memory_context", []),
                    ),
                ),
                "execution_path": ["portfolio_manager"],
            }

        def human_review_node(state: ResearchGraphState) -> ResearchGraphState:
            plan = self._model(state, "trader", TradePlan)
            if plan.action == "HOLD":
                review = {"status": "NOT_REQUIRED", "decision": "hold"}
            elif self.human_review_mode == "paper_queue":
                review = {
                    "status": "DEFERRED_TO_PAPER_QUEUE",
                    "decision": None,
                    "requires_human_approval": True,
                }
            else:
                response = interrupt(
                    {
                        "type": "TRADING_RESEARCH_REVIEW",
                        "symbol": pack.symbol,
                        "decision_time": pack.decision_time.isoformat(),
                        "proposed_plan": plan.model_dump(mode="json"),
                        "allowed_decisions": ["approve", "reject", "reduce"],
                        "external_broker": False,
                    }
                )
                if not isinstance(response, Mapping):
                    raise ValueError("human review response must be a JSON object")
                decision = str(response.get("decision", "reject")).lower()
                if decision == "approve":
                    review = {"status": "APPROVED", "decision": decision}
                elif decision == "reject":
                    manager = self._model(state, "manager", ResearchDecision)
                    plan = _hold_plan(manager, rationale="human reviewer rejected graph plan")
                    review = {"status": "REJECTED", "decision": decision}
                elif decision == "reduce":
                    target = float(response.get("target_weight", 0.0))
                    if plan.action != "BUY":
                        raise ValueError("reduce review is supported only for BUY plans")
                    if not 0.0 <= target <= plan.target_weight:
                        raise ValueError("reviewed target must not increase exposure")
                    if target == 0.0:
                        manager = self._model(state, "manager", ResearchDecision)
                        plan = _hold_plan(manager, rationale="human reviewer reduced BUY to zero")
                    else:
                        plan = plan.model_copy(
                            update={
                                "target_weight": target,
                                "rationale": f"{plan.rationale}; human-reviewed reduction",
                            }
                        )
                    review = {
                        "status": "EDITED",
                        "decision": decision,
                        "target_weight": target,
                    }
                else:
                    raise ValueError("unsupported human review decision")
            return {
                "trader": plan.model_dump(mode="json"),
                "human_review": review,
                "execution_path": ["human_review_gate"],
            }

        # LangGraph 1.2 supports cancellable node timeouts only for async nodes.
        # These nodes are synchronous, so provider/HTTP timeouts remain the safe
        # cancellation boundary and LangGraph supplies retry orchestration.
        llm_node_kwargs = {"retry_policy": retry}
        builder.add_node("news_analyst", news_node, **llm_node_kwargs)
        builder.add_node("macro_analyst", macro_node, **llm_node_kwargs)
        builder.add_node("fundamental_analyst", fundamental_node, **llm_node_kwargs)
        builder.add_node("debate_dispatch", debate_dispatch)
        builder.add_node("bull_researcher", bull_node, **llm_node_kwargs)
        builder.add_node("bear_researcher", bear_node, **llm_node_kwargs)
        builder.add_node("debate_join", debate_join)
        builder.add_node("research_manager", manager_node, **llm_node_kwargs)
        builder.add_node("cost_aware_hold", deterministic_hold_node)
        builder.add_node("preliminary_trader", preliminary_node, **llm_node_kwargs)
        risk_nodes: list[str] = []
        for persona in self.risk_personas:
            node_id = f"risk_review_{persona}"
            risk_nodes.append(node_id)
            builder.add_node(node_id, risk_node(persona), **llm_node_kwargs)
        builder.add_node("portfolio_manager", portfolio_manager_node, **llm_node_kwargs)
        builder.add_node("human_review_gate", human_review_node)

        for analyst in ("news_analyst", "macro_analyst", "fundamental_analyst"):
            builder.add_edge(START, analyst)
        builder.add_edge(
            ["news_analyst", "macro_analyst", "fundamental_analyst"],
            "debate_dispatch",
        )
        builder.add_edge("debate_dispatch", "bull_researcher")
        builder.add_edge("debate_dispatch", "bear_researcher")
        builder.add_edge(["bull_researcher", "bear_researcher"], "debate_join")
        builder.add_conditional_edges(
            "debate_join",
            debate_route,
            {"continue": "debate_dispatch", "manager": "research_manager"},
        )
        builder.add_conditional_edges(
            "research_manager",
            manager_route,
            {
                "low_confidence_hold": "cost_aware_hold",
                "full_committee": "preliminary_trader",
            },
        )
        for node_id in risk_nodes:
            builder.add_edge("preliminary_trader", node_id)
        builder.add_edge(risk_nodes, "portfolio_manager")
        builder.add_edge("portfolio_manager", "human_review_gate")
        builder.add_edge("cost_aware_hold", "human_review_gate")
        builder.add_edge("human_review_gate", END)
        return builder.compile(
            checkpointer=checkpointer,
            name="tradinglab_research_graph",
        )

    @contextmanager
    def _checkpointer(
        self,
        path: str | Path | None,
    ) -> Any:
        if path is None:
            yield InMemorySaver()
            return
        with open_sqlite_checkpoint_saver(path) as saver:
            yield saver

    @staticmethod
    def _result_from_state(state: Mapping[str, Any]) -> ResearchPipelineResult:
        rounds = tuple(
            DebateRound.model_validate(item) for item in state["debate_rounds"]
        )
        return ResearchPipelineResult(
            symbol=str(state["symbol"]),
            decision_time=str(state["decision_time"]),
            news=NewsAnalysis.model_validate(state["news"]),
            macro=MacroAnalysis.model_validate(state["macro"]),
            fundamental=FundamentalAnalysis.model_validate(state["fundamental"]),
            debate_rounds=rounds,
            bull=rounds[-1].bull,
            bear=rounds[-1].bear,
            manager=ResearchDecision.model_validate(state["manager"]),
            preliminary_trader=TradePlan.model_validate(state["preliminary_trader"]),
            risk_reviews=tuple(
                RiskReview.model_validate(item) for item in state.get("risk_reviews", [])
            ),
            trader=TradePlan.model_validate(state["trader"]),
        )

    def run(
        self,
        pack: EvidencePack,
        *,
        thread_id: str,
        node_runner: NodeRunner,
        current_weight: float = 0.0,
        memory_context: Sequence[Mapping[str, Any]] = (),
        checkpointer_path: str | Path | None = None,
        resume: bool = False,
        human_response: Mapping[str, Any] | None = None,
    ) -> ResearchGraphExecution:
        config: RunnableConfig = {
            "configurable": {"thread_id": thread_id},
            "tags": ["tradinglab", "research-graph", pack.symbol],
            "metadata": {"symbol": pack.symbol, "thread_id": thread_id},
        }
        initial: ResearchGraphState = {
            "symbol": pack.symbol,
            "decision_time": pack.decision_time.isoformat(),
            "current_weight": current_weight,
            "memory_context": [dict(item) for item in memory_context],
            "debate_round": 1,
            "debate_rounds": [],
            "risk_reviews": [],
            "execution_path": [],
        }
        with self._checkpointer(checkpointer_path) as checkpointer:
            if not resume and human_response is None:
                delete_thread = getattr(checkpointer, "delete_thread", None)
                if callable(delete_thread):
                    delete_thread(thread_id)
            graph = self.build(
                pack,
                thread_id=thread_id,
                node_runner=node_runner,
                checkpointer=checkpointer,
            )
            if human_response is not None:
                graph_input: ResearchGraphState | Command | None = Command(
                    resume=dict(human_response)
                )
            elif resume:
                graph_input = None
            else:
                graph_input = initial
            _append_graph_event(
                self.event_path,
                {
                    "event": "graph_start",
                    "thread_id": thread_id,
                    "symbol": pack.symbol,
                    "resume": resume or human_response is not None,
                },
            )
            event_count = 0
            try:
                for update in graph.stream(
                    graph_input,
                    config=config,
                    stream_mode="updates",
                ):
                    if not isinstance(update, dict):
                        continue
                    nodes = sorted(str(name) for name in update)
                    event_count += 1
                    _append_graph_event(
                        self.event_path,
                        {
                            "event": "graph_step",
                            "thread_id": thread_id,
                            "nodes": nodes,
                        },
                    )
            except Exception as exc:
                _append_graph_event(
                    self.event_path,
                    {
                        "event": "graph_error",
                        "thread_id": thread_id,
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:500],
                    },
                )
                raise
            snapshot = graph.get_state(config)
            state = dict(snapshot.values)
            interrupts = tuple(
                item.value if isinstance(item.value, dict) else {"value": item.value}
                for item in snapshot.interrupts
            )
            history = tuple(graph.get_state_history(config))
            interrupted = bool(interrupts)
            result = None if interrupted else self._result_from_state(state)
            _append_graph_event(
                self.event_path,
                {
                    "event": "graph_interrupted" if interrupted else "graph_complete",
                    "thread_id": thread_id,
                    "checkpoint_count": len(history),
                    "step_event_count": event_count,
                },
            )
            return ResearchGraphExecution(
                result=result,
                thread_id=thread_id,
                interrupted=interrupted,
                interrupt_payloads=interrupts,
                latest_state=state,
                checkpoint_count=len(history),
                execution_path=tuple(state.get("execution_path", [])),
                event_count=event_count,
            )

    def mermaid(self, pack: EvidencePack, *, thread_id: str = "graph-preview") -> str:
        with self._checkpointer(None) as checkpointer:
            graph = self.build(
                pack,
                thread_id=thread_id,
                node_runner=lambda _name, _model, function: function(),
                checkpointer=checkpointer,
            )
            return graph.get_graph().draw_mermaid()


__all__ = [
    "LangGraphResearchRuntime",
    "ResearchGraphExecution",
    "inspect_research_thread",
    "ResearchGraphInterrupted",
    "ResearchGraphState",
]
