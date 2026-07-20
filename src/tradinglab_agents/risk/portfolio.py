from __future__ import annotations

from collections.abc import Mapping

from tradinglab_agents.models import (
    MarketSnapshot,
    Portfolio,
    PortfolioRiskDecision,
)
from tradinglab_agents.risk.governor import RiskState


class PortfolioRiskGovernor:
    """Portfolio-level long-only allocation constraints and circuit breaker."""

    def __init__(
        self,
        *,
        max_position_weight: float = 0.20,
        max_gross_exposure: float = 0.90,
        max_positions: int = 5,
        max_drawdown: float = 0.15,
        min_confidence: float = 0.45,
    ) -> None:
        if not 0.0 < max_position_weight <= 1.0:
            raise ValueError("max_position_weight must be in (0, 1]")
        if not 0.0 < max_gross_exposure <= 1.0:
            raise ValueError("max_gross_exposure must be in (0, 1]")
        if max_position_weight > max_gross_exposure:
            raise ValueError("max_position_weight cannot exceed max_gross_exposure")
        if max_positions < 1:
            raise ValueError("max_positions must be positive")
        if not 0.0 < max_drawdown < 1.0:
            raise ValueError("max_drawdown must be in (0, 1)")
        if not 0.0 <= min_confidence <= 1.0:
            raise ValueError("min_confidence must be in [0, 1]")
        self.max_position_weight = max_position_weight
        self.max_gross_exposure = max_gross_exposure
        self.max_positions = max_positions
        self.max_drawdown = max_drawdown
        self.min_confidence = min_confidence
        self.state = RiskState.ACTIVE

    def export_state(self) -> dict[str, str]:
        return {"state": self.state.value}

    def restore_state(self, state: str | Mapping[str, str] | None) -> None:
        if state is None:
            return
        raw = state.get("state", RiskState.ACTIVE.value) if isinstance(state, Mapping) else state
        self.state = RiskState(str(raw))

    def _decision(
        self,
        *,
        approved: bool,
        target_weights: Mapping[str, float],
        reason: str,
        force_execution: bool = False,
    ) -> PortfolioRiskDecision:
        return PortfolioRiskDecision(
            approved=approved,
            target_weights={
                symbol.upper(): float(weight)
                for symbol, weight in sorted(target_weights.items())
            },
            reason=reason,
            force_execution=force_execution,
            state=self.state.value,
        )

    def _liquidation_decision(
        self,
        portfolio: Portfolio,
        universe: set[str],
        drawdown: float,
    ) -> PortfolioRiskDecision:
        targets = {symbol: 0.0 for symbol in sorted(universe.union(portfolio.held_symbols))}
        if portfolio.held_symbols:
            self.state = RiskState.LIQUIDATING
            return self._decision(
                approved=False,
                target_weights=targets,
                reason=(
                    f"portfolio drawdown circuit breaker: {drawdown:.2%}; "
                    "forced full liquidation"
                ),
                force_execution=True,
            )
        self.state = RiskState.HALTED
        return self._decision(
            approved=False,
            target_weights=targets,
            reason=(
                f"portfolio drawdown circuit breaker: {drawdown:.2%}; trading halted"
            ),
        )

    def constrain_targets(
        self,
        target_weights: Mapping[str, float],
        current_weights: Mapping[str, float],
        confidences: Mapping[str, float] | None = None,
    ) -> tuple[dict[str, float], list[str]]:
        normalized_targets = {
            symbol.upper(): float(value) for symbol, value in target_weights.items()
        }
        normalized_current = {
            symbol.upper(): float(value) for symbol, value in current_weights.items()
        }
        confidences = {
            symbol.upper(): float(value)
            for symbol, value in (confidences or {}).items()
        }
        symbols = set(normalized_targets).union(normalized_current)
        constrained: dict[str, float] = {}
        notes: list[str] = []

        for symbol in sorted(symbols):
            raw = float(
                normalized_targets.get(symbol, normalized_current.get(symbol, 0.0))
            )
            if raw < 0.0:
                notes.append(f"{symbol} negative target rejected to zero")
                raw = 0.0
            if raw > self.max_position_weight:
                notes.append(
                    f"{symbol} capped {raw:.2%}->{self.max_position_weight:.2%}"
                )
                raw = self.max_position_weight
            current = max(0.0, float(normalized_current.get(symbol, 0.0)))
            confidence = confidences.get(symbol, 1.0)
            if raw > current and confidence < self.min_confidence:
                notes.append(
                    f"{symbol} increase blocked by confidence {confidence:.2f}"
                )
                raw = current
            constrained[symbol] = raw

        positive = sorted(
            (
                (symbol, weight)
                for symbol, weight in constrained.items()
                if weight > 1e-12
            ),
            key=lambda item: (-item[1], item[0]),
        )
        if len(positive) > self.max_positions:
            keep = {symbol for symbol, _ in positive[: self.max_positions]}
            removed = sorted(symbol for symbol, _ in positive if symbol not in keep)
            notes.append("max_positions removed " + ",".join(removed))
            for symbol in removed:
                constrained[symbol] = 0.0

        gross = sum(constrained.values())
        if gross > self.max_gross_exposure + 1e-12:
            scale = self.max_gross_exposure / gross
            for symbol in constrained:
                constrained[symbol] *= scale
            notes.append(
                f"gross exposure scaled {gross:.2%}->{self.max_gross_exposure:.2%}"
            )
        return constrained, notes

    def review(
        self,
        target_weights: Mapping[str, float],
        portfolio: Portfolio,
        snapshot: MarketSnapshot,
        *,
        confidences: Mapping[str, float] | None = None,
    ) -> PortfolioRiskDecision:
        universe = set(symbol.upper() for symbol in target_weights)
        snapshot.require(universe.union(portfolio.held_symbols))
        equity = portfolio.equity(snapshot)
        portfolio.peak_equity = max(portfolio.peak_equity, equity)
        drawdown = (
            0.0
            if portfolio.peak_equity <= 0
            else 1.0 - equity / portfolio.peak_equity
        )

        if self.state in {RiskState.LIQUIDATING, RiskState.HALTED}:
            return self._liquidation_decision(portfolio, universe, drawdown)
        if drawdown >= self.max_drawdown:
            return self._liquidation_decision(portfolio, universe, drawdown)

        current = portfolio.weights(snapshot, universe.union(portfolio.held_symbols))
        constrained, notes = self.constrain_targets(
            target_weights,
            current,
            confidences,
        )
        gross = sum(constrained.values())
        reason = (
            f"portfolio targets approved; gross={gross:.2%}; "
            f"drawdown={drawdown:.2%}"
        )
        if notes:
            reason += "; " + "; ".join(notes)
        return self._decision(
            approved=True,
            target_weights=constrained,
            reason=reason,
        )
