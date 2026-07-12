from __future__ import annotations

from tradinglab_agents.models import Action, AgentOpinion, TradeIntent


class DecisionFusion:
    def __init__(
        self,
        quant_weight: float = 0.75,
        context_weight: float = 0.25,
        buy_threshold: float = 0.035,
        sell_threshold: float = -0.035,
        conflict_penalty: float = 0.70,
    ):
        total = quant_weight + context_weight
        if total <= 0:
            raise ValueError("fusion weights must sum to a positive value")
        if buy_threshold <= 0 or sell_threshold >= 0:
            raise ValueError("buy threshold must be positive and sell threshold negative")
        if not 0.0 <= conflict_penalty <= 1.0:
            raise ValueError("conflict_penalty must be in [0, 1]")
        self.quant_weight = quant_weight / total
        self.context_weight = context_weight / total
        self.buy_threshold = buy_threshold
        self.sell_threshold = sell_threshold
        self.conflict_penalty = conflict_penalty

    @staticmethod
    def _signed(opinion: AgentOpinion) -> float:
        direction = 1.0 if opinion.action == Action.BUY else -1.0 if opinion.action == Action.SELL else 0.0
        bounded_score = max(-1.0, min(1.0, opinion.score))
        if direction == 0.0:
            return bounded_score * opinion.confidence
        return direction * max(abs(bounded_score), 0.25) * opinion.confidence

    def combine(
        self,
        quant: AgentOpinion,
        context: AgentOpinion | None = None,
    ) -> AgentOpinion:
        quant_signal = self._signed(quant)
        if context is None:
            combined = quant_signal
            confidence = quant.confidence
            evidence_ids = quant.evidence_ids
            rationale = f"quant={quant_signal:.4f}; context=disabled"
        else:
            context_signal = self._signed(context)
            combined = self.quant_weight * quant_signal + self.context_weight * context_signal
            disagreement = quant_signal * context_signal < 0
            confidence = self.quant_weight * quant.confidence + self.context_weight * context.confidence
            if disagreement:
                confidence *= self.conflict_penalty
            evidence_ids = tuple(dict.fromkeys((*quant.evidence_ids, *context.evidence_ids)))
            rationale = (
                f"quant={quant_signal:.4f}; context={context_signal:.4f}; "
                f"disagreement={disagreement}; conflict_penalty={self.conflict_penalty:.2f}"
            )
        if combined > self.buy_threshold:
            action = Action.BUY
        elif combined < self.sell_threshold:
            action = Action.SELL
        else:
            action = Action.HOLD
        return AgentOpinion(
            agent="decision_fusion_agent",
            action=action,
            confidence=max(0.0, min(1.0, confidence)),
            score=combined,
            rationale=rationale,
            evidence_ids=evidence_ids,
        )

    @staticmethod
    def to_intent(symbol: str, opinion: AgentOpinion) -> TradeIntent:
        if opinion.action == Action.BUY:
            target = min(0.45, 0.08 + 0.42 * opinion.confidence)
        elif opinion.action == Action.SELL:
            target = 0.0
        else:
            target = -1.0
        return TradeIntent(
            symbol=symbol,
            action=opinion.action,
            confidence=opinion.confidence,
            target_weight=target,
            rationale=opinion.rationale,
            evidence_ids=opinion.evidence_ids,
        )
