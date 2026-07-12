from __future__ import annotations

from tradinglab_agents.agents.llm import MockLLM, StructuredReasoner
from tradinglab_agents.models import Action, AgentOpinion, EvidencePack


class ContextAnalystAgent:
    def __init__(self, reasoner: StructuredReasoner | None = None):
        self.reasoner = reasoner or MockLLM()

    def analyze(self, pack: EvidencePack) -> AgentOpinion:
        items = [
            {"evidence_id": item.evidence_id, "text": str(item.value)}
            for item in pack.by_kind("news")
        ]
        result = self.reasoner.analyze_news(items)
        score = float(result.get("score", 0.0))
        confidence = max(0.0, min(1.0, float(result.get("confidence", 0.0))))
        evidence_ids = tuple(str(value) for value in result.get("evidence_ids", []))
        unknown = set(evidence_ids).difference(pack.ids)
        if unknown:
            return AgentOpinion(
                agent="context_analyst_agent",
                action=Action.HOLD,
                confidence=0.0,
                score=0.0,
                rationale=f"invalid evidence references rejected: {sorted(unknown)}",
                evidence_ids=(),
            )
        if score > 0.15:
            action = Action.BUY
        elif score < -0.15:
            action = Action.SELL
        else:
            action = Action.HOLD
        return AgentOpinion(
            agent="context_analyst_agent",
            action=action,
            confidence=confidence,
            score=max(-1.0, min(1.0, score)),
            rationale=str(result.get("rationale", "")),
            evidence_ids=evidence_ids,
        )
