from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from tradinglab_agents.models import Fill, MarketSnapshot, Portfolio


class PaperBroker:
    """Long-only paper broker with synchronized multi-asset valuation.

    Reductions are always executed before increases so a rebalance can reuse
    released cash. Every valuation requires prices for all existing holdings;
    missing symbols fail immediately instead of being marked to zero.
    """

    def __init__(self, commission_bps: float = 3.0, slippage_bps: float = 5.0):
        if commission_bps < 0 or slippage_bps < 0:
            raise ValueError("broker costs must be non-negative")
        self.commission_bps = commission_bps
        self.slippage_bps = slippage_bps

    def _snapshot(
        self,
        prices: MarketSnapshot | Mapping[str, float],
        timestamp: datetime,
    ) -> MarketSnapshot:
        if isinstance(prices, MarketSnapshot):
            return prices
        return MarketSnapshot(timestamp=timestamp, prices=prices, field="open")

    def rebalance(
        self,
        portfolio: Portfolio,
        symbol: str,
        target_weight: float,
        execution_price: float,
        timestamp: datetime,
        valuation_prices: MarketSnapshot | Mapping[str, float] | None = None,
    ) -> Fill | None:
        normalized = symbol.upper()
        if not 0.0 <= target_weight <= 1.0:
            raise ValueError("target_weight must be in [0, 1]")
        price_map: dict[str, float]
        if valuation_prices is None:
            price_map = {normalized: execution_price}
            snapshot = self._snapshot(price_map, timestamp)
        elif isinstance(valuation_prices, MarketSnapshot):
            price_map = dict(valuation_prices.prices)
            price_map[normalized] = execution_price
            snapshot = MarketSnapshot(
                timestamp=valuation_prices.timestamp,
                prices=price_map,
                field=valuation_prices.field,
            )
        else:
            price_map = {key.upper(): float(value) for key, value in valuation_prices.items()}
            price_map[normalized] = execution_price
            snapshot = self._snapshot(price_map, timestamp)

        snapshot.require(portfolio.held_symbols.union({normalized}))
        equity = portfolio.equity(snapshot)
        target_value = equity * target_weight
        current_quantity = portfolio.positions.get(normalized, 0)
        desired_quantity = int(target_value // execution_price)
        delta = desired_quantity - current_quantity
        if delta == 0:
            return None

        slipped_price = execution_price * (
            1 + self.slippage_bps / 10_000 * (1 if delta > 0 else -1)
        )
        notional = abs(delta) * slipped_price
        fee = notional * self.commission_bps / 10_000
        cash_change = -(delta * slipped_price) - fee

        if delta > 0 and portfolio.cash + cash_change < -1e-9:
            affordable = int(
                portfolio.cash
                // (slipped_price * (1 + self.commission_bps / 10_000))
            )
            delta = max(0, affordable)
            if delta == 0:
                return None
            notional = delta * slipped_price
            fee = notional * self.commission_bps / 10_000
            cash_change = -notional - fee

        portfolio.cash += cash_change
        portfolio.positions[normalized] = current_quantity + delta
        if portfolio.cash < -1e-7:
            raise RuntimeError("paper broker produced negative cash")
        return Fill(
            symbol=normalized,
            quantity=delta,
            price=slipped_price,
            fee=fee,
            timestamp=timestamp,
        )

    def rebalance_many(
        self,
        portfolio: Portfolio,
        target_weights: Mapping[str, float],
        execution_snapshot: MarketSnapshot,
    ) -> list[Fill]:
        normalized = {
            symbol.upper(): float(weight) for symbol, weight in target_weights.items()
        }
        if not normalized:
            raise ValueError("target_weights cannot be empty")
        missing_targets = portfolio.held_symbols.difference(normalized)
        if missing_targets:
            raise ValueError(
                "target_weights must include every existing holding: "
                + ", ".join(sorted(missing_targets))
            )
        if any(weight < 0.0 or weight > 1.0 for weight in normalized.values()):
            raise ValueError("all target weights must be in [0, 1]")
        if sum(normalized.values()) > 1.0 + 1e-9:
            raise ValueError("long-only target weights cannot exceed 100% gross exposure")
        execution_snapshot.require(set(normalized).union(portfolio.held_symbols))

        current_weights = portfolio.weights(execution_snapshot, set(normalized))
        reductions = sorted(
            symbol
            for symbol, target in normalized.items()
            if target < current_weights.get(symbol, 0.0) - 1e-12
        )
        increases = sorted(
            symbol
            for symbol, target in normalized.items()
            if target > current_weights.get(symbol, 0.0) + 1e-12
        )

        fills: list[Fill] = []
        for symbol in [*reductions, *increases]:
            fill = self.rebalance(
                portfolio=portfolio,
                symbol=symbol,
                target_weight=normalized[symbol],
                execution_price=execution_snapshot.price(symbol),
                timestamp=execution_snapshot.timestamp,
                valuation_prices=execution_snapshot,
            )
            if fill is not None:
                fills.append(fill)
        return fills
