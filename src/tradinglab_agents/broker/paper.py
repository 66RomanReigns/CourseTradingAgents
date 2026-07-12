from __future__ import annotations

from datetime import datetime

from tradinglab_agents.models import Fill, Portfolio


class PaperBroker:
    def __init__(self, commission_bps: float = 3.0, slippage_bps: float = 5.0):
        self.commission_bps = commission_bps
        self.slippage_bps = slippage_bps

    def rebalance(
        self,
        portfolio: Portfolio,
        symbol: str,
        target_weight: float,
        execution_price: float,
        timestamp: datetime,
    ) -> Fill | None:
        prices = {symbol: execution_price}
        equity = portfolio.equity(prices)
        target_value = equity * target_weight
        current_qty = portfolio.positions.get(symbol, 0)
        desired_qty = int(target_value // execution_price)
        delta = desired_qty - current_qty
        if delta == 0:
            return None
        slipped_price = execution_price * (1 + self.slippage_bps / 10_000 * (1 if delta > 0 else -1))
        notional = abs(delta) * slipped_price
        fee = notional * self.commission_bps / 10_000
        cash_change = -(delta * slipped_price) - fee
        if portfolio.cash + cash_change < -1e-9:
            affordable = int(portfolio.cash // (slipped_price * (1 + self.commission_bps / 10_000)))
            delta = max(0, affordable)
            if delta == 0:
                return None
            notional = delta * slipped_price
            fee = notional * self.commission_bps / 10_000
            cash_change = -notional - fee
        portfolio.cash += cash_change
        portfolio.positions[symbol] = current_qty + delta
        return Fill(symbol=symbol, quantity=delta, price=slipped_price, fee=fee, timestamp=timestamp)
