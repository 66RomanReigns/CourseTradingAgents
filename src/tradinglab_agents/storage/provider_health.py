from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

from tradinglab_agents.data.provider_registry import DataKind
from tradinglab_agents.storage.provider_usage import ProviderState


class ProviderHealthStatus(StrEnum):
    UNKNOWN = "UNKNOWN"
    HEALTHY = "HEALTHY"
    DEGRADED_LATENCY = "DEGRADED_LATENCY"
    DEGRADED_STALE = "DEGRADED_STALE"
    RATE_LIMITED = "RATE_LIMITED"
    SCHEMA_CHANGED = "SCHEMA_CHANGED"
    AUTH_FAILED = "AUTH_FAILED"
    DATA_CONFLICT = "DATA_CONFLICT"
    OFFLINE = "OFFLINE"


@dataclass(frozen=True)
class ProviderHealthSnapshot:
    provider: str
    data_kind: DataKind
    status: ProviderHealthStatus
    consecutive_successes: int
    consecutive_failures: int
    last_success_at_utc: str | None
    last_failure_at_utc: str | None
    cooldown_until_utc: str | None
    latency_ms: float | None
    quality_score: float | None
    detail: dict[str, Any]
    updated_at_utc: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "data_kind": self.data_kind.value,
            "status": self.status.value,
            "is_available": self.is_available,
            "consecutive_successes": self.consecutive_successes,
            "consecutive_failures": self.consecutive_failures,
            "last_success_at_utc": self.last_success_at_utc,
            "last_failure_at_utc": self.last_failure_at_utc,
            "cooldown_until_utc": self.cooldown_until_utc,
            "latency_ms": self.latency_ms,
            "quality_score": self.quality_score,
            "detail": dict(self.detail),
            "updated_at_utc": self.updated_at_utc,
        }

    @property
    def is_available(self) -> bool:
        if self.status == ProviderHealthStatus.SCHEMA_CHANGED:
            return False
        if self.cooldown_until_utc:
            cooldown = datetime.fromisoformat(self.cooldown_until_utc)
            if cooldown > datetime.now(timezone.utc):
                return False
        return True


class ProviderHealthStore:
    SCHEMA_VERSION = 1

    def __init__(self, path: str | Path) -> None:
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
                    f"provider health database schema {version} is newer than supported {self.SCHEMA_VERSION}"
                )
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS provider_health (
                    provider TEXT NOT NULL,
                    data_kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    consecutive_successes INTEGER NOT NULL,
                    consecutive_failures INTEGER NOT NULL,
                    last_success_at_utc TEXT,
                    last_failure_at_utc TEXT,
                    cooldown_until_utc TEXT,
                    latency_ms REAL,
                    quality_score REAL,
                    detail_json TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL,
                    PRIMARY KEY (provider, data_kind)
                );

                CREATE TABLE IF NOT EXISTS provider_health_events (
                    event_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    data_kind TEXT NOT NULL,
                    previous_status TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    detail_json TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_provider_health_events_provider_time
                    ON provider_health_events(provider, data_kind, created_at_utc DESC);
                CREATE INDEX IF NOT EXISTS idx_provider_health_events_run
                    ON provider_health_events(run_id, created_at_utc);
                """
            )
            connection.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")

    @staticmethod
    def _snapshot(row: sqlite3.Row) -> ProviderHealthSnapshot:
        return ProviderHealthSnapshot(
            provider=str(row["provider"]),
            data_kind=DataKind(str(row["data_kind"])),
            status=ProviderHealthStatus(str(row["status"])),
            consecutive_successes=int(row["consecutive_successes"]),
            consecutive_failures=int(row["consecutive_failures"]),
            last_success_at_utc=row["last_success_at_utc"],
            last_failure_at_utc=row["last_failure_at_utc"],
            cooldown_until_utc=row["cooldown_until_utc"],
            latency_ms=(float(row["latency_ms"]) if row["latency_ms"] is not None else None),
            quality_score=(
                float(row["quality_score"])
                if row["quality_score"] is not None
                else None
            ),
            detail=dict(json.loads(row["detail_json"] or "{}")),
            updated_at_utc=str(row["updated_at_utc"]),
        )

    def get(self, provider: str, data_kind: DataKind) -> ProviderHealthSnapshot:
        normalized = provider.strip().lower()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM provider_health WHERE provider = ? AND data_kind = ?",
                (normalized, data_kind.value),
            ).fetchone()
        if row is not None:
            return self._snapshot(row)
        now = datetime.now(timezone.utc).isoformat()
        return ProviderHealthSnapshot(
            provider=normalized,
            data_kind=data_kind,
            status=ProviderHealthStatus.UNKNOWN,
            consecutive_successes=0,
            consecutive_failures=0,
            last_success_at_utc=None,
            last_failure_at_utc=None,
            cooldown_until_utc=None,
            latency_ms=None,
            quality_score=None,
            detail={},
            updated_at_utc=now,
        )

    def list(self) -> list[ProviderHealthSnapshot]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM provider_health ORDER BY data_kind, provider"
            ).fetchall()
        return [self._snapshot(row) for row in rows]

    def _write(
        self,
        *,
        run_id: str,
        provider: str,
        data_kind: DataKind,
        status: ProviderHealthStatus,
        reason: str,
        success: bool,
        latency_ms: float | None = None,
        quality_score: float | None = None,
        cooldown_seconds: float | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> ProviderHealthSnapshot:
        normalized = provider.strip().lower()
        previous = self.get(normalized, data_kind)
        now = datetime.now(timezone.utc)
        cooldown_until = (
            now + timedelta(seconds=max(0.0, float(cooldown_seconds)))
            if cooldown_seconds is not None
            else None
        )
        successes = previous.consecutive_successes + 1 if success else 0
        failures = previous.consecutive_failures + 1 if not success else 0
        merged_detail = {**previous.detail, **dict(detail or {})}
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO provider_health (
                    provider, data_kind, status, consecutive_successes,
                    consecutive_failures, last_success_at_utc,
                    last_failure_at_utc, cooldown_until_utc, latency_ms,
                    quality_score, detail_json, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, data_kind) DO UPDATE SET
                    status=excluded.status,
                    consecutive_successes=excluded.consecutive_successes,
                    consecutive_failures=excluded.consecutive_failures,
                    last_success_at_utc=excluded.last_success_at_utc,
                    last_failure_at_utc=excluded.last_failure_at_utc,
                    cooldown_until_utc=excluded.cooldown_until_utc,
                    latency_ms=excluded.latency_ms,
                    quality_score=excluded.quality_score,
                    detail_json=excluded.detail_json,
                    updated_at_utc=excluded.updated_at_utc
                """,
                (
                    normalized,
                    data_kind.value,
                    status.value,
                    successes,
                    failures,
                    now.isoformat() if success else previous.last_success_at_utc,
                    previous.last_failure_at_utc if success else now.isoformat(),
                    cooldown_until.isoformat() if cooldown_until else None,
                    latency_ms,
                    quality_score,
                    json.dumps(merged_detail, ensure_ascii=False, sort_keys=True, default=str),
                    now.isoformat(),
                ),
            )
            connection.execute(
                """
                INSERT INTO provider_health_events (
                    event_id, run_id, provider, data_kind, previous_status,
                    status, reason, detail_json, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"phe-{uuid4().hex}",
                    run_id,
                    normalized,
                    data_kind.value,
                    previous.status.value,
                    status.value,
                    reason[:500],
                    json.dumps(dict(detail or {}), ensure_ascii=False, sort_keys=True, default=str),
                    now.isoformat(),
                ),
            )
        return self.get(normalized, data_kind)

    def record_success(
        self,
        *,
        run_id: str,
        provider: str,
        data_kind: DataKind,
        latency_ms: float,
        quality_score: float,
        latency_threshold_ms: float,
        detail: Mapping[str, Any] | None = None,
    ) -> ProviderHealthSnapshot:
        status = (
            ProviderHealthStatus.DEGRADED_LATENCY
            if latency_ms > latency_threshold_ms
            else ProviderHealthStatus.HEALTHY
        )
        return self._write(
            run_id=run_id,
            provider=provider,
            data_kind=data_kind,
            status=status,
            reason="successful provider result",
            success=True,
            latency_ms=latency_ms,
            quality_score=quality_score,
            detail=detail,
        )

    def record_failure(
        self,
        *,
        run_id: str,
        provider: str,
        data_kind: DataKind,
        provider_state: ProviderState,
        reason: str,
        detail: Mapping[str, Any] | None = None,
        cooldown_seconds_override: float | None = None,
    ) -> ProviderHealthSnapshot:
        mapping = {
            ProviderState.RATE_LIMITED: (
                ProviderHealthStatus.RATE_LIMITED,
                3600.0,
            ),
            ProviderState.AUTH_FAILED: (
                ProviderHealthStatus.AUTH_FAILED,
                24 * 3600.0,
            ),
            ProviderState.NETWORK_BLOCKED: (
                ProviderHealthStatus.OFFLINE,
                300.0,
            ),
            ProviderState.DEGRADED_STALE: (
                ProviderHealthStatus.DEGRADED_STALE,
                None,
            ),
        }
        status, cooldown = mapping.get(
            provider_state,
            (
                ProviderHealthStatus.SCHEMA_CHANGED
                if any(
                    token in reason.lower()
                    for token in ("schema", "missing", "expected", "invalid json")
                )
                else ProviderHealthStatus.OFFLINE,
                900.0,
            ),
        )
        if cooldown_seconds_override is not None:
            cooldown = max(0.0, float(cooldown_seconds_override))
        return self._write(
            run_id=run_id,
            provider=provider,
            data_kind=data_kind,
            status=status,
            reason=reason,
            success=False,
            cooldown_seconds=cooldown,
            detail=detail,
        )

    def record_conflict(
        self,
        *,
        run_id: str,
        provider: str,
        data_kind: DataKind,
        quality_score: float,
        detail: Mapping[str, Any],
    ) -> ProviderHealthSnapshot:
        return self._write(
            run_id=run_id,
            provider=provider,
            data_kind=data_kind,
            status=ProviderHealthStatus.DATA_CONFLICT,
            reason="cross-source data conflict",
            success=False,
            quality_score=quality_score,
            cooldown_seconds=300.0,
            detail=detail,
        )

    def events(self, *, run_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        if not 1 <= limit <= 5000:
            raise ValueError("provider health event limit must be in [1, 5000]")
        params: list[Any] = []
        where = ""
        if run_id:
            where = "WHERE run_id = ?"
            params.append(run_id)
        params.append(limit)
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM provider_health_events {where}
                ORDER BY created_at_utc DESC LIMIT ?
                """,
                params,
            ).fetchall()
        return [
            {
                "event_id": row["event_id"],
                "run_id": row["run_id"],
                "provider": row["provider"],
                "data_kind": row["data_kind"],
                "previous_status": row["previous_status"],
                "status": row["status"],
                "reason": row["reason"],
                "detail": dict(json.loads(row["detail_json"] or "{}")),
                "created_at_utc": row["created_at_utc"],
            }
            for row in rows
        ]


__all__ = [
    "ProviderHealthSnapshot",
    "ProviderHealthStatus",
    "ProviderHealthStore",
]
