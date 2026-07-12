from __future__ import annotations

from tradinglab_agents.models import Action, AgentOpinion, EvidencePack


class CriticAgent:
    """Rule-based counterargument pass; later replaceable with an LLM adapter."""

    def review(self, opinion: AgentOpinion, pack: EvidencePack) -> AgentOpinion:
        vol = pack.get_float("volatility.20d_annualized")
        m5 = pack.get_float("momentum.5d")
        m20 = pack.get_float("momentum.20d")
        conflicting = m5 * m20 < 0
        penalty = 0.0
        reasons: list[str] = []
        if vol > 0.6:
            penalty += 0.25
            reasons.append("extreme volatility")
        if conflicting:
            penalty += 0.15
            reasons.append("short/medium momentum conflict")
        confidence = max(0.05, opinion.confidence - penalty)
        action = opinion.action
        if confidence < 0.45:
            action = Action.HOLD
            reasons.append("confidence downgraded below execution threshold")
        return AgentOpinion(
            agent="critic_agent",
            action=action,
            confidence=confidence,
            score=opinion.score,
            rationale="PASS" if not reasons else "DOWNGRADE: " + "; ".join(reasons),
            evidence_ids=opinion.evidence_ids,
        )
