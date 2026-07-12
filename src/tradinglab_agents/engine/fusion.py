from __future__ import annotations

from tradinglab_agents.models import Action, AgentOpinion, TradeIntent


class DecisionFusion:
    def fuse(self, symbol: str, primary: AgentOpinion, critic: AgentOpinion) -> TradeIntent:
        action = critic.action
        confidence = min(primary.confidence, critic.confidence)
        if action == Action.BUY:
            target = min(0.35, 0.10 + 0.30 * confidence)
        elif action == Action.SELL:
            target = 0.0
        else:
            target = -1.0  # sentinel: preserve current allocation
        return TradeIntent(
            symbol=symbol,
            action=action,
            confidence=confidence,
            target_weight=target,
            rationale=f"{primary.rationale}; critic={critic.rationale}",
            evidence_ids=primary.evidence_ids,
        )
