from __future__ import annotations

import hashlib
import json
import operator
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from tradinglab_agents.agents.regime import RegimeAssessment
from tradinglab_agents.engine.portfolio_planner import (
    PortfolioPlan,
    PortfolioPlanner,
    PortfolioPlanningContext,
    SymbolPlanningResult,
)
from tradinglab_agents.models import (
    Action,
    AgentOpinion,
    Evidence,
    EvidencePack,
    PortfolioRiskDecision,
    TradeIntent,
)
from tradinglab_agents.storage.langgraph_checkpoints import (
    open_sqlite_checkpoint_saver,
)


AuditRunner = Callable[[str, Callable[[], Any]], Any]
_DECISION_EVENT_LOCK = threading.Lock()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_event(path: Path | None, payload: Mapping[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"timestamp_utc": _utc_now(), **dict(payload)}
    with _DECISION_EVENT_LOCK:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def _evidence_to_dict(item: Evidence) -> dict[str, Any]:
    return {
        "evidence_id": item.evidence_id,
        "kind": item.kind,
        "timestamp": item.timestamp.isoformat(),
        "available_at": item.available_at.isoformat(),
        "value": item.value,
        "source": item.source,
        "detail": item.detail,
    }


def _evidence_from_dict(payload: Mapping[str, Any]) -> Evidence:
    return Evidence(
        evidence_id=str(payload["evidence_id"]),
        kind=str(payload["kind"]),
        timestamp=datetime.fromisoformat(str(payload["timestamp"])),
        available_at=datetime.fromisoformat(str(payload["available_at"])),
        value=payload["value"],
        source=str(payload["source"]),
        detail=str(payload.get("detail", "")),
    )


def _pack_to_dict(pack: EvidencePack) -> dict[str, Any]:
    return {
        "symbol": pack.symbol,
        "decision_time": pack.decision_time.isoformat(),
        "evidence": [_evidence_to_dict(item) for item in pack.evidence],
    }


def _pack_from_dict(payload: Mapping[str, Any]) -> EvidencePack:
    pack = EvidencePack(
        symbol=str(payload["symbol"]).upper(),
        decision_time=datetime.fromisoformat(str(payload["decision_time"])),
    )
    for item in payload.get("evidence", []):
        pack.add(_evidence_from_dict(item))
    return pack


def _opinion_to_dict(opinion: AgentOpinion | None) -> dict[str, Any] | None:
    if opinion is None:
        return None
    return {
        "agent": opinion.agent,
        "action": opinion.action.value,
        "confidence": opinion.confidence,
        "score": opinion.score,
        "rationale": opinion.rationale,
        "evidence_ids": list(opinion.evidence_ids),
    }


def _opinion_from_dict(payload: Mapping[str, Any] | None) -> AgentOpinion | None:
    if payload is None:
        return None
    return AgentOpinion(
        agent=str(payload["agent"]),
        action=Action(str(payload["action"])),
        confidence=float(payload["confidence"]),
        score=float(payload["score"]),
        rationale=str(payload["rationale"]),
        evidence_ids=tuple(str(item) for item in payload.get("evidence_ids", [])),
    )


def _intent_to_dict(intent: TradeIntent) -> dict[str, Any]:
    return {
        "symbol": intent.symbol,
        "action": intent.action.value,
        "confidence": intent.confidence,
        "target_weight": intent.target_weight,
        "rationale": intent.rationale,
        "evidence_ids": list(intent.evidence_ids),
    }


def _intent_from_dict(payload: Mapping[str, Any]) -> TradeIntent:
    return TradeIntent(
        symbol=str(payload["symbol"]).upper(),
        action=Action(str(payload["action"])),
        confidence=float(payload["confidence"]),
        target_weight=float(payload["target_weight"]),
        rationale=str(payload["rationale"]),
        evidence_ids=tuple(str(item) for item in payload.get("evidence_ids", [])),
    )


def _regime_to_dict(regime: RegimeAssessment) -> dict[str, Any]:
    return {
        "label": regime.label,
        "exposure_multiplier": regime.exposure_multiplier,
        "rationale": regime.rationale,
        "evidence_ids": list(regime.evidence_ids),
    }


def _regime_from_dict(payload: Mapping[str, Any]) -> RegimeAssessment:
    return RegimeAssessment(
        label=str(payload["label"]),
        exposure_multiplier=float(payload["exposure_multiplier"]),
        rationale=str(payload["rationale"]),
        evidence_ids=tuple(str(item) for item in payload.get("evidence_ids", [])),
    )


def portfolio_plan_to_dict(plan: PortfolioPlan) -> dict[str, Any]:
    return {
        "decision_time": plan.decision_time,
        "market_timestamp": plan.market_timestamp,
        "proposed_targets": dict(plan.proposed_targets),
        "target_weights": dict(plan.target_weights),
        "confidences": dict(plan.confidences),
        "symbol_decisions": {
            symbol: dict(value) for symbol, value in plan.symbol_decisions.items()
        },
        "risk_decision": {
            "approved": plan.risk_decision.approved,
            "target_weights": dict(plan.risk_decision.target_weights),
            "reason": plan.risk_decision.reason,
            "force_execution": plan.risk_decision.force_execution,
            "state": plan.risk_decision.state,
        },
        "strategy_state": dict(plan.strategy_state),
    }


def portfolio_plan_from_dict(payload: Mapping[str, Any]) -> PortfolioPlan:
    risk = dict(payload["risk_decision"])
    return PortfolioPlan(
        decision_time=str(payload["decision_time"]),
        market_timestamp=str(payload["market_timestamp"]),
        proposed_targets={
            str(symbol).upper(): float(value)
            for symbol, value in dict(payload["proposed_targets"]).items()
        },
        target_weights={
            str(symbol).upper(): float(value)
            for symbol, value in dict(payload["target_weights"]).items()
        },
        confidences={
            str(symbol).upper(): float(value)
            for symbol, value in dict(payload["confidences"]).items()
        },
        symbol_decisions={
            str(symbol).upper(): dict(value)
            for symbol, value in dict(payload["symbol_decisions"]).items()
        },
        risk_decision=PortfolioRiskDecision(
            approved=bool(risk["approved"]),
            target_weights={
                str(symbol).upper(): float(value)
                for symbol, value in dict(risk["target_weights"]).items()
            },
            reason=str(risk["reason"]),
            force_execution=bool(risk.get("force_execution", False)),
            state=str(risk.get("state", "ACTIVE")),
        ),
        strategy_state=dict(payload["strategy_state"]),
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def planning_input_sha256(context: PortfolioPlanningContext) -> str:
    market_files = {
        symbol: {
            "path": str(context.market.providers[symbol].path.resolve()),
            "sha256": _file_sha256(context.market.providers[symbol].path),
        }
        for symbol in context.market.symbols
    }
    news_files = {
        symbol: {
            "path": str(provider.path.resolve()),
            "sha256": _file_sha256(provider.path),
        }
        for symbol, provider in sorted(context.news_providers.items())
    }
    evidence_files = [
        {
            "path": str(provider.path.resolve()),
            "sha256": _file_sha256(provider.path),
        }
        for provider in context.evidence_providers
    ]
    corporate_action_files = {
        symbol: {
            "path": str(provider.path.resolve()),
            "sha256": _file_sha256(provider.path),
        }
        for symbol, provider in sorted(context.market.corporate_actions.items())
        if str(provider.path) and provider.path.is_file()
    }
    payload = {
        "symbols": list(context.market.symbols),
        "market_timestamp": context.timestamp.isoformat(),
        "decision_time": context.decision_time.isoformat(),
        "portfolio": {
            "cash": context.portfolio.cash,
            "positions": dict(sorted(context.portfolio.positions.items())),
            "peak_equity": context.portfolio.peak_equity,
        },
        "current_weights": dict(context.current_weights),
        "strategy_state": dict(context.strategy_state),
        "research_overlays": dict(context.overlays),
        "market_files": market_files,
        "news_files": news_files,
        "evidence_files": evidence_files,
        "corporate_action_files": corporate_action_files,
        "market_semantics": {
            "calendar": (
                context.market.calendar.name
                if context.market.calendar is not None
                else None
            ),
            "strict_session_times": context.market.strict_session_times,
            "require_complete_alignment": (
                context.market.require_complete_alignment
            ),
            "adjust_history_for_splits": (
                context.market.adjust_history_for_splits
            ),
            "adjust_history_for_dividends": (
                context.market.adjust_history_for_dividends
            ),
        },
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def canonical_plan_sha256(plan: PortfolioPlan | Mapping[str, Any]) -> str:
    payload = (
        portfolio_plan_to_dict(plan)
        if isinstance(plan, PortfolioPlan)
        else dict(plan)
    )
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


class SymbolDecisionInput(TypedDict):
    symbol: str


class SymbolDecisionState(TypedDict, total=False):
    symbol: str
    pack: dict[str, Any]
    quant_opinion: dict[str, Any]
    context_opinion: dict[str, Any] | None
    combined_opinion: dict[str, Any]
    reviewed_opinion: dict[str, Any]
    intent: dict[str, Any]
    regime: dict[str, Any]
    regime_state: dict[str, Any]
    symbol_results: Annotated[list[dict[str, Any]], operator.add]
    execution_path: Annotated[list[str], operator.add]


class SymbolDecisionOutput(TypedDict, total=False):
    symbol_results: Annotated[list[dict[str, Any]], operator.add]
    execution_path: Annotated[list[str], operator.add]


class DecisionGraphState(TypedDict, total=False):
    input_sha256: str
    symbols: list[str]
    context_summary: dict[str, Any]
    symbol_results: Annotated[list[dict[str, Any]], operator.add]
    portfolio_plan: dict[str, Any]
    plan_sha256: str
    execution_path: Annotated[list[str], operator.add]


@dataclass(frozen=True)
class DecisionGraphExecution:
    plan: PortfolioPlan
    thread_id: str
    checkpoint_count: int
    event_count: int
    execution_path: tuple[str, ...]
    plan_sha256: str
    latest_state: dict[str, Any]


@dataclass
class LangGraphDeterministicDecisionRuntime:
    planner: PortfolioPlanner
    event_path: Path | None = None

    def _symbol_graph(
        self,
        *,
        context: PortfolioPlanningContext | None,
        audit_runner: AuditRunner,
    ) -> Any:
        builder = StateGraph(
            SymbolDecisionState,
            input_schema=SymbolDecisionInput,
            output_schema=SymbolDecisionOutput,
        )

        def evidence_pack(state: SymbolDecisionState) -> SymbolDecisionState:
            if context is None:
                raise RuntimeError("decision graph preview cannot execute")
            symbol = str(state["symbol"]).upper()
            payload = dict(
                audit_runner(
                    f"decision.{symbol}.evidence_pack",
                    lambda: _pack_to_dict(
                        self.planner.build_evidence_pack(context, symbol)
                    ),
                )
            )
            return {
                "pack": payload,
                "execution_path": [f"decision.{symbol}.evidence_pack"],
            }

        def quant_signal(state: SymbolDecisionState) -> SymbolDecisionState:
            symbol = str(state["symbol"]).upper()
            pack = _pack_from_dict(state["pack"])
            payload = dict(
                audit_runner(
                    f"decision.{symbol}.quant_signal",
                    lambda: _opinion_to_dict(self.planner.quant_signal(pack)),
                )
            )
            return {
                "quant_opinion": payload,
                "execution_path": [f"decision.{symbol}.quant_signal"],
            }

        def fusion_critic(state: SymbolDecisionState) -> SymbolDecisionState:
            symbol = str(state["symbol"]).upper()
            pack = _pack_from_dict(state["pack"])
            quant = _opinion_from_dict(state["quant_opinion"])
            assert quant is not None

            def compute() -> dict[str, Any]:
                context_opinion, combined, reviewed, intent = (
                    self.planner.fuse_and_criticize(pack, quant)
                )
                return {
                    "context_opinion": _opinion_to_dict(context_opinion),
                    "combined_opinion": _opinion_to_dict(combined),
                    "reviewed_opinion": _opinion_to_dict(reviewed),
                    "intent": _intent_to_dict(intent),
                }

            payload = dict(
                audit_runner(
                    f"decision.{symbol}.fusion_critic",
                    compute,
                )
            )
            return {
                **payload,
                "execution_path": [f"decision.{symbol}.fusion_critic"],
            }

        def regime_guard(state: SymbolDecisionState) -> SymbolDecisionState:
            if context is None:
                raise RuntimeError("decision graph preview cannot execute")
            symbol = str(state["symbol"]).upper()
            pack = _pack_from_dict(state["pack"])

            def compute() -> dict[str, Any]:
                regime, next_state = self.planner.assess_regime(
                    context,
                    symbol,
                    pack,
                )
                return {
                    "regime": _regime_to_dict(regime),
                    "regime_state": dict(next_state),
                }

            payload = dict(
                audit_runner(
                    f"decision.{symbol}.regime_guard",
                    compute,
                )
            )
            return {
                **payload,
                "execution_path": [f"decision.{symbol}.regime_guard"],
            }

        def target_overlay(state: SymbolDecisionState) -> SymbolDecisionOutput:
            if context is None:
                raise RuntimeError("decision graph preview cannot execute")
            symbol = str(state["symbol"]).upper()
            pack = _pack_from_dict(state["pack"])
            quant = _opinion_from_dict(state["quant_opinion"])
            combined = _opinion_from_dict(state["combined_opinion"])
            reviewed = _opinion_from_dict(state["reviewed_opinion"])
            assert quant is not None and combined is not None and reviewed is not None
            context_opinion = _opinion_from_dict(state.get("context_opinion"))
            intent = _intent_from_dict(state["intent"])
            regime = _regime_from_dict(state["regime"])
            payload = dict(
                audit_runner(
                    f"decision.{symbol}.target_overlay",
                    lambda: self.planner.apply_symbol_target(
                        context,
                        symbol,
                        pack=pack,
                        quant_opinion=quant,
                        context_opinion=context_opinion,
                        combined=combined,
                        reviewed=reviewed,
                        intent=intent,
                        regime=regime,
                        next_regime_state=state["regime_state"],
                    ).to_dict(),
                )
            )
            return {
                "symbol_results": [payload],
                "execution_path": [f"decision.{symbol}.target_overlay"],
            }

        builder.add_node("evidence_pack", evidence_pack)
        builder.add_node("quant_signal", quant_signal)
        builder.add_node("fusion_critic", fusion_critic)
        builder.add_node("regime_guard", regime_guard)
        builder.add_node("target_overlay", target_overlay)
        builder.add_edge(START, "evidence_pack")
        builder.add_edge("evidence_pack", "quant_signal")
        builder.add_edge("evidence_pack", "regime_guard")
        builder.add_edge("quant_signal", "fusion_critic")
        builder.add_edge(["fusion_critic", "regime_guard"], "target_overlay")
        builder.add_edge("target_overlay", END)
        return builder.compile(name="tradinglab_symbol_decision_subgraph")

    def build(
        self,
        *,
        context: PortfolioPlanningContext | None,
        audit_runner: AuditRunner,
        checkpointer: BaseCheckpointSaver[Any] | None = None,
    ) -> Any:
        builder = StateGraph(DecisionGraphState)
        symbol_subgraph = self._symbol_graph(
            context=context,
            audit_runner=audit_runner,
        )

        def prepare_context(_: DecisionGraphState) -> DecisionGraphState:
            if context is None:
                symbols = ["DEMO"]
                input_digest = "preview"
                summary = {
                    "decision_time": "preview",
                    "market_timestamp": "preview",
                    "current_weights": {"DEMO": 0.0},
                    "overlay_symbols": [],
                    "external_broker": False,
                }
            else:
                symbols = list(context.market.symbols)
                input_digest = planning_input_sha256(context)
                summary = {
                    "decision_time": context.decision_time.isoformat(),
                    "market_timestamp": context.timestamp.isoformat(),
                    "current_weights": dict(context.current_weights),
                    "overlay_symbols": sorted(context.overlays),
                    "portfolio_equity": context.portfolio.equity(
                        context.close_snapshot
                    ),
                    "portfolio_peak_equity": context.portfolio.peak_equity,
                    "external_broker": False,
                }
            if not symbols:
                raise ValueError("decision graph has no symbols")
            return {
                "input_sha256": input_digest,
                "symbols": symbols,
                "context_summary": summary,
                "symbol_results": [],
                "execution_path": ["decision.prepare_context"],
            }

        def fan_out(state: DecisionGraphState) -> list[Send]:
            return [
                Send("symbol_pipeline", {"symbol": symbol})
                for symbol in state["symbols"]
            ]

        def portfolio_risk(state: DecisionGraphState) -> DecisionGraphState:
            if context is None:
                preview_plan = {
                    "decision_time": "preview",
                    "market_timestamp": "preview",
                    "proposed_targets": {"DEMO": 0.0},
                    "target_weights": {"DEMO": 0.0},
                    "confidences": {"DEMO": 0.0},
                    "symbol_decisions": {"DEMO": {"final_action": "HOLD"}},
                    "risk_decision": {
                        "approved": True,
                        "target_weights": {"DEMO": 0.0},
                        "reason": "preview",
                        "force_execution": False,
                        "state": "ACTIVE",
                    },
                    "strategy_state": {"regimes": {}, "risk_state": "ACTIVE"},
                }
                return {
                    "portfolio_plan": preview_plan,
                    "execution_path": ["decision.portfolio_risk"],
                }
            rows = [
                SymbolPlanningResult.from_dict(item)
                for item in state.get("symbol_results", [])
            ]
            plan = audit_runner(
                "decision.portfolio_risk",
                lambda: self.planner.finalize_plan(context, rows),
            )
            if not isinstance(plan, PortfolioPlan):
                raise TypeError("decision portfolio risk node must return PortfolioPlan")
            return {
                "portfolio_plan": portfolio_plan_to_dict(plan),
                "execution_path": ["decision.portfolio_risk"],
            }

        def finalize(state: DecisionGraphState) -> DecisionGraphState:
            def compute() -> dict[str, Any]:
                payload = dict(state["portfolio_plan"])
                symbols = sorted(state["symbols"])
                proposed = sorted(
                    str(item).upper() for item in payload["proposed_targets"]
                )
                targets = sorted(
                    str(item).upper() for item in payload["target_weights"]
                )
                decisions = sorted(
                    str(item).upper() for item in payload["symbol_decisions"]
                )
                if not (symbols == proposed == targets == decisions):
                    raise ValueError(
                        "decision graph final symbol mismatch: "
                        f"symbols={symbols}, proposed={proposed}, targets={targets}, "
                        f"decisions={decisions}"
                    )
                if any(
                    float(value) < -1e-12
                    for value in payload["target_weights"].values()
                ):
                    raise ValueError("decision graph produced a negative target")
                return {"plan_sha256": canonical_plan_sha256(payload)}

            payload = dict(audit_runner("decision.finalize", compute))
            return {
                **payload,
                "execution_path": ["decision.finalize"],
            }

        builder.add_node("prepare_context", prepare_context)
        builder.add_node("symbol_pipeline", symbol_subgraph)
        builder.add_node("portfolio_risk", portfolio_risk)
        builder.add_node("finalize", finalize)
        builder.add_edge(START, "prepare_context")
        builder.add_conditional_edges("prepare_context", fan_out)
        builder.add_edge("symbol_pipeline", "portfolio_risk")
        builder.add_edge("portfolio_risk", "finalize")
        builder.add_edge("finalize", END)
        return builder.compile(
            checkpointer=checkpointer,
            name="tradinglab_deterministic_decision_graph",
        )

    def run(
        self,
        *,
        thread_id: str,
        context: PortfolioPlanningContext,
        audit_runner: AuditRunner,
        checkpointer_path: str | Path | None = None,
        resume: bool = False,
    ) -> DecisionGraphExecution:
        config: RunnableConfig = {
            "configurable": {"thread_id": thread_id},
            "tags": ["tradinglab", "deterministic-decision-graph"],
            "metadata": {
                "thread_id": thread_id,
                "graph_type": "deterministic_decision",
            },
        }
        manager = (
            open_sqlite_checkpoint_saver(checkpointer_path)
            if checkpointer_path is not None
            else None
        )
        if manager is None:
            checkpointer = InMemorySaver()
            return self._run_with_saver(
                thread_id=thread_id,
                context=context,
                audit_runner=audit_runner,
                checkpointer=checkpointer,
                config=config,
                resume=resume,
                database=None,
            )
        with manager as checkpointer:
            return self._run_with_saver(
                thread_id=thread_id,
                context=context,
                audit_runner=audit_runner,
                checkpointer=checkpointer,
                config=config,
                resume=resume,
                database=Path(checkpointer_path),
            )

    def _run_with_saver(
        self,
        *,
        thread_id: str,
        context: PortfolioPlanningContext,
        audit_runner: AuditRunner,
        checkpointer: BaseCheckpointSaver[Any],
        config: RunnableConfig,
        resume: bool,
        database: Path | None,
    ) -> DecisionGraphExecution:
        expected_input_sha256 = planning_input_sha256(context)
        get_tuple = getattr(checkpointer, "get_tuple", None)
        existing_tuple = get_tuple(config) if callable(get_tuple) else None
        checkpoint_exists = existing_tuple is not None
        if resume and checkpoint_exists:
            existing_values = dict(
                existing_tuple.checkpoint.get("channel_values", {})
            )
            existing_input_sha256 = existing_values.get("input_sha256")
            if existing_input_sha256 != expected_input_sha256:
                raise ValueError(
                    "decision graph resume input hash mismatch: "
                    f"expected={expected_input_sha256}, "
                    f"checkpoint={existing_input_sha256}"
                )
        effective_resume = resume and checkpoint_exists
        if not effective_resume:
            delete_thread = getattr(checkpointer, "delete_thread", None)
            if callable(delete_thread):
                delete_thread(thread_id)
        graph = self.build(
            context=context,
            audit_runner=audit_runner,
            checkpointer=checkpointer,
        )
        _append_event(
            self.event_path,
            {
                "event": "decision_graph_start",
                "thread_id": thread_id,
                "resume_requested": resume,
                "resume": effective_resume,
                "symbol_count": len(context.market.symbols),
            },
        )
        event_count = 0
        try:
            graph_input: DecisionGraphState | None = (
                None if effective_resume else {"execution_path": []}
            )
            for update in graph.stream(
                graph_input,
                config=config,
                stream_mode="updates",
                subgraphs=True,
            ):
                event_count += 1
                if isinstance(update, tuple) and len(update) == 2:
                    namespace, payload = update
                    nodes = sorted(str(name) for name in payload) if isinstance(payload, dict) else []
                    namespace_value = [str(item) for item in namespace]
                elif isinstance(update, dict):
                    nodes = sorted(str(name) for name in update)
                    namespace_value = []
                else:
                    nodes = []
                    namespace_value = []
                _append_event(
                    self.event_path,
                    {
                        "event": "decision_graph_step",
                        "thread_id": thread_id,
                        "namespace": namespace_value,
                        "nodes": nodes,
                    },
                )
        except Exception as exc:
            _append_event(
                self.event_path,
                {
                    "event": "decision_graph_error",
                    "thread_id": thread_id,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:500],
                },
            )
            raise
        snapshot = graph.get_state(config)
        state = dict(snapshot.values)
        history = tuple(graph.get_state_history(config))
        raw_plan = portfolio_plan_from_dict(state["portfolio_plan"])
        digest = str(state["plan_sha256"])
        runtime = {
            "runtime": "langgraph",
            "graph_type": "deterministic_decision",
            "thread_id": thread_id,
            "checkpoint_count": len(history),
            "stream_event_count": event_count,
            "execution_path": list(state.get("execution_path", [])),
            "input_sha256": expected_input_sha256,
            "plan_sha256": digest,
            "checkpoint_database": str(database) if database is not None else None,
            "account_mutation_in_graph": False,
            "order_persistence_in_graph": False,
            "external_broker": False,
        }
        plan = replace(raw_plan, graph_runtime=runtime)
        _append_event(
            self.event_path,
            {
                "event": "decision_graph_complete",
                "thread_id": thread_id,
                "checkpoint_count": len(history),
                "step_event_count": event_count,
                "plan_sha256": digest,
            },
        )
        return DecisionGraphExecution(
            plan=plan,
            thread_id=thread_id,
            checkpoint_count=len(history),
            event_count=event_count,
            execution_path=tuple(state.get("execution_path", [])),
            plan_sha256=digest,
            latest_state=state,
        )

    def mermaid(self) -> str:
        graph = self.build(
            context=None,
            audit_runner=lambda _name, function: function(),
            checkpointer=InMemorySaver(),
        )
        return graph.get_graph(xray=True).draw_mermaid()


__all__ = [
    "DecisionGraphExecution",
    "LangGraphDeterministicDecisionRuntime",
    "canonical_plan_sha256",
    "planning_input_sha256",
    "portfolio_plan_from_dict",
    "portfolio_plan_to_dict",
]
