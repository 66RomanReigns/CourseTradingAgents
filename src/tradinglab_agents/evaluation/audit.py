from __future__ import annotations

from datetime import datetime


REQUIRED_DECISION_FIELDS = {
    "decision_time",
    "available_at",
    "execution_time",
    "final_action",
    "confidence",
    "target_weight",
    "evidence_ids",
    "available_evidence_ids",
}


def audit_backtest(result: dict) -> dict:
    decisions = result.get("decisions", [])
    violations: list[dict] = []
    cited_count = 0
    cited_valid_count = 0

    for index, decision in enumerate(decisions):
        missing = sorted(REQUIRED_DECISION_FIELDS.difference(decision))
        if missing:
            violations.append({"index": index, "type": "missing_fields", "detail": missing})
            continue
        decision_time = datetime.fromisoformat(decision["decision_time"])
        available_at = datetime.fromisoformat(decision["available_at"])
        execution_time = datetime.fromisoformat(decision["execution_time"])
        if available_at > decision_time:
            violations.append({"index": index, "type": "future_market_data"})
        if execution_time <= decision_time:
            violations.append({"index": index, "type": "non_future_execution"})
        confidence = float(decision["confidence"])
        target_weight = float(decision["target_weight"])
        if not 0.0 <= confidence <= 1.0:
            violations.append({"index": index, "type": "invalid_confidence", "detail": confidence})
        if not 0.0 <= target_weight <= 1.0:
            violations.append({"index": index, "type": "invalid_target_weight", "detail": target_weight})
        cited = set(decision.get("evidence_ids", []))
        available = set(decision.get("available_evidence_ids", []))
        cited_count += len(cited)
        cited_valid_count += len(cited.intersection(available))
        unknown = sorted(cited.difference(available))
        if unknown:
            violations.append({"index": index, "type": "unknown_evidence", "detail": unknown})

    temporal_checks = len(decisions) * 2
    structural_checks = len(decisions) * 2
    citation_coverage = cited_valid_count / cited_count if cited_count else 1.0
    denominator = max(1, temporal_checks + structural_checks + cited_count)
    score = max(0.0, 1.0 - len(violations) / denominator)
    return {
        "decision_count": len(decisions),
        "violation_count": len(violations),
        "audit_score": score,
        "citation_coverage": citation_coverage,
        "passed": not violations,
        "violations": violations[:100],
        "violations_truncated": len(violations) > 100,
    }


def audit_experiment(result: dict) -> dict:
    variants = {}
    for variant in result.get("variants", []):
        if "decisions" not in variant:
            continue
        variants[variant["name"]] = audit_backtest(variant)
    passed = all(item["passed"] for item in variants.values())
    return {
        "passed": passed,
        "variants": variants,
        "minimum_audit_score": min((item["audit_score"] for item in variants.values()), default=1.0),
        "minimum_citation_coverage": min(
            (item["citation_coverage"] for item in variants.values()), default=1.0
        ),
    }
