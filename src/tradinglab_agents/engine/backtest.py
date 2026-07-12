from __future__ import annotations

from dataclasses import asdict

from tradinglab_agents.agents.context import ContextAnalystAgent
from tradinglab_agents.agents.critic import CriticAgent
from tradinglab_agents.agents.quant import QuantSignalAgent
from tradinglab_agents.broker.paper import PaperBroker
from tradinglab_agents.config import BacktestSettings
from tradinglab_agents.data.csv_provider import LocalCsvProvider
from tradinglab_agents.data.news_provider import LocalNewsProvider
from tradinglab_agents.engine.features import FeatureEngine
from tradinglab_agents.engine.fusion import DecisionFusion
from tradinglab_agents.evaluation.metrics import compute_metrics
from tradinglab_agents.models import Action, Portfolio, RiskDecision
from tradinglab_agents.risk.governor import RiskGovernor


class BacktestEngine:
    """Point-in-time event loop with close decision and next-open execution."""

    def __init__(self, settings: BacktestSettings | None = None):
        self.settings = settings or BacktestSettings()

    def run(
        self,
        provider: LocalCsvProvider,
        news_provider: LocalNewsProvider | None = None,
        variant_name: str = "full_agent",
    ) -> dict:
        bars = list(provider.bars)
        warmup = max(20, self.settings.warmup_bars)
        if len(bars) <= warmup + 1:
            raise ValueError(f"at least {warmup + 2} bars are required")

        portfolio = Portfolio(
            cash=self.settings.initial_cash,
            peak_equity=self.settings.initial_cash,
        )
        feature_engine = FeatureEngine()
        quant = QuantSignalAgent()
        context = ContextAnalystAgent()
        critic = CriticAgent()
        fusion = DecisionFusion(
            quant_weight=self.settings.quant_weight,
            context_weight=self.settings.context_weight,
        )
        risk = RiskGovernor(
            max_position_weight=self.settings.max_position_weight,
            max_drawdown=self.settings.max_drawdown,
            min_confidence=self.settings.min_confidence,
        )
        broker = PaperBroker(
            commission_bps=self.settings.commission_bps,
            slippage_bps=self.settings.slippage_bps,
        )

        fills: list[dict] = []
        decisions: list[dict] = []
        equity_curve: list[dict] = []
        last_fill_index = -10_000

        for index in range(warmup, len(bars) - 1):
            decision_bar = bars[index]
            visible = provider.history(decision_bar.available_at)
            if len(visible) < 21:
                continue
            pack = feature_engine.build(visible, decision_bar.available_at)
            if self.settings.enable_context and news_provider is not None:
                news_provider.add_to_pack(pack)

            quant_opinion = quant.analyze(pack)
            context_opinion = (
                context.analyze(pack)
                if self.settings.enable_context and news_provider is not None
                else None
            )
            combined = fusion.combine(quant_opinion, context_opinion)
            reviewed = critic.review(combined, pack) if self.settings.enable_critic else combined
            intent = fusion.to_intent(provider.symbol, reviewed)

            close_prices = {provider.symbol: decision_bar.close}
            if self.settings.enable_risk:
                risk_decision = risk.review(intent, portfolio, close_prices)
            else:
                current = portfolio.weight(provider.symbol, close_prices)
                target = current if intent.action == Action.HOLD else max(0.0, min(1.0, intent.target_weight))
                risk_decision = RiskDecision(True, target, "risk governor disabled for ablation")

            next_bar = bars[index + 1]
            execution_prices = {provider.symbol: next_bar.open}
            current_weight = portfolio.weight(provider.symbol, execution_prices)
            weight_change = abs(risk_decision.target_weight - current_weight)
            cooldown_ok = index - last_fill_index >= self.settings.cooldown_bars
            liquidation = risk_decision.target_weight == 0.0 and current_weight > 0.0
            forced_trim = (
                self.settings.enable_risk
                and current_weight > self.settings.max_position_weight
                and risk_decision.target_weight < current_weight
            )
            should_rebalance = (
                risk_decision.approved
                and (intent.action != Action.HOLD or forced_trim)
                and weight_change >= self.settings.rebalance_threshold
                and (cooldown_ok or liquidation or forced_trim)
            )

            fill = None
            if should_rebalance:
                fill = broker.rebalance(
                    portfolio,
                    provider.symbol,
                    risk_decision.target_weight,
                    next_bar.open,
                    next_bar.open_at,
                )
                if fill:
                    fills.append(asdict(fill))
                    last_fill_index = index

            equity = portfolio.equity({provider.symbol: next_bar.close})
            equity_curve.append(
                {
                    "timestamp": next_bar.timestamp.isoformat(),
                    "equity": equity,
                }
            )
            decisions.append(
                {
                    "decision_time": decision_bar.timestamp.isoformat(),
                    "available_at": decision_bar.available_at.isoformat(),
                    "execution_time": next_bar.open_at.isoformat(),
                    "quant_action": quant_opinion.action.value,
                    "context_action": context_opinion.action.value if context_opinion else "DISABLED",
                    "fused_action": combined.action.value,
                    "final_action": intent.action.value,
                    "confidence": intent.confidence,
                    "target_weight": risk_decision.target_weight,
                    "current_weight_at_execution": current_weight,
                    "risk_reason": risk_decision.reason,
                    "critic_rationale": reviewed.rationale if self.settings.enable_critic else "DISABLED",
                    "evidence_ids": list(intent.evidence_ids),
                    "filled": fill is not None,
                }
            )

        metrics = compute_metrics(
            equity_curve,
            self.settings.initial_cash,
            fills,
        )
        return {
            "name": variant_name,
            "symbol": provider.symbol,
            "settings": asdict(self.settings),
            "initial_cash": self.settings.initial_cash,
            "final_equity": metrics["final_equity"],
            "metrics": metrics,
            "cash": portfolio.cash,
            "positions": portfolio.positions,
            "fills": fills,
            "decisions": decisions,
            "equity_curve": equity_curve,
        }
