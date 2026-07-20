from __future__ import annotations

from enum import Enum

from tradinglab_agents.models import Action, Portfolio, RiskDecision, TradeIntent


class RiskState(str, Enum):
    ACTIVE = "ACTIVE"
    LIQUIDATING = "LIQUIDATING"
    HALTED = "HALTED"


class RiskGovernor:
    """Deterministic portfolio guard with a sticky drawdown circuit breaker.

    Once the maximum drawdown is breached the governor transitions from ACTIVE
    to LIQUIDATING, emits a mandatory zero-weight decision until the position is
    flat, and then remains HALTED for the rest of the run. Recovery must be an
    explicit higher-level action; it is never inferred from a price rebound.
    """

    def __init__(
        self,
        max_position_weight: float = 0.30,
        max_drawdown: float = 0.15,
        min_confidence: float = 0.45,
    ):
        if not 0.0 <= max_position_weight <= 1.0:
            raise ValueError("max_position_weight must be in [0, 1]")
        if not 0.0 < max_drawdown < 1.0:
            raise ValueError("max_drawdown must be in (0, 1)")
        if not 0.0 <= min_confidence <= 1.0:
            raise ValueError("min_confidence must be in [0, 1]")
        self.max_position_weight = max_position_weight
        self.max_drawdown = max_drawdown
        self.min_confidence = min_confidence
        self.state = RiskState.ACTIVE

    def _decision(
        self,
        approved: bool,
        target_weight: float,
        reason: str,
        *,
        force_execution: bool = False,
    ) -> RiskDecision:
        return RiskDecision(
            approved=approved,
            target_weight=target_weight,
            reason=reason,
            force_execution=force_execution,
            state=self.state.value,
        )

    def _circuit_breaker_decision(
        self,
        current_weight: float,
        drawdown: float,
    ) -> RiskDecision:
        if current_weight > 0.0:
            self.state = RiskState.LIQUIDATING
            return self._decision(
                False,
                0.0,
                f"drawdown circuit breaker: {drawdown:.2%}; forced liquidation",
                force_execution=True,
            )
        self.state = RiskState.HALTED
        return self._decision(
            False,
            0.0,
            f"drawdown circuit breaker: {drawdown:.2%}; trading halted",
        )

    def _hold_or_trim(self, current_weight: float, prefix: str = "") -> RiskDecision:
        if current_weight > self.max_position_weight:
            return self._decision(
                True,
                self.max_position_weight,
                (
                    f"forced cap trim: {current_weight:.2%} -> "
                    f"{self.max_position_weight:.2%}; {prefix}"
                ).rstrip("; "),
            )
        return self._decision(True, current_weight, prefix or "hold current weight")

    def review(
        self,
        intent: TradeIntent,
        portfolio: Portfolio,
        prices: dict[str, float],
    ) -> RiskDecision:
        equity = portfolio.equity(prices)
        portfolio.peak_equity = max(portfolio.peak_equity, equity)
        drawdown = (
            0.0
            if portfolio.peak_equity <= 0
            else 1.0 - equity / portfolio.peak_equity
        )
        current_weight = portfolio.weight(intent.symbol, prices)

        if self.state in {RiskState.LIQUIDATING, RiskState.HALTED}:
            return self._circuit_breaker_decision(current_weight, drawdown)
        if drawdown >= self.max_drawdown:
            return self._circuit_breaker_decision(current_weight, drawdown)
        if intent.action == Action.HOLD:
            return self._hold_or_trim(current_weight)
        if intent.confidence < self.min_confidence:
            return self._hold_or_trim(
                current_weight,
                "confidence below risk threshold",
            )

        target = max(0.0, min(intent.target_weight, self.max_position_weight))
        cap_note = "capped" if target != intent.target_weight else "within cap"
        return self._decision(
            True,
            target,
            f"approved: {cap_note}; drawdown={drawdown:.2%}",
        )
