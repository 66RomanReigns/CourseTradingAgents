from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel


T = TypeVar("T", bound=BaseModel)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_node_name(name: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "_", name.strip())
    if not normalized:
        raise ValueError("workflow node name cannot be empty")
    return normalized


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    temporary.replace(path)


class WorkflowExecutionError(RuntimeError):
    def __init__(self, run_id: str, node: str, cause: Exception):
        self.run_id = run_id
        self.node = node
        self.cause = cause
        super().__init__(
            f"workflow {run_id} failed at node {node}: "
            f"{type(cause).__name__}: {cause}"
        )


class WorkflowStateStore:
    SCHEMA_VERSION = 1

    def __init__(
        self,
        run_dir: str | Path,
        *,
        run_id: str,
        mode: str,
        settings_hash: str,
        resume: bool,
    ) -> None:
        self.run_dir = Path(run_dir)
        self.run_id = run_id
        self.state_path = self.run_dir / "state.json"
        self.nodes_dir = self.run_dir / "nodes"
        if resume:
            if not self.state_path.is_file():
                raise ValueError(f"workflow checkpoint not found: {self.state_path}")
            state = self.state
            if state.get("run_id") != run_id:
                raise ValueError("workflow run_id does not match checkpoint")
            if state.get("mode") != mode:
                raise ValueError("workflow mode does not match checkpoint")
            if state.get("settings_hash") != settings_hash:
                raise ValueError("workflow settings changed; refusing unsafe resume")
            if int(state.get("schema_version", 0)) != self.SCHEMA_VERSION:
                raise ValueError("unsupported workflow checkpoint schema")
        else:
            self.run_dir.mkdir(parents=True, exist_ok=False)
            self.nodes_dir.mkdir(parents=True, exist_ok=True)
            now = _utc_now()
            _atomic_json(
                self.state_path,
                {
                    "schema_version": self.SCHEMA_VERSION,
                    "run_id": run_id,
                    "mode": mode,
                    "settings_hash": settings_hash,
                    "status": "PENDING",
                    "completed_nodes": [],
                    "active_node": None,
                    "failed_node": None,
                    "error": None,
                    "created_at_utc": now,
                    "updated_at_utc": now,
                },
            )

    @property
    def state(self) -> dict[str, Any]:
        payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("workflow state must be a JSON object")
        return payload

    def _write_state(self, state: dict[str, Any]) -> None:
        state["updated_at_utc"] = _utc_now()
        _atomic_json(self.state_path, state)

    def node_path(self, name: str) -> Path:
        return self.nodes_dir / f"{_safe_node_name(name)}.json"

    def has_node(self, name: str) -> bool:
        return name in set(self.state.get("completed_nodes", [])) and self.node_path(name).is_file()

    def load_node(self, name: str) -> Any:
        if not self.has_node(name):
            raise ValueError(f"workflow node is not complete: {name}")
        return json.loads(self.node_path(name).read_text(encoding="utf-8"))

    def _start_node(self, name: str) -> None:
        state = self.state
        state.update(
            {
                "status": "RUNNING",
                "active_node": name,
                "failed_node": None,
                "error": None,
            }
        )
        self._write_state(state)

    def _complete_node(self, name: str, payload: Any) -> None:
        _atomic_json(self.node_path(name), payload)
        state = self.state
        completed = list(dict.fromkeys([*state.get("completed_nodes", []), name]))
        state.update(
            {
                "status": "RUNNING",
                "completed_nodes": completed,
                "active_node": None,
                "failed_node": None,
                "error": None,
            }
        )
        self._write_state(state)

    def _fail_node(self, name: str, exc: Exception) -> None:
        state = self.state
        state.update(
            {
                "status": "FAILED",
                "active_node": None,
                "failed_node": name,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        self._write_state(state)

    def run_json_node(self, name: str, function: Callable[[], Any]) -> Any:
        if self.has_node(name):
            return self.load_node(name)
        self._start_node(name)
        try:
            payload = function()
        except Exception as exc:
            self._fail_node(name, exc)
            raise WorkflowExecutionError(self.run_id, name, exc) from exc
        self._complete_node(name, payload)
        return payload

    def run_model_node(
        self,
        name: str,
        model: type[T],
        function: Callable[[], T],
    ) -> T:
        if self.has_node(name):
            return model.model_validate(self.load_node(name))
        self._start_node(name)
        try:
            result = function()
            payload = result.model_dump(mode="json")
        except Exception as exc:
            self._fail_node(name, exc)
            raise WorkflowExecutionError(self.run_id, name, exc) from exc
        self._complete_node(name, payload)
        return result

    def complete(self, result_path: str | Path) -> None:
        state = self.state
        state.update(
            {
                "status": "COMPLETE",
                "active_node": None,
                "failed_node": None,
                "error": None,
                "result_path": str(result_path),
                "completed_at_utc": _utc_now(),
            }
        )
        self._write_state(state)
