from __future__ import annotations

from tradinglab_agents.models import Action, Portfolio, RiskDecision, TradeIntent


class RiskGovernor:
    def __init__(self, max_position_weight: float = 0.30, max_drawdown: float = 0.15):
        self.max_position_weight = max_position_weight
        self.max_drawdown = max_drawdown

    def review(self, intent: TradeIntent, portfolio: Portfolio, prices: dict[str, float]) -> RiskDecision:
        equity = portfolio.equity(prices)
        portfolio.peak_equity = max(portfolio.peak_equity, equity)
        drawdown = 0.0 if portfolio.peak_equity <= 0 else 1.0 - equity / portfolio.peak_equity
        if drawdown >= self.max_drawdown:
            return RiskDecision(False, 0.0, f"drawdown circuit breaker: {drawdown:.2%}")
        if intent.action == Action.HOLD:
            current_value = portfolio.positions.get(intent.symbol, 0) * prices[intent.symbol]
            current_weight = current_value / equity if equity else 0.0
            return RiskDecision(True, current_weight, "hold current weight")
        target = max(0.0, min(intent.target_weight, self.max_position_weight))
        if intent.confidence < 0.45:
            return RiskDecision(False, 0.0, "confidence below risk threshold")
        return RiskDecision(True, target, "approved with hard position cap")
