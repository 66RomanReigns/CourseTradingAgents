from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from tradinglab_agents.data.provider_quality import (
    DataQualityAssessment,
    ProviderCandidate,
    assess_conflict,
    assess_quality,
)
from tradinglab_agents.data.provider_registry import (
    DataKind,
    ProviderCapability,
    ProviderRegistry,
)
from tradinglab_agents.storage.provider_health import ProviderHealthStore
from tradinglab_agents.storage.provider_usage import (
    ProviderCallTracker,
    ProviderState,
    classify_provider_exception,
)


ProviderFetcher = Callable[["ProviderRequest", ProviderCapability], ProviderCandidate]


@dataclass(frozen=True)
class ProviderRequest:
    request_id: str
    data_kind: DataKind
    resource: str
    provider_order: tuple[str, ...] = ()
    shadow_validate: bool = False
    required: bool = True
    metadata: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.request_id.strip():
            raise ValueError("provider request_id cannot be empty")
        if not self.resource.strip():
            raise ValueError("provider request resource cannot be empty")


@dataclass(frozen=True)
class ProviderAttempt:
    provider: str
    status: str
    duration_ms: float
    error_type: str | None
    error: str | None
    skipped_reason: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "error_type": self.error_type,
            "error": self.error,
            "skipped_reason": self.skipped_reason,
        }


@dataclass(frozen=True)
class ProviderRouteResult:
    request: ProviderRequest
    selected: ProviderCandidate
    secondary: ProviderCandidate | None
    quality: DataQualityAssessment
    attempts: tuple[ProviderAttempt, ...]
    fallback_depth: int

    def as_dict(self, *, include_records: bool = False) -> dict[str, Any]:
        return {
            "request": {
                "request_id": self.request.request_id,
                "data_kind": self.request.data_kind.value,
                "resource": self.request.resource,
                "provider_order": list(self.request.provider_order),
                "shadow_validate": self.request.shadow_validate,
                "required": self.request.required,
                "metadata": dict(self.request.metadata or {}),
            },
            "selected": self.selected.as_dict(include_records=include_records),
            "secondary": (
                self.secondary.as_dict(include_records=include_records)
                if self.secondary is not None
                else None
            ),
            "quality": self.quality.as_dict(),
            "attempts": [item.as_dict() for item in self.attempts],
            "fallback_depth": self.fallback_depth,
        }


class ProviderFallbackRouter:
    def __init__(
        self,
        *,
        registry: ProviderRegistry,
        health_store: ProviderHealthStore,
        run_id: str,
        fetchers: Mapping[str, ProviderFetcher],
        tracker: ProviderCallTracker | None = None,
        minimum_quality_score: float = 0.75,
        block_quality_score: float = 0.45,
        conflict_warn_relative_difference: float = 0.005,
        conflict_block_relative_difference: float = 0.02,
    ) -> None:
        if not 0.0 <= block_quality_score <= minimum_quality_score <= 1.0:
            raise ValueError("quality thresholds must satisfy 0 <= block <= minimum <= 1")
        if not 0.0 <= conflict_warn_relative_difference <= conflict_block_relative_difference:
            raise ValueError("conflict thresholds must satisfy 0 <= warn <= block")
        self.registry = registry
        self.health_store = health_store
        self.run_id = run_id
        self.fetchers = {key.strip().lower(): value for key, value in fetchers.items()}
        self.tracker = tracker
        self.minimum_quality_score = minimum_quality_score
        self.block_quality_score = block_quality_score
        self.conflict_warn_relative_difference = conflict_warn_relative_difference
        self.conflict_block_relative_difference = conflict_block_relative_difference
        self._pacing_lock = threading.Lock()
        self._pacing_semaphores: dict[str, threading.BoundedSemaphore] = {}
        self._last_provider_start: dict[str, float] = {}
        self._quota_lock = threading.Lock()
        self._quota_reserved_units: dict[str, float] = {}

    @contextmanager
    def _provider_slot(self, capability: ProviderCapability):
        group = str(capability.rate_limit_group or capability.provider)
        with self._pacing_lock:
            semaphore = self._pacing_semaphores.get(group)
            if semaphore is None:
                semaphore = threading.BoundedSemaphore(capability.max_concurrency)
                self._pacing_semaphores[group] = semaphore
        semaphore.acquire()
        try:
            while True:
                with self._pacing_lock:
                    now = time.monotonic()
                    earliest = self._last_provider_start.get(group, 0.0) + (
                        capability.min_interval_seconds
                    )
                    if now >= earliest:
                        self._last_provider_start[group] = now
                        break
                    delay = earliest - now
                time.sleep(delay)
            yield
        finally:
            semaphore.release()

    @staticmethod
    def _quota_window() -> tuple[datetime, float]:
        timezone_name = ZoneInfo("America/New_York")
        now_local = datetime.now(timezone_name)
        start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        next_local = start_local + timedelta(days=1)
        return (
            start_local.astimezone(timezone.utc),
            max(0.0, (next_local - now_local).total_seconds()),
        )

    def _reserve_quota(
        self,
        capability: ProviderCapability,
    ) -> tuple[bool, dict[str, Any]]:
        limit = capability.application_daily_quota
        if (
            capability.is_cache
            or capability.quota_units <= 0
            or limit is None
            or self.tracker is None
        ):
            return True, {"quota_enforced": False}
        group = str(capability.rate_limit_group or capability.provider)
        since_utc, cooldown_seconds = self._quota_window()
        providers = self.registry.providers_in_rate_group(group)
        with self._quota_lock:
            used = self.tracker.store.units_since(providers, since_utc)
            reserved = self._quota_reserved_units.get(group, 0.0)
            requested = float(capability.quota_units)
            allowed = used + reserved + requested <= float(limit) + 1e-12
            detail = {
                "quota_enforced": True,
                "quota_group": group,
                "providers": list(providers),
                "daily_limit": float(limit),
                "used_units": round(used, 6),
                "reserved_units": round(reserved, 6),
                "requested_units": requested,
                "quota_day_start_utc": since_utc.isoformat(),
                "cooldown_seconds": round(cooldown_seconds, 3),
                "credentials_sent": False,
            }
            if allowed:
                self._quota_reserved_units[group] = reserved + requested
            return allowed, detail

    def _release_quota(self, capability: ProviderCapability) -> None:
        if (
            capability.is_cache
            or capability.quota_units <= 0
            or capability.application_daily_quota is None
            or self.tracker is None
        ):
            return
        group = str(capability.rate_limit_group or capability.provider)
        with self._quota_lock:
            remaining = max(
                0.0,
                self._quota_reserved_units.get(group, 0.0)
                - float(capability.quota_units),
            )
            if remaining <= 1e-12:
                self._quota_reserved_units.pop(group, None)
            else:
                self._quota_reserved_units[group] = remaining

    def fingerprint_context(self) -> dict[str, Any]:
        return {
            "registry": self.registry.as_dict(),
            "minimum_quality_score": self.minimum_quality_score,
            "block_quality_score": self.block_quality_score,
            "conflict_warn_relative_difference": (
                self.conflict_warn_relative_difference
            ),
            "conflict_block_relative_difference": (
                self.conflict_block_relative_difference
            ),
        }

    def _capabilities(self, request: ProviderRequest) -> tuple[ProviderCapability, ...]:
        if request.provider_order:
            return tuple(
                self.registry.capability(provider, request.data_kind)
                for provider in request.provider_order
            )
        return self.registry.providers_for(request.data_kind)

    def _record_tracker_success(
        self,
        capability: ProviderCapability,
        request: ProviderRequest,
        candidate: ProviderCandidate,
        duration_ms: float,
        quota_detail: Mapping[str, Any],
    ) -> None:
        if self.tracker is None:
            return
        self.tracker.record_state(
            provider=capability.provider,
            operation=f"provider_graph.{request.data_kind.value.lower()}",
            resource=request.resource,
            status=(ProviderState.CACHE_HIT if capability.is_cache else ProviderState.LIVE_OK),
            units=0.0 if capability.is_cache else capability.quota_units,
            duration_ms=duration_ms,
            detail={
                "request_id": request.request_id,
                "record_count": len(candidate.records),
                "payload_sha256": candidate.payload_sha256,
                "is_cache": capability.is_cache,
                "quota": dict(quota_detail),
            },
        )

    def _attempt(
        self,
        request: ProviderRequest,
        capability: ProviderCapability,
    ) -> tuple[ProviderCandidate | None, ProviderAttempt]:
        health = self.health_store.get(capability.provider, request.data_kind)
        if not health.is_available:
            return None, ProviderAttempt(
                provider=capability.provider,
                status="SKIPPED",
                duration_ms=0.0,
                error_type=None,
                error=None,
                skipped_reason=f"health={health.status.value}, cooldown={health.cooldown_until_utc}",
            )
        fetcher = self.fetchers.get(capability.provider)
        if fetcher is None:
            return None, ProviderAttempt(
                provider=capability.provider,
                status="SKIPPED",
                duration_ms=0.0,
                error_type=None,
                error=None,
                skipped_reason="fetcher_not_registered",
            )
        quota_allowed, quota_detail = self._reserve_quota(capability)
        if not quota_allowed:
            reason = (
                "application daily quota exhausted before credentialed request: "
                f"group={quota_detail['quota_group']}, "
                f"used={quota_detail['used_units']}, "
                f"reserved={quota_detail['reserved_units']}, "
                f"requested={quota_detail['requested_units']}, "
                f"limit={quota_detail['daily_limit']}"
            )
            self.health_store.record_failure(
                run_id=self.run_id,
                provider=capability.provider,
                data_kind=request.data_kind,
                provider_state=ProviderState.RATE_LIMITED,
                reason=reason,
                detail={
                    "request_id": request.request_id,
                    "resource": request.resource,
                    **quota_detail,
                },
                cooldown_seconds_override=float(
                    quota_detail["cooldown_seconds"]
                ),
            )
            if self.tracker is not None:
                self.tracker.record_state(
                    provider=capability.provider,
                    operation=f"provider_graph.{request.data_kind.value.lower()}",
                    resource=request.resource,
                    status=ProviderState.RATE_LIMITED,
                    units=0.0,
                    detail={
                        "request_id": request.request_id,
                        "quota_preflight_blocked": True,
                        **quota_detail,
                    },
                    error=reason,
                )
            return None, ProviderAttempt(
                provider=capability.provider,
                status="SKIPPED",
                duration_ms=0.0,
                error_type="ApplicationQuotaExceeded",
                error=reason,
                skipped_reason="application_daily_quota_exhausted",
            )
        started = time.perf_counter()
        try:
            with self._provider_slot(capability):
                candidate = fetcher(request, capability)
            if candidate.provider.strip().lower() != capability.provider:
                raise ValueError(
                    f"fetcher provider mismatch: {candidate.provider} != {capability.provider}"
                )
            if candidate.data_kind != request.data_kind:
                raise ValueError("fetcher returned wrong data kind")
            if candidate.resource != request.resource:
                raise ValueError("fetcher returned wrong resource")
            if not candidate.records and not bool(
                candidate.metadata.get("empty_is_valid", False)
            ):
                raise ValueError("provider returned no canonical records")
            duration_ms = round((time.perf_counter() - started) * 1000.0, 3)
            provisional_quality = max(
                0.0,
                min(
                    1.0,
                    0.4 * capability.authority_score
                    + 0.2 * candidate.completeness
                    + 0.2 * float(candidate.schema_valid)
                    + 0.2 * float(candidate.point_in_time_valid),
                ),
            )
            self.health_store.record_success(
                run_id=self.run_id,
                provider=capability.provider,
                data_kind=request.data_kind,
                latency_ms=duration_ms,
                quality_score=provisional_quality,
                latency_threshold_ms=capability.timeout_seconds * 1000.0,
                detail={
                    "request_id": request.request_id,
                    "resource": request.resource,
                    "payload_sha256": candidate.payload_sha256,
                },
            )
            self._record_tracker_success(
                capability,
                request,
                candidate,
                duration_ms,
                quota_detail,
            )
            return candidate, ProviderAttempt(
                provider=capability.provider,
                status="SUCCESS",
                duration_ms=duration_ms,
                error_type=None,
                error=None,
                skipped_reason=None,
            )
        except Exception as exc:
            duration_ms = round((time.perf_counter() - started) * 1000.0, 3)
            state = classify_provider_exception(exc)
            safe_error = f"{type(exc).__name__}: {exc}"[:1000]
            self.health_store.record_failure(
                run_id=self.run_id,
                provider=capability.provider,
                data_kind=request.data_kind,
                provider_state=state,
                reason=safe_error,
                detail={
                    "request_id": request.request_id,
                    "resource": request.resource,
                },
            )
            if self.tracker is not None:
                self.tracker.record_state(
                    provider=capability.provider,
                    operation=f"provider_graph.{request.data_kind.value.lower()}",
                    resource=request.resource,
                    status=state,
                    units=capability.quota_units,
                    duration_ms=duration_ms,
                    error=safe_error,
                    detail={
                        "request_id": request.request_id,
                        "quota": dict(quota_detail),
                    },
                )
            return None, ProviderAttempt(
                provider=capability.provider,
                status="FAILED",
                duration_ms=duration_ms,
                error_type=type(exc).__name__,
                error=safe_error,
                skipped_reason=None,
            )
        finally:
            self._release_quota(capability)

    def route(self, request: ProviderRequest) -> ProviderRouteResult:
        capabilities = self._capabilities(request)
        if not capabilities:
            raise ValueError(f"no providers registered for {request.data_kind.value}")
        attempts: list[ProviderAttempt] = []
        successful: list[tuple[ProviderCandidate, ProviderCapability, int]] = []
        for depth, capability in enumerate(capabilities):
            candidate, attempt = self._attempt(request, capability)
            attempts.append(attempt)
            if candidate is None:
                continue
            successful.append((candidate, capability, depth))
            if not request.shadow_validate:
                break
            if len(successful) >= 2:
                break

        if not successful:
            if request.required:
                summary = "; ".join(
                    f"{item.provider}:{item.status}:{item.error or item.skipped_reason}"
                    for item in attempts
                )
                raise RuntimeError(
                    f"all providers failed for {request.request_id}: {summary}"
                )
            raise RuntimeError(
                f"optional provider request produced no result: {request.request_id}"
            )

        selected, capability, fallback_depth = successful[0]
        secondary = successful[1][0] if len(successful) > 1 else None
        conflict = assess_conflict(
            selected,
            secondary,
            warn_relative_difference=self.conflict_warn_relative_difference,
            block_relative_difference=self.conflict_block_relative_difference,
        )
        quality = assess_quality(
            selected,
            capability,
            conflict,
            fallback_depth=fallback_depth,
            minimum_score=self.minimum_quality_score,
            block_score=self.block_quality_score,
            now=datetime.now(timezone.utc),
        )
        if conflict.severity.value == "MAJOR":
            for candidate, _, _ in successful:
                self.health_store.record_conflict(
                    run_id=self.run_id,
                    provider=candidate.provider,
                    data_kind=request.data_kind,
                    quality_score=quality.score,
                    detail={
                        "request_id": request.request_id,
                        "resource": request.resource,
                        **conflict.as_dict(),
                    },
                )
        else:
            self.health_store.record_success(
                run_id=self.run_id,
                provider=capability.provider,
                data_kind=request.data_kind,
                latency_ms=next(
                    item.duration_ms
                    for item in attempts
                    if item.provider == capability.provider
                ),
                quality_score=quality.score,
                latency_threshold_ms=capability.timeout_seconds * 1000.0,
                detail={
                    "request_id": request.request_id,
                    "resource": request.resource,
                    "quality_status": quality.status.value,
                    "fallback_depth": fallback_depth,
                    "conflict": conflict.as_dict(),
                },
            )
        return ProviderRouteResult(
            request=request,
            selected=selected,
            secondary=secondary,
            quality=quality,
            attempts=tuple(attempts),
            fallback_depth=fallback_depth,
        )


__all__ = [
    "ProviderAttempt",
    "ProviderFallbackRouter",
    "ProviderFetcher",
    "ProviderRequest",
    "ProviderRouteResult",
]
