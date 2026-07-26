from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Iterable, Mapping

from tradinglab_agents.data.provider_registry import DataKind, ProviderCapability


class ConflictSeverity(StrEnum):
    NOT_CHECKED = "NOT_CHECKED"
    CONSISTENT = "CONSISTENT"
    MINOR = "MINOR"
    MAJOR = "MAJOR"
    INSUFFICIENT = "INSUFFICIENT"


class QualityGateStatus(StrEnum):
    NORMAL = "NORMAL"
    DEGRADED = "DEGRADED"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class ProviderCandidate:
    provider: str
    data_kind: DataKind
    resource: str
    records: tuple[dict[str, Any], ...]
    observed_at: datetime
    latest_data_at: datetime
    schema_valid: bool = True
    point_in_time_valid: bool = True
    completeness: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.provider.strip():
            raise ValueError("candidate provider cannot be empty")
        if not self.resource.strip():
            raise ValueError("candidate resource cannot be empty")
        if not 0.0 <= self.completeness <= 1.0:
            raise ValueError("candidate completeness must be in [0, 1]")
        if self.observed_at.tzinfo is None or self.latest_data_at.tzinfo is None:
            raise ValueError("candidate timestamps must be timezone-aware")

    @property
    def payload_sha256(self) -> str:
        payload = json.dumps(
            list(self.records),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def as_dict(self, *, include_records: bool = False) -> dict[str, Any]:
        payload = {
            "provider": self.provider,
            "data_kind": self.data_kind.value,
            "resource": self.resource,
            "record_count": len(self.records),
            "observed_at": self.observed_at.isoformat(),
            "latest_data_at": self.latest_data_at.isoformat(),
            "schema_valid": self.schema_valid,
            "point_in_time_valid": self.point_in_time_valid,
            "completeness": self.completeness,
            "payload_sha256": self.payload_sha256,
            "metadata": dict(self.metadata),
        }
        if include_records:
            payload["records"] = list(self.records)
        return payload


@dataclass(frozen=True)
class ConflictAssessment:
    severity: ConflictSeverity
    compared_records: int
    max_relative_difference: float
    mean_relative_difference: float
    agreement_score: float
    detail: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity.value,
            "compared_records": self.compared_records,
            "max_relative_difference": self.max_relative_difference,
            "mean_relative_difference": self.mean_relative_difference,
            "agreement_score": self.agreement_score,
            "detail": dict(self.detail),
        }


@dataclass(frozen=True)
class DataQualityAssessment:
    provider: str
    data_kind: DataKind
    resource: str
    score: float
    status: QualityGateStatus
    freshness_score: float
    authority_score: float
    agreement_score: float
    schema_score: float
    completeness_score: float
    point_in_time_score: float
    fallback_depth: int
    allow_position_increase: bool
    requires_human_review: bool
    reasons: tuple[str, ...]
    conflict: ConflictAssessment

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "data_kind": self.data_kind.value,
            "resource": self.resource,
            "score": self.score,
            "status": self.status.value,
            "freshness_score": self.freshness_score,
            "authority_score": self.authority_score,
            "agreement_score": self.agreement_score,
            "schema_score": self.schema_score,
            "completeness_score": self.completeness_score,
            "point_in_time_score": self.point_in_time_score,
            "fallback_depth": self.fallback_depth,
            "allow_position_increase": self.allow_position_increase,
            "requires_human_review": self.requires_human_review,
            "reasons": list(self.reasons),
            "conflict": self.conflict.as_dict(),
        }


def _normalized_key(record: Mapping[str, Any]) -> str | None:
    for key in ("timestamp", "date", "available_at", "evidence_id", "event_id", "id"):
        value = record.get(key)
        if value is not None:
            return str(value)
    return None


def _market_value(record: Mapping[str, Any]) -> float | None:
    for key in ("close", "value"):
        value = record.get(key)
        if value is not None:
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                return None
            return parsed if math.isfinite(parsed) else None
    return None


def assess_conflict(
    primary: ProviderCandidate,
    secondary: ProviderCandidate | None,
    *,
    warn_relative_difference: float,
    block_relative_difference: float,
) -> ConflictAssessment:
    if secondary is None:
        return ConflictAssessment(
            severity=ConflictSeverity.NOT_CHECKED,
            compared_records=0,
            max_relative_difference=0.0,
            mean_relative_difference=0.0,
            agreement_score=0.75,
            detail={"reason": "no secondary result"},
        )
    if primary.data_kind != secondary.data_kind or primary.resource != secondary.resource:
        raise ValueError("conflict candidates must represent the same data request")

    primary_map = {
        key: record
        for record in primary.records
        if (key := _normalized_key(record)) is not None
    }
    secondary_map = {
        key: record
        for record in secondary.records
        if (key := _normalized_key(record)) is not None
    }
    common = sorted(set(primary_map).intersection(secondary_map))
    if not common:
        exact = primary.payload_sha256 == secondary.payload_sha256
        return ConflictAssessment(
            severity=(ConflictSeverity.CONSISTENT if exact else ConflictSeverity.INSUFFICIENT),
            compared_records=0,
            max_relative_difference=0.0,
            mean_relative_difference=0.0,
            agreement_score=(1.0 if exact else 0.5),
            detail={"reason": "no comparable record keys", "payload_equal": exact},
        )

    differences: list[float] = []
    for key in common:
        left = _market_value(primary_map[key])
        right = _market_value(secondary_map[key])
        if left is None or right is None:
            continue
        denominator = max(abs(left), abs(right), 1e-12)
        differences.append(abs(left - right) / denominator)
    if not differences:
        exact_matches = sum(primary_map[key] == secondary_map[key] for key in common)
        agreement = exact_matches / len(common)
        severity = (
            ConflictSeverity.CONSISTENT
            if agreement >= 0.95
            else ConflictSeverity.MINOR
            if agreement >= 0.75
            else ConflictSeverity.MAJOR
        )
        return ConflictAssessment(
            severity=severity,
            compared_records=len(common),
            max_relative_difference=0.0,
            mean_relative_difference=0.0,
            agreement_score=agreement,
            detail={"exact_record_agreement": agreement},
        )

    maximum = max(differences)
    mean = sum(differences) / len(differences)
    if maximum >= block_relative_difference:
        severity = ConflictSeverity.MAJOR
    elif maximum >= warn_relative_difference:
        severity = ConflictSeverity.MINOR
    else:
        severity = ConflictSeverity.CONSISTENT
    agreement = max(0.0, min(1.0, 1.0 - maximum / max(block_relative_difference, 1e-9)))
    return ConflictAssessment(
        severity=severity,
        compared_records=len(differences),
        max_relative_difference=maximum,
        mean_relative_difference=mean,
        agreement_score=agreement,
        detail={
            "primary_provider": primary.provider,
            "secondary_provider": secondary.provider,
            "warn_threshold": warn_relative_difference,
            "block_threshold": block_relative_difference,
        },
    )


def assess_quality(
    candidate: ProviderCandidate,
    capability: ProviderCapability,
    conflict: ConflictAssessment,
    *,
    fallback_depth: int,
    minimum_score: float,
    block_score: float,
    now: datetime | None = None,
) -> DataQualityAssessment:
    current = now or datetime.now(timezone.utc)
    age_hours = max(0.0, (current - candidate.latest_data_at).total_seconds() / 3600.0)
    freshness_score = max(0.0, min(1.0, 1.0 - age_hours / capability.freshness_hours))
    schema_score = 1.0 if candidate.schema_valid else 0.0
    point_in_time_score = 1.0 if candidate.point_in_time_valid and capability.point_in_time else 0.0
    fallback_penalty = min(0.30, fallback_depth * 0.12)
    score = (
        0.24 * freshness_score
        + 0.20 * capability.authority_score
        + 0.20 * conflict.agreement_score
        + 0.14 * schema_score
        + 0.12 * candidate.completeness
        + 0.10 * point_in_time_score
        - fallback_penalty
    )
    score = round(max(0.0, min(1.0, score)), 6)
    reasons: list[str] = []
    if freshness_score < 0.5:
        reasons.append("stale_data")
    if fallback_depth > 0:
        reasons.append("fallback_provider")
    if conflict.severity == ConflictSeverity.MINOR:
        reasons.append("minor_cross_source_conflict")
    if conflict.severity == ConflictSeverity.MAJOR:
        reasons.append("major_cross_source_conflict")
    if not candidate.schema_valid:
        reasons.append("schema_invalid")
    if not candidate.point_in_time_valid:
        reasons.append("point_in_time_invalid")
    if candidate.completeness < 0.8:
        reasons.append("incomplete_payload")

    blocked = (
        score < block_score
        or not candidate.schema_valid
        or not candidate.point_in_time_valid
        or conflict.severity == ConflictSeverity.MAJOR
    )
    degraded = score < minimum_score or fallback_depth > 0 or bool(reasons)
    status = (
        QualityGateStatus.BLOCKED
        if blocked
        else QualityGateStatus.DEGRADED
        if degraded
        else QualityGateStatus.NORMAL
    )
    allow_increase = (
        status == QualityGateStatus.NORMAL
        and fallback_depth == 0
        and conflict.severity in {ConflictSeverity.CONSISTENT, ConflictSeverity.NOT_CHECKED}
    )
    return DataQualityAssessment(
        provider=candidate.provider,
        data_kind=candidate.data_kind,
        resource=candidate.resource,
        score=score,
        status=status,
        freshness_score=round(freshness_score, 6),
        authority_score=capability.authority_score,
        agreement_score=round(conflict.agreement_score, 6),
        schema_score=schema_score,
        completeness_score=candidate.completeness,
        point_in_time_score=point_in_time_score,
        fallback_depth=fallback_depth,
        allow_position_increase=allow_increase,
        requires_human_review=(status != QualityGateStatus.NORMAL),
        reasons=tuple(reasons),
        conflict=conflict,
    )


def aggregate_quality(assessments: Iterable[DataQualityAssessment]) -> dict[str, Any]:
    values = tuple(assessments)
    if not values:
        return {
            "status": QualityGateStatus.NORMAL.value,
            "minimum_score": 1.0,
            "mean_score": 1.0,
            "allow_position_increase": True,
            "requires_human_review": False,
            "blocked_resources": [],
            "degraded_resources": [],
            "assessments": [],
        }
    minimum = min(item.score for item in values)
    mean = sum(item.score for item in values) / len(values)
    blocked = [item.resource for item in values if item.status == QualityGateStatus.BLOCKED]
    degraded = [item.resource for item in values if item.status == QualityGateStatus.DEGRADED]
    status = (
        QualityGateStatus.BLOCKED
        if blocked
        else QualityGateStatus.DEGRADED
        if degraded
        else QualityGateStatus.NORMAL
    )
    return {
        "status": status.value,
        "minimum_score": round(minimum, 6),
        "mean_score": round(mean, 6),
        "allow_position_increase": all(item.allow_position_increase for item in values),
        "requires_human_review": any(item.requires_human_review for item in values),
        "blocked_resources": sorted(blocked),
        "degraded_resources": sorted(degraded),
        "assessments": [item.as_dict() for item in values],
    }


__all__ = [
    "ConflictAssessment",
    "ConflictSeverity",
    "DataQualityAssessment",
    "ProviderCandidate",
    "QualityGateStatus",
    "aggregate_quality",
    "assess_conflict",
    "assess_quality",
]
