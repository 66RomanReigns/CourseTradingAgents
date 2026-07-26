from __future__ import annotations

import hashlib
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

from tradinglab_agents.data.fallback_router import (
    ProviderFallbackRouter,
    ProviderRequest,
    ProviderRouteResult,
)
from tradinglab_agents.data.provider_quality import QualityGateStatus
from tradinglab_agents.data.provider_registry import DataKind
from tradinglab_agents.storage.langgraph_checkpoints import (
    open_sqlite_checkpoint_saver,
)


PersistResult = Callable[[dict[str, Any]], dict[str, Any]]
_PROVIDER_EVENT_LOCK = threading.Lock()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_event(path: Path | None, payload: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"timestamp_utc": _utc_now(), **payload}
    with _PROVIDER_EVENT_LOCK:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def _request_dict(request: ProviderRequest) -> dict[str, Any]:
    return {
        "request_id": request.request_id,
        "data_kind": request.data_kind.value,
        "resource": request.resource,
        "provider_order": list(request.provider_order),
        "shadow_validate": request.shadow_validate,
        "required": request.required,
        "metadata": dict(request.metadata or {}),
    }


def _request_from_dict(payload: dict[str, Any]) -> ProviderRequest:
    return ProviderRequest(
        request_id=str(payload["request_id"]),
        data_kind=DataKind(str(payload["data_kind"])),
        resource=str(payload["resource"]),
        provider_order=tuple(str(item) for item in payload.get("provider_order", [])),
        shadow_validate=bool(payload.get("shadow_validate", False)),
        required=bool(payload.get("required", True)),
        metadata=dict(payload.get("metadata", {})),
    )


def provider_request_sha256(
    requests: Sequence[ProviderRequest],
    input_context: Mapping[str, Any] | None = None,
) -> str:
    payload = json.dumps(
        {
            "requests": [_request_dict(item) for item in requests],
            "input_context": dict(input_context or {}),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class DataProviderGraphState(TypedDict, total=False):
    input_sha256: str
    requests: list[dict[str, Any]]
    input_context: dict[str, Any]
    request: dict[str, Any]
    route_results: Annotated[list[dict[str, Any]], operator.add]
    persisted_results: list[dict[str, Any]]
    quality_summary: dict[str, Any]
    execution_path: Annotated[list[str], operator.add]


@dataclass(frozen=True)
class DataProviderGraphExecution:
    thread_id: str
    input_sha256: str
    route_results: tuple[dict[str, Any], ...]
    persisted_results: tuple[dict[str, Any], ...]
    quality_summary: dict[str, Any]
    checkpoint_count: int
    event_count: int
    execution_path: tuple[str, ...]
    latest_state: dict[str, Any]


@dataclass
class LangGraphDataProviderRuntime:
    event_path: Path | None = None

    def build(
        self,
        *,
        router: ProviderFallbackRouter,
        persist_result: PersistResult,
        checkpointer: BaseCheckpointSaver[Any] | None = None,
    ) -> Any:
        builder = StateGraph(DataProviderGraphState)

        def prepare(state: DataProviderGraphState) -> DataProviderGraphState:
            requests = state.get("requests", [])
            if not requests:
                raise ValueError("data provider graph requires at least one request")
            identifiers = [str(item.get("request_id", "")) for item in requests]
            if any(not item for item in identifiers):
                raise ValueError("data provider request_id cannot be empty")
            if len(set(identifiers)) != len(identifiers):
                raise ValueError("duplicate data provider request_id")
            return {"execution_path": ["provider.prepare"]}

        def dispatch(state: DataProviderGraphState) -> list[Send]:
            return [
                Send("route_request", {"request": dict(request)})
                for request in state["requests"]
            ]

        def route_request(state: DataProviderGraphState) -> DataProviderGraphState:
            request = _request_from_dict(dict(state["request"]))
            result = router.route(request)
            return {
                "route_results": [result.as_dict(include_records=True)],
                "execution_path": [f"provider.route.{request.request_id}"],
            }

        def persist_and_gate(state: DataProviderGraphState) -> DataProviderGraphState:
            route_results = list(state.get("route_results", []))
            expected = sorted(str(item["request_id"]) for item in state["requests"])
            actual = sorted(
                str(item.get("request", {}).get("request_id", ""))
                for item in route_results
            )
            if expected != actual:
                raise ValueError(
                    "data provider graph result mismatch: "
                    f"expected={expected}, actual={actual}"
                )
            assessments = [dict(item["quality"]) for item in route_results]
            blocked = sorted(
                str(item["resource"])
                for item in assessments
                if item["status"] == QualityGateStatus.BLOCKED.value
            )
            degraded = sorted(
                str(item["resource"])
                for item in assessments
                if item["status"] == QualityGateStatus.DEGRADED.value
            )
            status = (
                QualityGateStatus.BLOCKED.value
                if blocked
                else QualityGateStatus.DEGRADED.value
                if degraded
                else QualityGateStatus.NORMAL.value
            )
            quality = {
                "status": status,
                "request_count": len(assessments),
                "minimum_score": min(float(item["score"]) for item in assessments),
                "mean_score": sum(float(item["score"]) for item in assessments)
                / len(assessments),
                "allow_position_increase": all(
                    bool(item["allow_position_increase"])
                    for item in assessments
                ),
                "requires_human_review": any(
                    bool(item["requires_human_review"])
                    for item in assessments
                ),
                "blocked_resources": blocked,
                "degraded_resources": degraded,
                "assessments": assessments,
            }
            if blocked:
                raise ValueError(
                    "data provider quality gate blocked persistence: "
                    f"resources={blocked}"
                )
            persisted = [persist_result(dict(item)) for item in route_results]
            return {
                "persisted_results": persisted,
                "quality_summary": quality,
                "execution_path": ["provider.persist_and_quality_gate"],
            }

        builder.add_node("prepare", prepare)
        builder.add_node("route_request", route_request)
        builder.add_node("persist_and_gate", persist_and_gate)
        builder.add_edge(START, "prepare")
        builder.add_conditional_edges("prepare", dispatch, ["route_request"])
        builder.add_edge("route_request", "persist_and_gate")
        builder.add_edge("persist_and_gate", END)
        return builder.compile(
            checkpointer=checkpointer,
            name="tradinglab_data_provider_graph",
        )

    @contextmanager
    def _checkpointer(self, path: str | Path | None):
        if path is None:
            yield InMemorySaver()
            return
        with open_sqlite_checkpoint_saver(path) as saver:
            yield saver

    def run(
        self,
        *,
        thread_id: str,
        requests: Sequence[ProviderRequest],
        router: ProviderFallbackRouter,
        persist_result: PersistResult,
        checkpointer_path: str | Path | None = None,
        resume: bool = False,
        input_context: Mapping[str, Any] | None = None,
    ) -> DataProviderGraphExecution:
        resolved_context = dict(input_context or router.fingerprint_context())
        input_sha256 = provider_request_sha256(requests, resolved_context)
        config: RunnableConfig = {
            "configurable": {"thread_id": thread_id},
            "tags": ["tradinglab", "data-provider-graph"],
            "metadata": {
                "thread_id": thread_id,
                "graph_type": "data_provider",
                "input_sha256": input_sha256,
            },
        }
        with self._checkpointer(checkpointer_path) as checkpointer:
            get_tuple = getattr(checkpointer, "get_tuple", None)
            existing = get_tuple(config) if callable(get_tuple) else None
            checkpoint_exists = existing is not None
            if resume and checkpoint_exists:
                existing_values = dict(existing.checkpoint.get("channel_values", {}))
                previous_hash = existing_values.get("input_sha256")
                if previous_hash and previous_hash != input_sha256:
                    raise ValueError(
                        "data provider resume input mismatch: "
                        f"checkpoint={previous_hash}, current={input_sha256}"
                    )
            effective_resume = resume and checkpoint_exists
            if not effective_resume:
                delete_thread = getattr(checkpointer, "delete_thread", None)
                if callable(delete_thread):
                    delete_thread(thread_id)
            graph = self.build(
                router=router,
                persist_result=persist_result,
                checkpointer=checkpointer,
            )
            _append_event(
                self.event_path,
                {
                    "event": "data_provider_graph_start",
                    "thread_id": thread_id,
                    "resume_requested": resume,
                    "resume": effective_resume,
                    "request_count": len(requests),
                    "input_sha256": input_sha256,
                },
            )
            event_count = 0
            try:
                graph_input: DataProviderGraphState | None = (
                    None
                    if effective_resume
                    else {
                        "input_sha256": input_sha256,
                        "requests": [_request_dict(item) for item in requests],
                        "input_context": resolved_context,
                        "route_results": [],
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
                            "event": "data_provider_graph_step",
                            "thread_id": thread_id,
                            "nodes": sorted(str(name) for name in update),
                        },
                    )
            except Exception as exc:
                _append_event(
                    self.event_path,
                    {
                        "event": "data_provider_graph_error",
                        "thread_id": thread_id,
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:500],
                    },
                )
                raise
            snapshot = graph.get_state(config)
            state = dict(snapshot.values)
            history = tuple(graph.get_state_history(config))
            route_results = tuple(dict(item) for item in state["route_results"])
            persisted = tuple(dict(item) for item in state["persisted_results"])
            quality = dict(state["quality_summary"])
            _append_event(
                self.event_path,
                {
                    "event": "data_provider_graph_complete",
                    "thread_id": thread_id,
                    "checkpoint_count": len(history),
                    "step_event_count": event_count,
                    "quality_status": quality.get("status"),
                    "allow_position_increase": quality.get(
                        "allow_position_increase"
                    ),
                },
            )
            return DataProviderGraphExecution(
                thread_id=thread_id,
                input_sha256=input_sha256,
                route_results=route_results,
                persisted_results=persisted,
                quality_summary=quality,
                checkpoint_count=len(history),
                event_count=event_count,
                execution_path=tuple(state.get("execution_path", [])),
                latest_state=state,
            )

    def mermaid(self) -> str:
        class _NoopRouter:
            def route(self, request: ProviderRequest) -> ProviderRouteResult:
                raise RuntimeError(f"not executed: {request.request_id}")

        with self._checkpointer(None) as checkpointer:
            graph = self.build(
                router=_NoopRouter(),  # type: ignore[arg-type]
                persist_result=lambda payload: payload,
                checkpointer=checkpointer,
            )
            return graph.get_graph().draw_mermaid()


__all__ = [
    "DataProviderGraphExecution",
    "DataProviderGraphState",
    "LangGraphDataProviderRuntime",
    "provider_request_sha256",
]
