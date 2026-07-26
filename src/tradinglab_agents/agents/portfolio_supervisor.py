from __future__ import annotations

import json
import math
import operator
import threading
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal, TypedDict, TypeVar

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy
from pydantic import BaseModel

from tradinglab_agents.agents.langchain_runtime import LangChainStructuredClient
from tradinglab_agents.agents.schemas import (
    GuardedPortfolioAllocation,
    PortfolioAllocationItem,
    PortfolioCommitteeReview,
    PortfolioSupervisorDecision,
)
from tradinglab_agents.data.csv_provider import LocalCsvProvider
from tradinglab_agents.storage.langgraph_checkpoints import (
    open_sqlite_checkpoint_saver,
)


TModel = TypeVar("TModel", bound=BaseModel)
NodeRunner = Callable[[str, type[Any], Callable[[], Any]], Any]
PORTFOLIO_SUPERVISOR_CALLS = 3
_EVENT_LOCK = threading.Lock()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_event(path: Path | None, payload: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"timestamp_utc": _utc_now(), **payload}
    with _EVENT_LOCK:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def _retryable(exc: Exception) -> bool:
    possible_cause = getattr(exc, "cause", None)
    root: Exception = possible_cause if isinstance(possible_cause, Exception) else exc
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


def _pearson(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or len(left) < 2:
        return 0.0
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    left_centered = [value - left_mean for value in left]
    right_centered = [value - right_mean for value in right]
    numerator = sum(a * b for a, b in zip(left_centered, right_centered, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left_centered))
    right_norm = math.sqrt(sum(value * value for value in right_centered))
    if left_norm <= 1e-15 or right_norm <= 1e-15:
        return 0.0
    return max(-1.0, min(1.0, numerator / (left_norm * right_norm)))


def build_portfolio_market_context(
    providers: Mapping[str, LocalCsvProvider],
    *,
    window_sessions: int,
    high_correlation_threshold: float,
) -> dict[str, Any]:
    """Build point-in-time cross-asset statistics from synchronized closes."""

    if window_sessions < 20:
        raise ValueError("portfolio correlation window must be at least 20 sessions")
    if not 0.0 <= high_correlation_threshold <= 1.0:
        raise ValueError("high correlation threshold must be in [0, 1]")
    normalized = {symbol.upper(): provider for symbol, provider in providers.items()}
    if not normalized:
        raise ValueError("portfolio market context requires at least one provider")
    timestamp_sets = [set(bar.timestamp for bar in provider.bars) for provider in normalized.values()]
    common = sorted(set.intersection(*timestamp_sets))
    required = min(len(common), window_sessions + 1)
    if required < 3:
        raise ValueError("insufficient synchronized history for portfolio context")
    selected = common[-required:]
    close_maps = {
        symbol: {bar.timestamp: bar.close for bar in provider.bars}
        for symbol, provider in normalized.items()
    }
    returns: dict[str, list[float]] = {}
    volatilities: dict[str, float] = {}
    trailing_returns: dict[str, float] = {}
    for symbol in sorted(normalized):
        closes = [float(close_maps[symbol][timestamp]) for timestamp in selected]
        series = [
            closes[index] / closes[index - 1] - 1.0
            for index in range(1, len(closes))
            if closes[index - 1] > 0
        ]
        returns[symbol] = series
        mean = sum(series) / len(series)
        variance = (
            sum((value - mean) ** 2 for value in series) / max(1, len(series) - 1)
        )
        volatilities[symbol] = math.sqrt(max(0.0, variance)) * math.sqrt(252.0)
        trailing_returns[symbol] = closes[-1] / closes[0] - 1.0

    correlations: dict[str, float] = {}
    high_pairs: list[dict[str, Any]] = []
    symbols = sorted(normalized)
    for left_index, left in enumerate(symbols):
        for right in symbols[left_index + 1 :]:
            value = _pearson(returns[left], returns[right])
            key = f"{left}|{right}"
            correlations[key] = value
            if value >= high_correlation_threshold:
                high_pairs.append(
                    {
                        "symbols": [left, right],
                        "correlation": value,
                    }
                )
    return {
        "as_of": selected[-1].isoformat(),
        "window_sessions": len(selected) - 1,
        "symbols": symbols,
        "annualized_volatility": volatilities,
        "trailing_return": trailing_returns,
        "correlations": correlations,
        "high_correlation_threshold": high_correlation_threshold,
        "high_correlation_pairs": high_pairs,
        "point_in_time": True,
    }


@dataclass
class PortfolioReviewerAgent:
    client: Any
    reviewer: Literal["correlation", "concentration"]

    def review(
        self,
        candidate_plans: Sequence[Mapping[str, Any]],
        market_context: Mapping[str, Any],
        *,
        max_gross_target: float,
        cluster_gross_cap: float,
    ) -> PortfolioCommitteeReview:
        result = self.client.complete(
            task=f"portfolio_{self.reviewer}_review",
            system_prompt=(
                f"You are the {self.reviewer} reviewer in a paper-only portfolio research "
                "committee. Compare all candidate trade plans together. You may only reduce "
                "or veto proposed exposure; never increase a symbol above its supplied target, "
                "never weaken a protective SELL, and never create a new position. Return "
                "strict structured caps for every supplied symbol."
            ),
            payload={
                "reviewer": self.reviewer,
                "candidate_plans": [dict(item) for item in candidate_plans],
                "market_context": dict(market_context),
                "policy": {
                    "paper_only": True,
                    "may_increase_exposure": False,
                    "max_gross_target": max_gross_target,
                    "high_correlation_cluster_cap": cluster_gross_cap,
                },
            },
            response_model=PortfolioCommitteeReview,
        )
        if result.reviewer != self.reviewer:
            raise ValueError(
                f"portfolio reviewer {self.reviewer} returned reviewer={result.reviewer}"
            )
        supplied = {str(item["symbol"]).upper() for item in candidate_plans}
        returned = {item.symbol for item in result.symbol_caps}
        if supplied != returned:
            raise ValueError(
                f"portfolio {self.reviewer} reviewer returned mismatched symbols: "
                f"expected={sorted(supplied)}, returned={sorted(returned)}"
            )
        return result


@dataclass
class PortfolioSupervisorAgent:
    client: Any

    def allocate(
        self,
        candidate_plans: Sequence[Mapping[str, Any]],
        market_context: Mapping[str, Any],
        reviews: Sequence[PortfolioCommitteeReview],
        *,
        max_gross_target: float,
    ) -> PortfolioSupervisorDecision:
        result = self.client.complete(
            task="portfolio_supervisor",
            system_prompt=(
                "You are the final cross-asset portfolio research supervisor for a paper-only "
                "system. Reconcile per-symbol research with correlation and concentration "
                "reviews. You may only preserve, reduce or veto supplied targets. Do not turn "
                "HOLD into BUY, do not weaken SELL, do not exceed any reviewer cap, and do not "
                "exceed the supplied gross target. Return one allocation per supplied symbol."
            ),
            payload={
                "candidate_plans": [dict(item) for item in candidate_plans],
                "market_context": dict(market_context),
                "committee_reviews": [
                    item.model_dump(mode="json") for item in reviews
                ],
                "policy": {
                    "paper_only": True,
                    "may_increase_exposure": False,
                    "max_gross_target": max_gross_target,
                    "real_broker": False,
                },
            },
            response_model=PortfolioSupervisorDecision,
        )
        supplied = {str(item["symbol"]).upper() for item in candidate_plans}
        returned = {item.symbol for item in result.allocations}
        if supplied != returned:
            raise ValueError(
                "portfolio supervisor returned mismatched symbols: "
                f"expected={sorted(supplied)}, returned={sorted(returned)}"
            )
        return result


def _high_correlation_clusters(
    symbols: Sequence[str],
    correlations: Mapping[str, Any],
    threshold: float,
) -> list[list[str]]:
    adjacency = {symbol: set() for symbol in symbols}
    for raw_key, raw_value in correlations.items():
        try:
            left, right = str(raw_key).split("|", 1)
            value = float(raw_value)
        except (TypeError, ValueError):
            continue
        left, right = left.upper(), right.upper()
        if left in adjacency and right in adjacency and value >= threshold:
            adjacency[left].add(right)
            adjacency[right].add(left)
    clusters: list[list[str]] = []
    visited: set[str] = set()
    for symbol in sorted(symbols):
        if symbol in visited:
            continue
        stack = [symbol]
        component: list[str] = []
        while stack:
            current = stack.pop()
            if current in visited:
                continue
            visited.add(current)
            component.append(current)
            stack.extend(sorted(adjacency[current].difference(visited), reverse=True))
        if len(component) > 1:
            clusters.append(sorted(component))
    return clusters


def guard_portfolio_allocation(
    candidate_plans: Sequence[Mapping[str, Any]],
    reviews: Sequence[PortfolioCommitteeReview],
    proposal: PortfolioSupervisorDecision,
    market_context: Mapping[str, Any],
    *,
    max_gross_target: float,
    max_positions: int,
    high_correlation_threshold: float,
    cluster_gross_cap: float,
) -> GuardedPortfolioAllocation:
    """Deterministically enforce non-expansion and cross-asset concentration limits."""

    originals = {
        str(item["symbol"]).upper(): dict(item) for item in candidate_plans
    }
    proposed = {item.symbol: item for item in proposal.allocations}
    review_caps: dict[str, float] = {
        symbol: float(item.get("target_weight", 0.0))
        for symbol, item in originals.items()
    }
    gross_cap = max_gross_target
    notes: list[str] = []
    for review in reviews:
        gross_cap = min(gross_cap, review.max_gross_target)
        for item in review.symbol_caps:
            if item.symbol in review_caps:
                review_caps[item.symbol] = min(
                    review_caps[item.symbol],
                    item.max_target_weight,
                )

    allocations: dict[str, PortfolioAllocationItem] = {}
    for symbol in sorted(originals):
        original = originals[symbol]
        original_action = str(original.get("action", "HOLD")).upper()
        original_target = max(0.0, min(0.20, float(original.get("target_weight", 0.0))))
        original_confidence = max(
            0.0,
            min(1.0, float(original.get("confidence", 0.0))),
        )
        item = proposed.get(symbol)
        if item is None:
            proposed_action = "HOLD"
            proposed_target = 0.0
            proposed_confidence = original_confidence
            proposed_rationale = "portfolio supervisor omitted the symbol; fail-closed HOLD"
            notes.append(f"{symbol} omitted by supervisor and reduced to HOLD")
        else:
            proposed_action = item.action
            proposed_target = item.target_weight
            proposed_confidence = item.confidence
            proposed_rationale = item.rationale
        cap = min(original_target, review_caps.get(symbol, original_target))
        target = min(proposed_target, cap)

        if original_action == "SELL":
            action, target = "SELL", 0.0
            if proposed_action != "SELL":
                notes.append(f"{symbol} protective SELL restored by deterministic guard")
        elif original_action == "HOLD":
            action = "HOLD"
            target = min(original_target, target)
            if proposed_action != "HOLD" or proposed_target > original_target + 1e-12:
                notes.append(f"{symbol} HOLD could not be upgraded by portfolio supervisor")
        elif target <= 1e-12:
            action, target = "HOLD", 0.0
            notes.append(f"{symbol} BUY reduced to zero by portfolio committee")
        else:
            action = "BUY"
        if proposed_target > cap + 1e-12:
            notes.append(
                f"{symbol} target clamped {proposed_target:.2%}->{target:.2%}"
            )
        allocations[symbol] = PortfolioAllocationItem(
            symbol=symbol,
            action=action,
            target_weight=target,
            confidence=min(original_confidence, proposed_confidence),
            rationale=(
                f"{proposed_rationale}; deterministic non-expansion guard; "
                f"original={original_target:.2%}; committee_cap={cap:.2%}"
            ),
        )

    clusters = _high_correlation_clusters(
        list(allocations),
        dict(market_context.get("correlations", {})),
        high_correlation_threshold,
    )
    for cluster in clusters:
        cluster_gross = sum(allocations[symbol].target_weight for symbol in cluster)
        if cluster_gross <= cluster_gross_cap + 1e-12:
            continue
        scale = cluster_gross_cap / cluster_gross
        for symbol in cluster:
            current = allocations[symbol]
            target = current.target_weight * scale
            allocations[symbol] = current.model_copy(
                update={
                    "action": "BUY" if target > 1e-12 else "HOLD",
                    "target_weight": target,
                    "rationale": (
                        f"{current.rationale}; high-correlation cluster scaled by {scale:.4f}"
                    ),
                }
            )
        notes.append(
            "high-correlation cluster "
            + ",".join(cluster)
            + f" scaled {cluster_gross:.2%}->{cluster_gross_cap:.2%}"
        )

    positive = sorted(
        (
            item for item in allocations.values() if item.target_weight > 1e-12
        ),
        key=lambda item: (-item.target_weight, -item.confidence, item.symbol),
    )
    if len(positive) > max_positions:
        keep = {item.symbol for item in positive[:max_positions]}
        for item in positive[max_positions:]:
            allocations[item.symbol] = item.model_copy(
                update={
                    "action": "HOLD",
                    "target_weight": 0.0,
                    "rationale": f"{item.rationale}; removed by portfolio max_positions guard",
                }
            )
        notes.append(
            "portfolio supervisor max_positions removed "
            + ",".join(sorted(set(item.symbol for item in positive).difference(keep)))
        )

    gross = sum(item.target_weight for item in allocations.values())
    effective_gross_cap = min(max_gross_target, gross_cap)
    if gross > effective_gross_cap + 1e-12:
        scale = effective_gross_cap / gross
        for symbol, item in list(allocations.items()):
            target = item.target_weight * scale
            allocations[symbol] = item.model_copy(
                update={
                    "action": (
                        "SELL"
                        if item.action == "SELL"
                        else ("BUY" if target > 1e-12 else "HOLD")
                    ),
                    "target_weight": target,
                    "rationale": f"{item.rationale}; portfolio gross scaled by {scale:.4f}",
                }
            )
        notes.append(
            f"portfolio supervisor gross scaled {gross:.2%}->{effective_gross_cap:.2%}"
        )
    ordered = tuple(allocations[symbol] for symbol in sorted(allocations))
    return GuardedPortfolioAllocation(
        allocations=ordered,
        gross_target=sum(item.target_weight for item in ordered),
        constraint_notes=tuple(notes),
        high_correlation_clusters=tuple(tuple(cluster) for cluster in clusters),
    )


class PortfolioGraphState(TypedDict, total=False):
    candidate_plans: list[dict[str, Any]]
    market_context: dict[str, Any]
    correlation_review: dict[str, Any]
    concentration_review: dict[str, Any]
    supervisor_proposal: dict[str, Any]
    guarded_allocation: dict[str, Any]
    execution_path: Annotated[list[str], operator.add]


@dataclass(frozen=True)
class PortfolioGraphExecution:
    result: GuardedPortfolioAllocation
    proposal: PortfolioSupervisorDecision
    reviews: tuple[PortfolioCommitteeReview, ...]
    thread_id: str
    checkpoint_count: int
    event_count: int
    execution_path: tuple[str, ...]
    latest_state: dict[str, Any]


@dataclass
class LangGraphPortfolioSupervisorRuntime:
    quick_client: Any
    deep_client: Any
    trace_path: Path
    event_path: Path | None = None
    retry_attempts: int = 2
    max_gross_target: float = 0.90
    max_positions: int = 5
    high_correlation_threshold: float = 0.80
    cluster_gross_cap: float = 0.25

    def __post_init__(self) -> None:
        if not 1 <= self.retry_attempts <= 5:
            raise ValueError("portfolio graph retry_attempts must be in [1, 5]")
        if not 0.0 < self.max_gross_target <= 1.0:
            raise ValueError("portfolio graph max_gross_target must be in (0, 1]")
        if self.max_positions < 1:
            raise ValueError("portfolio graph max_positions must be positive")
        if not 0.0 <= self.high_correlation_threshold <= 1.0:
            raise ValueError("portfolio graph high correlation threshold must be in [0, 1]")
        if not 0.0 < self.cluster_gross_cap <= self.max_gross_target:
            raise ValueError(
                "portfolio graph cluster cap must be positive and no greater than gross cap"
            )

    def _clients(
        self,
        *,
        thread_id: str,
    ) -> tuple[LangChainStructuredClient, LangChainStructuredClient]:
        return (
            LangChainStructuredClient(
                self.quick_client,
                self.trace_path,
                tier="quick",
                symbol="PORTFOLIO",
                thread_id=thread_id,
            ),
            LangChainStructuredClient(
                self.deep_client,
                self.trace_path,
                tier="deep",
                symbol="PORTFOLIO",
                thread_id=thread_id,
            ),
        )

    def build(
        self,
        *,
        thread_id: str,
        node_runner: NodeRunner,
        checkpointer: BaseCheckpointSaver[Any] | None,
    ) -> Any:
        quick, deep = self._clients(thread_id=thread_id)
        retry = RetryPolicy(max_attempts=self.retry_attempts, retry_on=_retryable)
        builder = StateGraph(PortfolioGraphState)

        def run_model(
            name: str,
            model: type[TModel],
            function: Callable[[], TModel],
        ) -> dict[str, Any]:
            result = node_runner(name, model, function)
            return result.model_dump(mode="json")

        def correlation_node(state: PortfolioGraphState) -> PortfolioGraphState:
            return {
                "correlation_review": run_model(
                    "portfolio.correlation_reviewer",
                    PortfolioCommitteeReview,
                    lambda: PortfolioReviewerAgent(quick, "correlation").review(
                        state["candidate_plans"],
                        state["market_context"],
                        max_gross_target=self.max_gross_target,
                        cluster_gross_cap=self.cluster_gross_cap,
                    ),
                ),
                "execution_path": ["portfolio.correlation_reviewer"],
            }

        def concentration_node(state: PortfolioGraphState) -> PortfolioGraphState:
            return {
                "concentration_review": run_model(
                    "portfolio.concentration_reviewer",
                    PortfolioCommitteeReview,
                    lambda: PortfolioReviewerAgent(quick, "concentration").review(
                        state["candidate_plans"],
                        state["market_context"],
                        max_gross_target=self.max_gross_target,
                        cluster_gross_cap=self.cluster_gross_cap,
                    ),
                ),
                "execution_path": ["portfolio.concentration_reviewer"],
            }

        def supervisor_node(state: PortfolioGraphState) -> PortfolioGraphState:
            reviews = (
                PortfolioCommitteeReview.model_validate(state["correlation_review"]),
                PortfolioCommitteeReview.model_validate(state["concentration_review"]),
            )
            return {
                "supervisor_proposal": run_model(
                    "portfolio.supervisor",
                    PortfolioSupervisorDecision,
                    lambda: PortfolioSupervisorAgent(deep).allocate(
                        state["candidate_plans"],
                        state["market_context"],
                        reviews,
                        max_gross_target=self.max_gross_target,
                    ),
                ),
                "execution_path": ["portfolio.supervisor"],
            }

        def guard_node(state: PortfolioGraphState) -> PortfolioGraphState:
            reviews = (
                PortfolioCommitteeReview.model_validate(state["correlation_review"]),
                PortfolioCommitteeReview.model_validate(state["concentration_review"]),
            )
            proposal = PortfolioSupervisorDecision.model_validate(
                state["supervisor_proposal"]
            )
            result = guard_portfolio_allocation(
                state["candidate_plans"],
                reviews,
                proposal,
                state["market_context"],
                max_gross_target=self.max_gross_target,
                max_positions=self.max_positions,
                high_correlation_threshold=self.high_correlation_threshold,
                cluster_gross_cap=self.cluster_gross_cap,
            )
            payload = node_runner(
                "portfolio.deterministic_guard",
                GuardedPortfolioAllocation,
                lambda: result,
            )
            return {
                "guarded_allocation": payload.model_dump(mode="json"),
                "execution_path": ["portfolio.deterministic_guard"],
            }

        node_kwargs = {"retry_policy": retry}
        builder.add_node("correlation_reviewer", correlation_node, **node_kwargs)
        builder.add_node("concentration_reviewer", concentration_node, **node_kwargs)
        builder.add_node("portfolio_supervisor", supervisor_node, **node_kwargs)
        builder.add_node("deterministic_guard", guard_node)
        builder.add_edge(START, "correlation_reviewer")
        builder.add_edge(START, "concentration_reviewer")
        builder.add_edge(
            ["correlation_reviewer", "concentration_reviewer"],
            "portfolio_supervisor",
        )
        builder.add_edge("portfolio_supervisor", "deterministic_guard")
        builder.add_edge("deterministic_guard", END)
        return builder.compile(
            checkpointer=checkpointer,
            name="tradinglab_portfolio_supervisor_graph",
        )

    @contextmanager
    def _checkpointer(self, path: str | Path | None) -> Any:
        if path is None:
            yield InMemorySaver()
            return
        with open_sqlite_checkpoint_saver(path) as saver:
            yield saver

    def run(
        self,
        candidate_plans: Sequence[Mapping[str, Any]],
        market_context: Mapping[str, Any],
        *,
        thread_id: str,
        node_runner: NodeRunner,
        checkpointer_path: str | Path | None = None,
        resume: bool = False,
    ) -> PortfolioGraphExecution:
        if len(candidate_plans) < 2:
            raise ValueError("portfolio supervisor requires at least two candidate plans")
        initial: PortfolioGraphState = {
            "candidate_plans": [dict(item) for item in candidate_plans],
            "market_context": dict(market_context),
            "execution_path": [],
        }
        config: RunnableConfig = {
            "configurable": {"thread_id": thread_id},
            "tags": ["tradinglab", "portfolio-supervisor-graph"],
            "metadata": {"symbol": "PORTFOLIO", "thread_id": thread_id},
        }
        with self._checkpointer(checkpointer_path) as checkpointer:
            if not resume:
                delete_thread = getattr(checkpointer, "delete_thread", None)
                if callable(delete_thread):
                    delete_thread(thread_id)
            graph = self.build(
                thread_id=thread_id,
                node_runner=node_runner,
                checkpointer=checkpointer,
            )
            graph_input: PortfolioGraphState | None = None if resume else initial
            _append_event(
                self.event_path,
                {
                    "event": "portfolio_graph_start",
                    "thread_id": thread_id,
                    "resume": resume,
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
                    event_count += 1
                    _append_event(
                        self.event_path,
                        {
                            "event": "portfolio_graph_step",
                            "thread_id": thread_id,
                            "nodes": sorted(str(name) for name in update),
                        },
                    )
            except Exception as exc:
                _append_event(
                    self.event_path,
                    {
                        "event": "portfolio_graph_error",
                        "thread_id": thread_id,
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:500],
                    },
                )
                raise
            snapshot = graph.get_state(config)
            state = dict(snapshot.values)
            history = tuple(graph.get_state_history(config))
            result = GuardedPortfolioAllocation.model_validate(
                state["guarded_allocation"]
            )
            proposal = PortfolioSupervisorDecision.model_validate(
                state["supervisor_proposal"]
            )
            reviews = (
                PortfolioCommitteeReview.model_validate(state["correlation_review"]),
                PortfolioCommitteeReview.model_validate(state["concentration_review"]),
            )
            _append_event(
                self.event_path,
                {
                    "event": "portfolio_graph_complete",
                    "thread_id": thread_id,
                    "checkpoint_count": len(history),
                    "step_event_count": event_count,
                },
            )
            return PortfolioGraphExecution(
                result=result,
                proposal=proposal,
                reviews=reviews,
                thread_id=thread_id,
                checkpoint_count=len(history),
                event_count=event_count,
                execution_path=tuple(state.get("execution_path", [])),
                latest_state=state,
            )

    def mermaid(self) -> str:
        with self._checkpointer(None) as checkpointer:
            graph = self.build(
                thread_id="portfolio-preview",
                node_runner=lambda _name, _model, function: function(),
                checkpointer=checkpointer,
            )
            return graph.get_graph().draw_mermaid()


__all__ = [
    "LangGraphPortfolioSupervisorRuntime",
    "PORTFOLIO_SUPERVISOR_CALLS",
    "PortfolioGraphExecution",
    "PortfolioReviewerAgent",
    "PortfolioSupervisorAgent",
    "build_portfolio_market_context",
    "guard_portfolio_allocation",
]
