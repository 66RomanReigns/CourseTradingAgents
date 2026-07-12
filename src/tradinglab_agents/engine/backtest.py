from __future__ import annotations

from dataclasses import asdict

from tradinglab_agents.agents.critic import CriticAgent
from tradinglab_agents.agents.quant import QuantSignalAgent
from tradinglab_agents.broker.paper import PaperBroker
from tradinglab_agents.data.csv_provider import LocalCsvProvider
from tradinglab_agents.engine.features import FeatureEngine
from tradinglab_agents.engine.fusion import DecisionFusion
from tradinglab_agents.models import Portfolio
from tradinglab_agents.risk.governor import RiskGovernor


class BacktestEngine:
    """Close-to-next-open event loop; prevents same-bar lookahead execution."""

    def __init__(self, initial_cash: float = 100_000.0):
        self.initial_cash = initial_cash

    def run(self, provider: LocalCsvProvider) -> dict:
        bars = list(provider.bars)
        portfolio = Portfolio(cash=self.initial_cash, peak_equity=self.initial_cash)
        feature_engine = FeatureEngine()
        quant = QuantSignalAgent()
        critic = CriticAgent()
        fusion = DecisionFusion()
        risk = RiskGovernor()
        broker = PaperBroker()
        fills = []
        decisions = []
        equity_curve = []

        for index in range(20, len(bars) - 1):
            decision_bar = bars[index]
            visible = provider.history(decision_bar.available_at, limit=index + 1)
            pack = feature_engine.build(visible, decision_bar.available_at)
            primary = quant.analyze(pack)
            reviewed = critic.review(primary, pack)
            intent = fusion.fuse(provider.symbol, primary, reviewed)
            prices = {provider.symbol: decision_bar.close}
            risk_decision = risk.review(intent, portfolio, prices)
            next_bar = bars[index + 1]
            if risk_decision.approved:
                fill = broker.rebalance(
                    portfolio,
                    provider.symbol,
                    risk_decision.target_weight,
                    next_bar.open,
                    next_bar.timestamp,
                )
                if fill:
                    fills.append(asdict(fill))
            equity = portfolio.equity({provider.symbol: next_bar.close})
            equity_curve.append({"timestamp": next_bar.timestamp.isoformat(), "equity": equity})
            decisions.append(
                {
                    "decision_time": decision_bar.timestamp.isoformat(),
                    "execution_time": next_bar.timestamp.isoformat(),
                    "action": intent.action.value,
                    "confidence": intent.confidence,
                    "target_weight": risk_decision.target_weight,
                    "risk_reason": risk_decision.reason,
                }
            )

        final_price = bars[-1].close
        final_equity = portfolio.equity({provider.symbol: final_price})
        total_return = final_equity / self.initial_cash - 1.0
        buy_hold = bars[-1].close / bars[20].close - 1.0
        return {
            "symbol": provider.symbol,
            "initial_cash": self.initial_cash,
            "final_equity": final_equity,
            "total_return": total_return,
            "buy_hold_return": buy_hold,
            "cash": portfolio.cash,
            "positions": portfolio.positions,
            "fills": fills,
            "decisions": decisions,
            "equity_curve": equity_curve,
        }
