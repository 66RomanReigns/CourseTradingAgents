from __future__ import annotations

from tradinglab_agents.models import Action, Portfolio, RiskDecision, TradeIntent


class RiskGovernor:
    def __init__(
        self,
        max_position_weight: float = 0.30,
        max_drawdown: float = 0.15,
        min_confidence: float = 0.45,
    ):
        if not 0.0 <= max_position_weight <= 1.0:
            raise ValueError("max_position_weight must be in [0, 1]")
        self.max_position_weight = max_position_weight
        self.max_drawdown = max_drawdown
        self.min_confidence = min_confidence

    def _hold_or_trim(self, current_weight: float, prefix: str = "") -> RiskDecision:
        if current_weight > self.max_position_weight:
            return RiskDecision(
                True,
                self.max_position_weight,
                f"forced cap trim: {current_weight:.2%} -> {self.max_position_weight:.2%}; {prefix}".rstrip("; "),
            )
        return RiskDecision(True, current_weight, prefix or "hold current weight")

    def review(self, intent: TradeIntent, portfolio: Portfolio, prices: dict[str, float]) -> RiskDecision:
        equity = portfolio.equity(prices)
        portfolio.peak_equity = max(portfolio.peak_equity, equity)
        drawdown = 0.0 if portfolio.peak_equity <= 0 else 1.0 - equity / portfolio.peak_equity
        current_weight = portfolio.weight(intent.symbol, prices)
        if drawdown >= self.max_drawdown:
            return RiskDecision(False, 0.0, f"drawdown circuit breaker: {drawdown:.2%}")
        if intent.action == Action.HOLD:
            return self._hold_or_trim(current_weight)
        if intent.confidence < self.min_confidence:
            return self._hold_or_trim(current_weight, "confidence below risk threshold")
        target = max(0.0, min(intent.target_weight, self.max_position_weight))
        cap_note = "capped" if target != intent.target_weight else "within cap"
        return RiskDecision(True, target, f"approved: {cap_note}; drawdown={drawdown:.2%}")
