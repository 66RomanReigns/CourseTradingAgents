from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict

from tradinglab_agents.agents.context import ContextAnalystAgent
from tradinglab_agents.agents.critic import CriticAgent
from tradinglab_agents.agents.quant import QuantSignalAgent
from tradinglab_agents.agents.regime import RegimeGuardAgent
from tradinglab_agents.broker.paper import PaperBroker
from tradinglab_agents.config import BacktestSettings
from tradinglab_agents.data.corporate_actions import (
    LocalCorporateActionProvider,
    apply_corporate_actions,
)
from tradinglab_agents.data.csv_provider import LocalCsvProvider
from tradinglab_agents.data.evidence_provider import LocalPointInTimeEvidenceProvider
from tradinglab_agents.data.news_provider import LocalNewsProvider
from tradinglab_agents.engine.features import FeatureEngine
from tradinglab_agents.engine.fusion import DecisionFusion
from tradinglab_agents.engine.market import AlignedMarketData
from tradinglab_agents.engine.trading_calendar import ExchangeTradingCalendar
from tradinglab_agents.evaluation.metrics import compute_metrics
from tradinglab_agents.models import (
    Action,
    Portfolio,
    PortfolioRiskDecision,
)
from tradinglab_agents.risk.portfolio import PortfolioRiskGovernor


class MultiAssetBacktestEngine:
    """Synchronized point-in-time backtest for a long-only asset universe."""

    def __init__(self, settings: BacktestSettings | None = None):
        self.settings = settings or BacktestSettings()

    def run(
        self,
        providers: Mapping[str, LocalCsvProvider],
        news_providers: Mapping[str, LocalNewsProvider] | None = None,
        *,
        variant_name: str = "multi_asset_agent",
        evidence_providers: Sequence[LocalPointInTimeEvidenceProvider] | None = None,
        corporate_action_providers: Mapping[
            str, LocalCorporateActionProvider
        ] | None = None,
    ) -> dict:
        calendar = ExchangeTradingCalendar(self.settings.market_calendar_name)
        resolved_actions = dict(corporate_action_providers or {})
        if (
            corporate_action_providers is None
            and self.settings.market_corporate_actions_enabled
        ):
            for symbol, provider in providers.items():
                path = (
                    provider.path.parent
                    / f"{symbol.upper()}{self.settings.market_corporate_action_suffix}"
                )
                if path.is_file():
                    resolved_actions[symbol.upper()] = LocalCorporateActionProvider(
                        path,
                        symbol,
                        calendar=calendar,
                    )
        market = AlignedMarketData(
            providers,
            calendar=(calendar if self.settings.market_strict_sessions else None),
            strict_session_times=self.settings.market_strict_session_times,
            require_complete_alignment=(
                self.settings.market_require_complete_alignment
            ),
            corporate_actions=resolved_actions,
            adjust_history_for_splits=(
                self.settings.market_adjust_history_for_splits
            ),
            adjust_history_for_dividends=(
                self.settings.market_adjust_history_for_dividends
            ),
        )
        news_providers = {
            symbol.upper(): provider
            for symbol, provider in (news_providers or {}).items()
        }
        unknown_news = set(news_providers).difference(market.symbols)
        if unknown_news:
            raise ValueError(f"news providers contain unknown symbols: {sorted(unknown_news)}")
        evidence_providers = tuple(evidence_providers or ())
        warmup = max(20, self.settings.warmup_bars)
        if len(market.timestamps) <= warmup + 1:
            raise ValueError(f"at least {warmup + 2} synchronized bars are required")

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
            buy_threshold=self.settings.buy_threshold,
            sell_threshold=self.settings.sell_threshold,
            conflict_penalty=self.settings.conflict_penalty,
        )
        regime_guards = {symbol: RegimeGuardAgent() for symbol in market.symbols}
        portfolio_risk = PortfolioRiskGovernor(
            max_position_weight=self.settings.max_position_weight,
            max_gross_exposure=self.settings.max_gross_exposure,
            max_positions=self.settings.max_positions,
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
        portfolio_history: list[dict] = []
        corporate_action_events: list[dict] = []
        last_fill_index = {symbol: -10_000 for symbol in market.symbols}

        for index in range(warmup, len(market.timestamps) - 1):
            timestamp = market.timestamps[index]
            next_timestamp = market.timestamps[index + 1]
            decision_time = market.decision_time(timestamp)
            close_snapshot = market.close_snapshot(timestamp)
            current_close_weights = portfolio.weights(
                close_snapshot,
                set(market.symbols),
            )

            proposed_targets: dict[str, float] = {}
            confidences: dict[str, float] = {}
            symbol_rows: dict[str, dict] = {}

            for symbol in market.symbols:
                visible = market.history(symbol, decision_time)
                if len(visible) < 21:
                    raise ValueError(
                        f"insufficient visible history for {symbol} at {decision_time.isoformat()}"
                    )
                pack = feature_engine.build(visible, decision_time)
                news_provider = news_providers.get(symbol)
                if self.settings.enable_context and news_provider is not None:
                    news_provider.add_to_pack(pack)
                if self.settings.enable_context:
                    for external_provider in evidence_providers:
                        external_provider.add_to_pack(pack)

                quant_opinion = quant.analyze(pack)
                has_context_evidence = any(
                    item.kind in {"news", "macro", "fundamental"}
                    for item in pack.evidence
                )
                context_opinion = (
                    context.analyze(pack)
                    if self.settings.enable_context and has_context_evidence
                    else None
                )
                combined = fusion.combine(quant_opinion, context_opinion)
                reviewed = (
                    critic.review(combined, pack)
                    if self.settings.enable_critic
                    else combined
                )
                intent = fusion.to_intent(symbol, reviewed)
                assessment = regime_guards[symbol].assess(pack)

                current_weight = current_close_weights[symbol]
                if intent.action == Action.HOLD or intent.confidence < self.settings.min_confidence:
                    target = current_weight
                elif intent.action == Action.SELL:
                    target = 0.0
                else:
                    target = intent.target_weight

                if self.settings.enable_regime_guard:
                    base_cap = (
                        self.settings.max_position_weight
                        if self.settings.enable_risk
                        else 0.45
                    )
                    target = min(target, base_cap * assessment.exposure_multiplier)

                proposed_targets[symbol] = max(0.0, target)
                confidences[symbol] = intent.confidence
                symbol_rows[symbol] = {
                    "symbol": symbol,
                    "decision_time": decision_time.isoformat(),
                    "market_timestamp": timestamp.isoformat(),
                    "quant_action": quant_opinion.action.value,
                    "quant_score": quant_opinion.score,
                    "quant_confidence": quant_opinion.confidence,
                    "context_action": (
                        context_opinion.action.value if context_opinion else "DISABLED"
                    ),
                    "context_score": context_opinion.score if context_opinion else None,
                    "context_confidence": (
                        context_opinion.confidence if context_opinion else None
                    ),
                    "fused_action": combined.action.value,
                    "fused_score": combined.score,
                    "final_action": intent.action.value,
                    "confidence": intent.confidence,
                    "current_weight_at_decision": current_weight,
                    "proposed_target_weight": proposed_targets[symbol],
                    "regime": assessment.label,
                    "regime_exposure_multiplier": assessment.exposure_multiplier,
                    "regime_rationale": assessment.rationale,
                    "critic_rationale": (
                        reviewed.rationale if self.settings.enable_critic else "DISABLED"
                    ),
                    "evidence_ids": sorted(intent.evidence_ids),
                    "available_evidence_ids": sorted(pack.ids),
                    "evidence_count": len(pack.evidence),
                }

            if self.settings.enable_risk:
                risk_decision = portfolio_risk.review(
                    proposed_targets,
                    portfolio,
                    close_snapshot,
                    confidences=confidences,
                )
            else:
                gross = sum(proposed_targets.values())
                if gross > 1.0:
                    scale = 1.0 / gross
                    unconstrained_targets = {
                        symbol: weight * scale
                        for symbol, weight in proposed_targets.items()
                    }
                    reason = (
                        "portfolio risk governor disabled for ablation; "
                        f"broker cash invariant scaled gross {gross:.2%}->100.00%"
                    )
                else:
                    unconstrained_targets = proposed_targets
                    reason = "portfolio risk governor disabled for ablation"
                risk_decision = PortfolioRiskDecision(
                    approved=True,
                    target_weights=unconstrained_targets,
                    reason=reason,
                )

            execution_snapshot = market.open_snapshot(next_timestamp)
            session_actions = market.actions_for_session(
                next_timestamp.date(),
                as_of=execution_snapshot.timestamp,
            )
            action_effects = apply_corporate_actions(
                portfolio,
                session_actions,
                execution_snapshot.prices,
            )
            corporate_action_events.extend(
                {
                    **effect.as_dict(),
                    "effective_at": execution_snapshot.timestamp.isoformat(),
                    "paper_only": True,
                    "external_broker": False,
                }
                for effect in action_effects
            )
            current_execution_weights = portfolio.weights(
                execution_snapshot,
                set(market.symbols),
            )
            execution_targets: dict[str, float] = {}
            should_rebalance: dict[str, bool] = {}

            for symbol in market.symbols:
                target = risk_decision.target_weights.get(symbol, 0.0)
                current = current_execution_weights[symbol]
                weight_change = abs(target - current)
                protective_reduction = target < current - 1e-12
                cooldown_ok = (
                    index - last_fill_index[symbol] >= self.settings.cooldown_bars
                )
                directional = symbol_rows[symbol]["final_action"] != Action.HOLD.value
                should_trade = (
                    risk_decision.force_execution
                    or protective_reduction
                    or (
                        directional
                        and weight_change >= self.settings.rebalance_threshold
                        and cooldown_ok
                    )
                )
                should_rebalance[symbol] = should_trade
                execution_targets[symbol] = target if should_trade else current

            day_fills = broker.rebalance_many(
                portfolio,
                execution_targets,
                execution_snapshot,
            )
            day_fills_by_symbol: dict[str, list[dict]] = {
                symbol: [] for symbol in market.symbols
            }
            for fill in day_fills:
                payload = asdict(fill)
                fills.append(payload)
                day_fills_by_symbol[fill.symbol].append(payload)
                last_fill_index[fill.symbol] = index

            execution_equity = portfolio.equity(execution_snapshot)
            execution_weights = portfolio.weights(
                execution_snapshot,
                set(market.symbols),
            )
            execution_gross_exposure = sum(
                abs(value) for value in execution_weights.values()
            )
            next_close_snapshot = market.close_snapshot(next_timestamp)
            equity = portfolio.equity(next_close_snapshot)
            close_weights = portfolio.weights(
                next_close_snapshot,
                set(market.symbols),
            )
            gross_exposure = sum(abs(value) for value in close_weights.values())
            equity_curve.append(
                {
                    "timestamp": next_timestamp.isoformat(),
                    "equity": equity,
                }
            )
            portfolio_history.append(
                {
                    "timestamp": next_timestamp.isoformat(),
                    "cash": portfolio.cash,
                    "execution_equity": execution_equity,
                    "execution_weights": execution_weights,
                    "execution_gross_exposure": execution_gross_exposure,
                    "equity": equity,
                    "positions": dict(sorted(portfolio.positions.items())),
                    "weights": close_weights,
                    "gross_exposure": gross_exposure,
                    "risk_state": risk_decision.state,
                }
            )

            for symbol in market.symbols:
                row = symbol_rows[symbol]
                row.update(
                    {
                        "execution_time": execution_snapshot.timestamp.isoformat(),
                        "risk_target_weight": risk_decision.target_weights.get(
                            symbol, 0.0
                        ),
                        "execution_target_weight": execution_targets[symbol],
                        "current_weight_at_execution": current_execution_weights[symbol],
                        "risk_reason": risk_decision.reason,
                        "risk_state": risk_decision.state,
                        "risk_force_execution": risk_decision.force_execution,
                        "should_rebalance": should_rebalance[symbol],
                        "filled": bool(day_fills_by_symbol[symbol]),
                        "fills": day_fills_by_symbol[symbol],
                        "portfolio_execution_gross_exposure": execution_gross_exposure,
                    }
                )
                decisions.append(row)

        metrics = compute_metrics(
            equity_curve,
            self.settings.initial_cash,
            fills,
        )
        final_snapshot = market.close_snapshot(market.timestamps[-1])
        final_weights = portfolio.weights(final_snapshot, set(market.symbols))
        return {
            "name": variant_name,
            "symbols": list(market.symbols),
            "settings": asdict(self.settings),
            "initial_cash": self.settings.initial_cash,
            "final_equity": metrics["final_equity"],
            "metrics": metrics,
            "cash": portfolio.cash,
            "positions": dict(sorted(portfolio.positions.items())),
            "final_weights": final_weights,
            "gross_exposure": sum(abs(value) for value in final_weights.values()),
            "fills": fills,
            "decisions": decisions,
            "equity_curve": equity_curve,
            "portfolio_history": portfolio_history,
            "market_semantics": market.market_semantics(),
            "corporate_action_events": corporate_action_events,
            "data_alignment": {
                "common_sessions": len(market.timestamps),
                "dropped_non_common_timestamps": market.dropped_timestamp_count,
                "first_timestamp": market.timestamps[0].isoformat(),
                "last_timestamp": market.timestamps[-1].isoformat(),
            },
        }
