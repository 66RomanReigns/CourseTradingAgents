from __future__ import annotations

from tradinglab_agents.models import Action, AgentOpinion, EvidencePack


class CriticAgent:
    """Single-pass counterargument and evidence validation agent."""

    def review(self, opinion: AgentOpinion, pack: EvidencePack) -> AgentOpinion:
        unknown = set(opinion.evidence_ids).difference(pack.ids)
        if unknown:
            return AgentOpinion(
                agent="critic_agent",
                action=Action.HOLD,
                confidence=0.0,
                score=0.0,
                rationale=f"VETO: unknown evidence ids {sorted(unknown)}",
                evidence_ids=(),
            )

        vol = pack.get_float("volatility.20d_annualized")
        m5 = pack.get_float("momentum.5d")
        m20 = pack.get_float("momentum.20d")
        conflicting = m5 * m20 < 0
        penalty = 0.0
        reasons: list[str] = []
        if vol > 0.60:
            penalty += 0.25
            reasons.append("extreme volatility")
        elif vol > 0.40:
            penalty += 0.10
            reasons.append("elevated volatility")
        if conflicting:
            penalty += 0.15
            reasons.append("short/medium momentum conflict")
        if opinion.action != Action.HOLD and not opinion.evidence_ids:
            penalty += 0.35
            reasons.append("directional decision has no evidence")

        confidence = max(0.0, opinion.confidence - penalty)
        action = opinion.action
        if confidence < 0.45:
            action = Action.HOLD
            reasons.append("confidence below execution threshold")
        return AgentOpinion(
            agent="critic_agent",
            action=action,
            confidence=confidence,
            score=opinion.score,
            rationale="PASS" if not reasons else "DOWNGRADE: " + "; ".join(reasons),
            evidence_ids=opinion.evidence_ids,
        )
