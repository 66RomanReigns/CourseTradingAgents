from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, TypeVar
from uuid import uuid4

from tradinglab_agents.data.http_client import DataApiError


T = TypeVar("T")


class ProviderState(StrEnum):
    LIVE_OK = "LIVE_OK"
    CACHE_HIT = "CACHE_HIT"
    DEGRADED_STALE = "DEGRADED_STALE"
    NETWORK_BLOCKED = "NETWORK_BLOCKED"
    RATE_LIMITED = "RATE_LIMITED"
    AUTH_FAILED = "AUTH_FAILED"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    FAILED = "FAILED"
    DRY_RUN = "DRY_RUN"
    SKIPPED = "SKIPPED"


def classify_provider_exception(exc: Exception) -> ProviderState:
    text = f"{type(exc).__name__}: {exc}".lower()
    if any(token in text for token in ("captive_portal", "captive portal", "tls_intercepted")):
        return ProviderState.NETWORK_BLOCKED
    if any(token in text for token in ("certificate verify", "ssl", "network is unreachable", "connection timed out")):
        return ProviderState.NETWORK_BLOCKED
    if "http 429" in text or "rate limit" in text or "frequency" in text and "limit" in text:
        return ProviderState.RATE_LIMITED
    if any(token in text for token in ("http 401", "http 403", "invalid api key", "api key is invalid", "unauthorized")):
        return ProviderState.AUTH_FAILED
    if isinstance(exc, DataApiError):
        return ProviderState.PROVIDER_ERROR
    return ProviderState.FAILED


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_error(exc: Exception) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return text[:2000]


@dataclass(frozen=True)
class ProviderCallRecord:
    call_id: str
    run_id: str
    provider: str
    operation: str
    resource: str
    status: ProviderState
    units: float
    started_at_utc: str
    completed_at_utc: str
    duration_ms: float
    error: str | None
    detail: dict[str, Any]


class ProviderUsageStore:
    """Secret-free persistent ledger for external provider calls and budgets."""

    SCHEMA_VERSION = 1

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > self.SCHEMA_VERSION:
                raise RuntimeError(
                    f"provider usage database schema {version} is newer than supported {self.SCHEMA_VERSION}"
                )
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS provider_calls (
                    call_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    resource TEXT NOT NULL,
                    status TEXT NOT NULL,
                    units REAL NOT NULL,
                    started_at_utc TEXT NOT NULL,
                    completed_at_utc TEXT NOT NULL,
                    duration_ms REAL NOT NULL,
                    error TEXT,
                    detail_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_provider_calls_run
                    ON provider_calls(run_id, started_at_utc);
                CREATE INDEX IF NOT EXISTS idx_provider_calls_provider_time
                    ON provider_calls(provider, started_at_utc);
                """
            )
            connection.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")

    def record(self, record: ProviderCallRecord) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO provider_calls (
                    call_id, run_id, provider, operation, resource, status,
                    units, started_at_utc, completed_at_utc, duration_ms,
                    error, detail_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.call_id,
                    record.run_id,
                    record.provider,
                    record.operation,
                    record.resource,
                    record.status.value,
                    float(record.units),
                    record.started_at_utc,
                    record.completed_at_utc,
                    float(record.duration_ms),
                    record.error,
                    json.dumps(record.detail, ensure_ascii=False, sort_keys=True, default=str),
                ),
            )

    def units_since(
        self,
        providers: Sequence[str],
        since_utc: datetime,
    ) -> float:
        normalized = tuple(
            sorted({str(item).strip().lower() for item in providers if str(item).strip()})
        )
        if not normalized:
            return 0.0
        if since_utc.tzinfo is None:
            raise ValueError("since_utc must be timezone-aware")
        placeholders = ",".join("?" for _ in normalized)
        with self._connection() as connection:
            row = connection.execute(
                f"""
                SELECT SUM(units) AS units
                FROM provider_calls
                WHERE provider IN ({placeholders})
                  AND started_at_utc >= ?
                """,
                (*normalized, since_utc.astimezone(timezone.utc).isoformat()),
            ).fetchone()
        return float(row["units"] or 0.0)

    def summary(self, *, run_id: str | None = None) -> dict[str, Any]:
        clauses: list[str] = []
        params: list[Any] = []
        if run_id:
            clauses.append("run_id = ?")
            params.append(run_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT provider, status, COUNT(*) AS calls, SUM(units) AS units,
                       SUM(duration_ms) AS duration_ms
                FROM provider_calls {where}
                GROUP BY provider, status
                ORDER BY provider, status
                """,
                params,
            ).fetchall()
        return {
            "run_id": run_id,
            "providers": [
                {
                    "provider": row["provider"],
                    "status": row["status"],
                    "calls": int(row["calls"]),
                    "units": float(row["units"] or 0.0),
                    "duration_ms": float(row["duration_ms"] or 0.0),
                }
                for row in rows
            ],
        }


class ProviderCallTracker:
    def __init__(self, store: ProviderUsageStore, run_id: str):
        self.store = store
        self.run_id = run_id

    def call(
        self,
        *,
        provider: str,
        operation: str,
        resource: str,
        units: float,
        function: Callable[[], T],
        detail: Mapping[str, Any] | None = None,
    ) -> T:
        started_at = _utc_now()
        started = time.perf_counter()
        try:
            result = function()
        except Exception as exc:
            completed_at = _utc_now()
            self.store.record(
                ProviderCallRecord(
                    call_id=f"call-{uuid4().hex}",
                    run_id=self.run_id,
                    provider=provider,
                    operation=operation,
                    resource=resource,
                    status=classify_provider_exception(exc),
                    units=float(units),
                    started_at_utc=started_at.isoformat(),
                    completed_at_utc=completed_at.isoformat(),
                    duration_ms=round((time.perf_counter() - started) * 1000, 3),
                    error=_safe_error(exc),
                    detail=dict(detail or {}),
                )
            )
            raise
        completed_at = _utc_now()
        self.store.record(
            ProviderCallRecord(
                call_id=f"call-{uuid4().hex}",
                run_id=self.run_id,
                provider=provider,
                operation=operation,
                resource=resource,
                status=ProviderState.LIVE_OK,
                units=float(units),
                started_at_utc=started_at.isoformat(),
                completed_at_utc=completed_at.isoformat(),
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
                error=None,
                detail=dict(detail or {}),
            )
        )
        return result

    def record_state(
        self,
        *,
        provider: str,
        operation: str,
        resource: str,
        status: ProviderState,
        units: float = 0.0,
        duration_ms: float = 0.0,
        detail: Mapping[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        now = _utc_now().isoformat()
        self.store.record(
            ProviderCallRecord(
                call_id=f"call-{uuid4().hex}",
                run_id=self.run_id,
                provider=provider,
                operation=operation,
                resource=resource,
                status=status,
                units=float(units),
                started_at_utc=now,
                completed_at_utc=now,
                duration_ms=float(duration_ms),
                error=error,
                detail=dict(detail or {}),
            )
        )


__all__ = [
    "ProviderCallRecord",
    "ProviderCallTracker",
    "ProviderState",
    "ProviderUsageStore",
    "classify_provider_exception",
]
