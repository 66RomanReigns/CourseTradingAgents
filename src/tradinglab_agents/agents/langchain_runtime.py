from __future__ import annotations

import json
import os
import time
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar
from uuid import UUID, uuid4

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.prompts import PromptTemplate
from langchain_core.runnables import RunnableConfig, RunnableLambda
from pydantic import BaseModel

from tradinglab_agents.agents.llm import StructuredLLMClient


TModel = TypeVar("TModel", bound=BaseModel)
_TRACE_LOCK = threading.Lock()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SecretFreeLangChainTraceHandler(BaseCallbackHandler):
    """Persist runnable lifecycle metadata without prompts, payloads or outputs."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._started: dict[UUID, float] = {}

    def _append(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = {"timestamp_utc": _utc_now(), **payload}
        with _TRACE_LOCK:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def record_role_start(
        self,
        *,
        event_id: str,
        task: str,
        tier: str,
        symbol: str | None,
        thread_id: str | None,
    ) -> float:
        started = time.perf_counter()
        self._append(
            {
                "event": "role_start",
                "event_id": event_id,
                "task": task,
                "tier": tier,
                "symbol": symbol,
                "thread_id": thread_id,
            }
        )
        return started

    def record_role_end(self, *, event_id: str, started: float) -> None:
        self._append(
            {
                "event": "role_end",
                "event_id": event_id,
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
            }
        )

    def record_role_error(
        self,
        *,
        event_id: str,
        started: float,
        error: BaseException,
    ) -> None:
        self._append(
            {
                "event": "role_error",
                "event_id": event_id,
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                "error_type": type(error).__name__,
                "error": str(error)[:500],
            }
        )

    def on_chain_start(
        self,
        serialized: dict[str, Any],
        inputs: dict[str, Any],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        del inputs, kwargs
        tag_list = list(tags or ())
        serialized_name = serialized.get("name") if serialized else None
        is_role_root = (
            "structured-output" in tag_list
            and not any(tag.startswith("seq:step:") for tag in tag_list)
            and serialized_name is None
        )
        if not is_role_root:
            return
        self._started[run_id] = time.perf_counter()
        safe_metadata = {
            key: value
            for key, value in (metadata or {}).items()
            if key in {"task", "tier", "symbol", "thread_id", "graph_node"}
        }
        self._append(
            {
                "event": "chain_start",
                "run_id": str(run_id),
                "tags": tag_list,
                "metadata": safe_metadata,
            }
        )

    def on_chain_end(
        self,
        outputs: dict[str, Any],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        del outputs, parent_run_id, kwargs
        started = self._started.pop(run_id, None)
        if started is None:
            return
        self._append(
            {
                "event": "chain_end",
                "run_id": str(run_id),
                "duration_ms": (
                    round((time.perf_counter() - started) * 1000, 3)
                    if started is not None
                    else None
                ),
            }
        )

    def on_chain_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        del parent_run_id, kwargs
        started = self._started.pop(run_id, None)
        if started is None:
            return
        self._append(
            {
                "event": "chain_error",
                "run_id": str(run_id),
                "duration_ms": (
                    round((time.perf_counter() - started) * 1000, 3)
                    if started is not None
                    else None
                ),
                "error_type": type(error).__name__,
                "error": str(error)[:500],
            }
        )


@dataclass
class LangChainStructuredClient:
    """Adapt the existing structured client contract to LangChain runnables.

    The project keeps its provider-neutral, Pydantic-validated client and uses
    LangChain for prompt composition, runnable metadata, callbacks and future
    middleware integration. This avoids binding portfolio logic to one model SDK.
    """

    delegate: StructuredLLMClient
    trace_path: Path
    tier: str
    symbol: str | None = None
    thread_id: str | None = None
    _handler: SecretFreeLangChainTraceHandler = field(init=False, repr=False)
    _prompt: PromptTemplate = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.tier not in {"quick", "deep"}:
            raise ValueError("LangChain client tier must be quick or deep")
        self._handler = SecretFreeLangChainTraceHandler(self.trace_path)
        self._prompt = PromptTemplate.from_template(
            "{system_prompt}\n\n"
            "LangChain role task: {task}\n"
            "Required Pydantic schema: {schema_name}\n"
            "Return only a schema-valid JSON object."
        )

    @property
    def identity(self) -> str:
        return f"langchain:{self.tier}:{self.delegate.identity}"

    @property
    def last_call_metadata(self) -> dict[str, Any]:
        value = getattr(self.delegate, "last_call_metadata", {})
        return dict(value) if isinstance(value, dict) else {}

    @property
    def last_metadata(self) -> dict[str, Any]:
        value = getattr(self.delegate, "last_metadata", {})
        return dict(value) if isinstance(value, dict) else {}

    def complete(
        self,
        *,
        task: str,
        system_prompt: str,
        payload: dict[str, Any],
        response_model: type[TModel],
    ) -> TModel:
        def render(request: dict[str, Any]) -> dict[str, Any]:
            prompt_value = self._prompt.invoke(
                {
                    "system_prompt": request["system_prompt"],
                    "task": request["task"],
                    "schema_name": request["response_model"].__name__,
                }
            )
            return {
                **request,
                "rendered_system_prompt": prompt_value.to_string(),
            }

        def invoke(request: dict[str, Any]) -> TModel:
            return self.delegate.complete(
                task=request["task"],
                system_prompt=request["rendered_system_prompt"],
                payload=request["payload"],
                response_model=request["response_model"],
            )

        chain = (
            RunnableLambda(render, name=f"render_{task}")
            | RunnableLambda(invoke, name=f"invoke_{task}")
        ).with_config(run_name=f"tradinglab_{task}")
        config: RunnableConfig = {
            "tags": ["tradinglab", "structured-output", self.tier, task],
            "metadata": {
                "task": task,
                "tier": self.tier,
                "symbol": self.symbol,
                "thread_id": self.thread_id,
                "graph_node": task,
            },
        }
        event_id = str(uuid4())
        started = self._handler.record_role_start(
            event_id=event_id,
            task=task,
            tier=self.tier,
            symbol=self.symbol,
            thread_id=self.thread_id,
        )
        try:
            result = chain.invoke(
                {
                    "task": task,
                    "system_prompt": system_prompt,
                    "payload": payload,
                    "response_model": response_model,
                },
                config=config,
            )
        except Exception as exc:
            self._handler.record_role_error(
                event_id=event_id,
                started=started,
                error=exc,
            )
            raise
        self._handler.record_role_end(event_id=event_id, started=started)
        return result


def langsmith_enabled() -> bool:
    return os.environ.get("LANGSMITH_TRACING", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


__all__ = [
    "LangChainStructuredClient",
    "SecretFreeLangChainTraceHandler",
    "langsmith_enabled",
]
