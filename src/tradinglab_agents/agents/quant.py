from __future__ import annotations

from tradinglab_agents.models import Action, AgentOpinion, EvidencePack


class QuantSignalAgent:
    """Deterministic analyst; interpretable and suitable for ablation."""

    def analyze(self, pack: EvidencePack) -> AgentOpinion:
        m5 = pack.get_float("momentum.5d")
        m20 = pack.get_float("momentum.20d")
        trend = pack.get_float("trend.sma5_vs_sma20")
        vol = pack.get_float("volatility.20d_annualized")
        volume_ratio = pack.get_float("volume.ratio20", 1.0)

        score = 2.0 * m5 + 1.5 * m20 + 2.0 * trend
        score += 0.02 * max(-1.0, min(1.0, volume_ratio - 1.0))
        score -= max(0.0, vol - 0.45) * 0.15

        if score > 0.025:
            action = Action.BUY
        elif score < -0.025:
            action = Action.SELL
        else:
            action = Action.HOLD
        confidence = min(0.95, 0.45 + abs(score) * 4.0)
        ids = (
            "momentum.5d",
            "momentum.20d",
            "trend.sma5_vs_sma20",
            "volatility.20d_annualized",
            "volume.ratio20",
        )
        return AgentOpinion(
            agent="quant_signal_agent",
            action=action,
            confidence=confidence,
            score=score,
            rationale=f"deterministic factor score={score:.4f}, volatility={vol:.3f}",
            evidence_ids=ids,
        )
