from __future__ import annotations

import json
import operator
import threading
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from tradinglab_agents.storage.langgraph_checkpoints import (
    open_sqlite_checkpoint_saver,
)


CandidateSelector = Callable[[], Sequence[Mapping[str, Any]]]
SymbolResearchRunner = Callable[[Mapping[str, Any]], Mapping[str, Any]]
PortfolioRunner = Callable[[Sequence[Mapping[str, Any]]], Mapping[str, Any]]
AuditRunner = Callable[[str, Callable[[], Any]], Any]
_PARENT_EVENT_LOCK = threading.Lock()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_event(path: Path | None, payload: Mapping[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"timestamp_utc": _utc_now(), **dict(payload)}
    with _PARENT_EVENT_LOCK:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


class ParentResearchState(TypedDict, total=False):
    candidates: list[dict[str, Any]]
    candidate_results: Annotated[list[dict[str, Any]], operator.add]
    portfolio_summary: dict[str, Any]
    execution_path: Annotated[list[str], operator.add]


class CandidateTaskState(TypedDict):
    candidate: dict[str, Any]


@dataclass(frozen=True)
class ParentResearchExecution:
    thread_id: str
    candidates: tuple[dict[str, Any], ...]
    candidate_results: tuple[dict[str, Any], ...]
    portfolio_summary: dict[str, Any]
    checkpoint_count: int
    event_count: int
    execution_path: tuple[str, ...]
    latest_state: dict[str, Any]


@dataclass
class LangGraphResearchParentRuntime:
    """Top-level research orchestration over existing symbol and portfolio graphs.

    The parent graph owns dynamic candidate fan-out and the portfolio fan-in.
    Existing symbol and portfolio runtimes remain independently checkpointed
    sub-workflows during the staged v0.14 migration.
    """

    event_path: Path | None = None

    @staticmethod
    def _normalize_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
        symbol = str(candidate.get("symbol", "")).strip().upper()
        if not symbol:
            raise ValueError("candidate symbol cannot be empty")
        priority = float(candidate.get("priority", 0.0))
        if priority < 0.0:
            raise ValueError("candidate priority cannot be negative")
        return {"symbol": symbol, "priority": priority}

    def build(
        self,
        *,
        candidate_selector: CandidateSelector,
        symbol_runner: SymbolResearchRunner,
        portfolio_runner: PortfolioRunner,
        audit_runner: AuditRunner,
        checkpointer: BaseCheckpointSaver[Any] | None = None,
    ) -> Any:
        builder = StateGraph(ParentResearchState)

        def candidate_screen(_: ParentResearchState) -> ParentResearchState:
            raw = audit_runner("candidate_screen", lambda: list(candidate_selector()))
            candidates = [self._normalize_candidate(item) for item in raw]
            if not candidates:
                raise ValueError("candidate screen returned no symbols")
            symbols = [item["symbol"] for item in candidates]
            if len(set(symbols)) != len(symbols):
                raise ValueError("candidate screen returned duplicate symbols")
            return {
                "candidates": candidates,
                "candidate_results": [],
                "execution_path": ["parent.candidate_screen"],
            }

        def fan_out(state: ParentResearchState) -> list[Send]:
            return [
                Send("symbol_research", {"candidate": dict(candidate)})
                for candidate in state["candidates"]
            ]

        def symbol_research(state: CandidateTaskState) -> ParentResearchState:
            candidate = self._normalize_candidate(state["candidate"])
            symbol = candidate["symbol"]
            result = dict(symbol_runner(candidate))
            result = audit_runner(
                f"research.parent.symbol.{symbol}",
                lambda: result,
            )
            returned_symbol = str(result.get("symbol", symbol)).upper()
            if returned_symbol != symbol:
                raise ValueError(
                    f"symbol research mismatch: expected {symbol}, got {returned_symbol}"
                )
            normalized = {**dict(result), "symbol": symbol}
            return {
                "candidate_results": [normalized],
                "execution_path": [f"parent.symbol.{symbol}"],
            }

        def portfolio_supervisor(state: ParentResearchState) -> ParentResearchState:
            results = sorted(
                (dict(item) for item in state.get("candidate_results", [])),
                key=lambda item: str(item["symbol"]),
            )
            expected = sorted(item["symbol"] for item in state["candidates"])
            actual = sorted(str(item["symbol"]).upper() for item in results)
            if actual != expected:
                raise ValueError(
                    "parent graph candidate fan-in mismatch: "
                    f"expected={expected}, actual={actual}"
                )
            summary = dict(portfolio_runner(results))
            summary = audit_runner(
                "research.parent.portfolio_supervisor",
                lambda: summary,
            )
            return {
                "portfolio_summary": summary,
                "execution_path": ["parent.portfolio_supervisor"],
            }

        builder.add_node("candidate_screen", candidate_screen)
        builder.add_node("symbol_research", symbol_research)
        builder.add_node("portfolio_supervisor", portfolio_supervisor)
        builder.add_edge(START, "candidate_screen")
        builder.add_conditional_edges("candidate_screen", fan_out)
        builder.add_edge("symbol_research", "portfolio_supervisor")
        builder.add_edge("portfolio_supervisor", END)
        return builder.compile(
            checkpointer=checkpointer,
            name="tradinglab_research_parent_graph",
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
        *,
        thread_id: str,
        candidate_selector: CandidateSelector,
        symbol_runner: SymbolResearchRunner,
        portfolio_runner: PortfolioRunner,
        audit_runner: AuditRunner,
        checkpointer_path: str | Path | None = None,
        resume: bool = False,
    ) -> ParentResearchExecution:
        config: RunnableConfig = {
            "configurable": {"thread_id": thread_id},
            "tags": ["tradinglab", "research-parent-graph"],
            "metadata": {"thread_id": thread_id, "graph_type": "research_parent"},
        }
        with self._checkpointer(checkpointer_path) as checkpointer:
            if not resume:
                delete_thread = getattr(checkpointer, "delete_thread", None)
                if callable(delete_thread):
                    delete_thread(thread_id)
            graph = self.build(
                candidate_selector=candidate_selector,
                symbol_runner=symbol_runner,
                portfolio_runner=portfolio_runner,
                audit_runner=audit_runner,
                checkpointer=checkpointer,
            )
            _append_event(
                self.event_path,
                {
                    "event": "parent_graph_start",
                    "thread_id": thread_id,
                    "resume": resume,
                },
            )
            event_count = 0
            try:
                graph_input: ParentResearchState | None = None if resume else {}
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
                            "event": "parent_graph_step",
                            "thread_id": thread_id,
                            "nodes": sorted(str(name) for name in update),
                        },
                    )
            except Exception as exc:
                _append_event(
                    self.event_path,
                    {
                        "event": "parent_graph_error",
                        "thread_id": thread_id,
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:500],
                    },
                )
                raise
            snapshot = graph.get_state(config)
            state = dict(snapshot.values)
            history = tuple(graph.get_state_history(config))
            candidates = tuple(dict(item) for item in state["candidates"])
            results = tuple(
                sorted(
                    (dict(item) for item in state["candidate_results"]),
                    key=lambda item: str(item["symbol"]),
                )
            )
            portfolio = dict(state["portfolio_summary"])
            _append_event(
                self.event_path,
                {
                    "event": "parent_graph_complete",
                    "thread_id": thread_id,
                    "checkpoint_count": len(history),
                    "step_event_count": event_count,
                    "candidate_count": len(candidates),
                },
            )
            return ParentResearchExecution(
                thread_id=thread_id,
                candidates=candidates,
                candidate_results=results,
                portfolio_summary=portfolio,
                checkpoint_count=len(history),
                event_count=event_count,
                execution_path=tuple(state.get("execution_path", [])),
                latest_state=state,
            )

    def mermaid(self) -> str:
        with self._checkpointer(None) as checkpointer:
            graph = self.build(
                candidate_selector=lambda: [{"symbol": "DEMO", "priority": 1.0}],
                symbol_runner=lambda candidate: {
                    "symbol": candidate["symbol"],
                    "result": {},
                },
                portfolio_runner=lambda _results: {"status": "preview"},
                audit_runner=lambda _name, function: function(),
                checkpointer=checkpointer,
            )
            return graph.get_graph().draw_mermaid()


__all__ = [
    "LangGraphResearchParentRuntime",
    "ParentResearchExecution",
    "ParentResearchState",
]
