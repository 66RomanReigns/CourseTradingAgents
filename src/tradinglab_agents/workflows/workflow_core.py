from __future__ import annotations

import json
import operator
import threading
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from tradinglab_agents.storage.langgraph_checkpoints import (
    open_sqlite_checkpoint_saver,
)


DataValidator = Callable[[], Mapping[str, Any]]
ResearchRunner = Callable[[], Mapping[str, Any]]
OverlayAssembler = Callable[[Mapping[str, Any]], Mapping[str, Mapping[str, Any]]]
DecisionPreparer = Callable[
    [Mapping[str, Any], Mapping[str, Any], Mapping[str, Mapping[str, Any]]],
    Mapping[str, Any],
]
AuditRunner = Callable[[str, Callable[[], Any]], Any]
_CORE_EVENT_LOCK = threading.Lock()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_event(path: Path | None, payload: Mapping[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"timestamp_utc": _utc_now(), **dict(payload)}
    with _CORE_EVENT_LOCK:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


class WorkflowCoreState(TypedDict, total=False):
    research_enabled: bool
    data_validation: dict[str, Any]
    research: dict[str, Any]
    research_overlays: dict[str, dict[str, Any]]
    decision_preparation: dict[str, Any]
    execution_path: Annotated[list[str], operator.add]


@dataclass(frozen=True)
class WorkflowCoreExecution:
    thread_id: str
    data_validation: dict[str, Any]
    research: dict[str, Any]
    research_overlays: dict[str, dict[str, Any]]
    decision_preparation: dict[str, Any]
    checkpoint_count: int
    event_count: int
    execution_path: tuple[str, ...]
    latest_state: dict[str, Any]


@dataclass
class LangGraphWorkflowCoreRuntime:
    """Persistent core workflow around validation, research and decision inputs.

    Provider refresh and paper execution intentionally remain outside this graph
    in v0.15. The graph owns only deterministic local validation, the already
    checkpointed research-parent workflow, overlay construction and validation
    of the payload that may later be passed to the paper service.
    """

    event_path: Path | None = None

    def build(
        self,
        *,
        data_validator: DataValidator,
        research_runner: ResearchRunner,
        overlay_assembler: OverlayAssembler,
        decision_preparer: DecisionPreparer,
        audit_runner: AuditRunner,
        checkpointer: BaseCheckpointSaver[Any] | None = None,
    ) -> Any:
        builder = StateGraph(WorkflowCoreState)

        def validate_data(_: WorkflowCoreState) -> WorkflowCoreState:
            payload = dict(
                audit_runner("workflow.core.data_validation", data_validator)
            )
            status = str(payload.get("status", "")).lower()
            if status not in {"completed", "not_required"}:
                raise ValueError("workflow core data validation did not complete")
            if status == "completed" and int(payload.get("market_files", 0)) < 1:
                raise ValueError("workflow core validation found no market files")
            return {
                "data_validation": payload,
                "execution_path": ["core.data_validation"],
            }

        def research_route(state: WorkflowCoreState) -> str:
            return "research" if bool(state.get("research_enabled", True)) else "skip"

        def run_research(_: WorkflowCoreState) -> WorkflowCoreState:
            # Execute the nested research graph before the outer audit wrapper so
            # a deep WorkflowExecutionError keeps its original agent node name.
            payload = dict(research_runner())
            payload = dict(
                audit_runner(
                    "workflow.core.research_parent",
                    lambda: payload,
                )
            )
            candidates = payload.get("candidates")
            if not isinstance(candidates, Mapping) or not candidates:
                raise ValueError("workflow core research returned no candidates")
            return {
                "research": payload,
                "execution_path": ["core.research_parent"],
            }

        def skip_research(_: WorkflowCoreState) -> WorkflowCoreState:
            return {
                "research": {
                    "status": "disabled",
                    "candidate_count": 0,
                    "candidates": {},
                },
                "execution_path": ["core.research_disabled"],
            }

        def assemble_overlays(state: WorkflowCoreState) -> WorkflowCoreState:
            research = dict(state.get("research", {}))
            payload = {
                str(symbol).upper(): dict(overlay)
                for symbol, overlay in dict(
                    audit_runner(
                        "workflow.core.overlay_assembly",
                        lambda: overlay_assembler(research),
                    )
                ).items()
            }
            candidates = research.get("candidates", {})
            expected = (
                sorted(str(symbol).upper() for symbol in candidates)
                if isinstance(candidates, Mapping)
                else []
            )
            actual = sorted(payload)
            if expected != actual:
                raise ValueError(
                    "workflow core overlay symbol mismatch: "
                    f"expected={expected}, actual={actual}"
                )
            return {
                "research_overlays": payload,
                "execution_path": ["core.overlay_assembly"],
            }

        def prepare_decisions(state: WorkflowCoreState) -> WorkflowCoreState:
            payload = dict(
                audit_runner(
                    "workflow.core.decision_preparation",
                    lambda: decision_preparer(
                        dict(state["data_validation"]),
                        dict(state.get("research", {})),
                        {
                            str(symbol): dict(overlay)
                            for symbol, overlay in state.get(
                                "research_overlays", {}
                            ).items()
                        },
                    ),
                )
            )
            if str(payload.get("status", "")).lower() not in {"ready", "no_research"}:
                raise ValueError("workflow core decision preparation is not ready")
            if bool(payload.get("external_broker", True)):
                raise ValueError("workflow core cannot enable an external broker")
            if not bool(payload.get("safe_for_paper_input", False)):
                raise ValueError("workflow core decision preparation failed safety checks")
            return {
                "decision_preparation": payload,
                "execution_path": ["core.decision_preparation"],
            }

        builder.add_node("data_validation", validate_data)
        builder.add_node("research_parent", run_research)
        builder.add_node("research_disabled", skip_research)
        builder.add_node("overlay_assembly", assemble_overlays)
        builder.add_node("decision_preparation", prepare_decisions)
        builder.add_edge(START, "data_validation")
        builder.add_conditional_edges(
            "data_validation",
            research_route,
            {
                "research": "research_parent",
                "skip": "research_disabled",
            },
        )
        builder.add_edge("research_parent", "overlay_assembly")
        builder.add_edge("research_disabled", "overlay_assembly")
        builder.add_edge("overlay_assembly", "decision_preparation")
        builder.add_edge("decision_preparation", END)
        return builder.compile(
            checkpointer=checkpointer,
            name="tradinglab_workflow_core_graph",
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
        research_enabled: bool,
        data_validator: DataValidator,
        research_runner: ResearchRunner,
        overlay_assembler: OverlayAssembler,
        decision_preparer: DecisionPreparer,
        audit_runner: AuditRunner,
        checkpointer_path: str | Path | None = None,
        resume: bool = False,
    ) -> WorkflowCoreExecution:
        config: RunnableConfig = {
            "configurable": {"thread_id": thread_id},
            "tags": ["tradinglab", "workflow-core-graph"],
            "metadata": {"thread_id": thread_id, "graph_type": "workflow_core"},
        }
        with self._checkpointer(checkpointer_path) as checkpointer:
            get_tuple = getattr(checkpointer, "get_tuple", None)
            checkpoint_exists = bool(
                callable(get_tuple) and get_tuple(config) is not None
            )
            effective_resume = resume and checkpoint_exists
            if not effective_resume:
                delete_thread = getattr(checkpointer, "delete_thread", None)
                if callable(delete_thread):
                    delete_thread(thread_id)
            graph = self.build(
                data_validator=data_validator,
                research_runner=research_runner,
                overlay_assembler=overlay_assembler,
                decision_preparer=decision_preparer,
                audit_runner=audit_runner,
                checkpointer=checkpointer,
            )
            _append_event(
                self.event_path,
                {
                    "event": "workflow_core_start",
                    "thread_id": thread_id,
                    "resume_requested": resume,
                    "resume": effective_resume,
                    "research_enabled": research_enabled,
                },
            )
            event_count = 0
            try:
                graph_input: WorkflowCoreState | None = (
                    None
                    if effective_resume
                    else {
                        "research_enabled": research_enabled,
                        "execution_path": [],
                    }
                )
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
                            "event": "workflow_core_step",
                            "thread_id": thread_id,
                            "nodes": sorted(str(name) for name in update),
                        },
                    )
            except Exception as exc:
                _append_event(
                    self.event_path,
                    {
                        "event": "workflow_core_error",
                        "thread_id": thread_id,
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:500],
                    },
                )
                raise
            snapshot = graph.get_state(config)
            state = dict(snapshot.values)
            history = tuple(graph.get_state_history(config))
            validation = dict(state["data_validation"])
            research = dict(state["research"])
            overlays = {
                str(symbol): dict(overlay)
                for symbol, overlay in state.get("research_overlays", {}).items()
            }
            preparation = dict(state["decision_preparation"])
            _append_event(
                self.event_path,
                {
                    "event": "workflow_core_complete",
                    "thread_id": thread_id,
                    "checkpoint_count": len(history),
                    "step_event_count": event_count,
                    "overlay_count": len(overlays),
                },
            )
            return WorkflowCoreExecution(
                thread_id=thread_id,
                data_validation=validation,
                research=research,
                research_overlays=overlays,
                decision_preparation=preparation,
                checkpoint_count=len(history),
                event_count=event_count,
                execution_path=tuple(state.get("execution_path", [])),
                latest_state=state,
            )

    def mermaid(self) -> str:
        with self._checkpointer(None) as checkpointer:
            graph = self.build(
                data_validator=lambda: {
                    "status": "completed",
                    "market_files": 1,
                },
                research_runner=lambda: {
                    "status": "completed",
                    "candidates": {"DEMO": {"result": {}}},
                },
                overlay_assembler=lambda _research: {
                    "DEMO": {
                        "action": "HOLD",
                        "target_weight": 0.0,
                        "requires_human_approval": True,
                    }
                },
                decision_preparer=lambda _validation, _research, _overlays: {
                    "status": "ready",
                    "safe_for_paper_input": True,
                    "external_broker": False,
                },
                audit_runner=lambda _name, function: function(),
                checkpointer=checkpointer,
            )
            return graph.get_graph().draw_mermaid()


__all__ = [
    "LangGraphWorkflowCoreRuntime",
    "WorkflowCoreExecution",
    "WorkflowCoreState",
]
