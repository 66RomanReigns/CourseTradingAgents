from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from tradinglab_agents.agents.context import ContextAnalystAgent
from tradinglab_agents.agents.critic import CriticAgent
from tradinglab_agents.agents.quant import QuantSignalAgent
from tradinglab_agents.agents.regime import RegimeGuardAgent
from tradinglab_agents.config import BacktestSettings
from tradinglab_agents.data.evidence_provider import LocalPointInTimeEvidenceProvider
from tradinglab_agents.data.news_provider import LocalNewsProvider
from tradinglab_agents.engine.features import FeatureEngine
from tradinglab_agents.engine.fusion import DecisionFusion
from tradinglab_agents.engine.market import AlignedMarketData
from tradinglab_agents.models import Action, Portfolio, PortfolioRiskDecision
from tradinglab_agents.risk.portfolio import PortfolioRiskGovernor


@dataclass(frozen=True)
class PortfolioPlan:
    decision_time: str
    market_timestamp: str
    proposed_targets: dict[str, float]
    target_weights: dict[str, float]
    confidences: dict[str, float]
    symbol_decisions: dict[str, dict[str, Any]]
    risk_decision: PortfolioRiskDecision
    strategy_state: dict[str, Any]


class PortfolioPlanner:
    """Reusable synchronized close-time planner for backtests and paper runs."""

    def __init__(self, settings: BacktestSettings | None = None):
        self.settings = settings or BacktestSettings()
        self.feature_engine = FeatureEngine()
        self.quant = QuantSignalAgent()
        self.context = ContextAnalystAgent()
        self.critic = CriticAgent()
        self.fusion = DecisionFusion(
            quant_weight=self.settings.quant_weight,
            context_weight=self.settings.context_weight,
            buy_threshold=self.settings.buy_threshold,
            sell_threshold=self.settings.sell_threshold,
            conflict_penalty=self.settings.conflict_penalty,
        )

    def plan(
        self,
        market: AlignedMarketData,
        timestamp,
        portfolio: Portfolio,
        *,
        news_providers: Mapping[str, LocalNewsProvider] | None = None,
        evidence_providers: Sequence[LocalPointInTimeEvidenceProvider] | None = None,
        strategy_state: Mapping[str, Any] | None = None,
        research_overlays: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> PortfolioPlan:
        news = {
            symbol.upper(): provider for symbol, provider in (news_providers or {}).items()
        }
        unknown_news = set(news).difference(market.symbols)
        if unknown_news:
            raise ValueError(f"news providers contain unknown symbols: {sorted(unknown_news)}")
        evidence_providers = tuple(evidence_providers or ())
        overlays = {
            symbol.upper(): dict(value)
            for symbol, value in (research_overlays or {}).items()
        }
        unknown_overlays = set(overlays).difference(market.symbols)
        if unknown_overlays:
            raise ValueError(
                f"research overlays contain unknown symbols: {sorted(unknown_overlays)}"
            )
        state = dict(strategy_state or {})
        regime_state = dict(state.get("regimes", {}))
        close_snapshot = market.close_snapshot(timestamp)
        decision_time = market.decision_time(timestamp)
        current_weights = portfolio.weights(close_snapshot, set(market.symbols))

        proposed_targets: dict[str, float] = {}
        confidences: dict[str, float] = {}
        symbol_rows: dict[str, dict[str, Any]] = {}
        next_regime_state: dict[str, Any] = {}

        for symbol in market.symbols:
            visible = market.history(symbol, decision_time)
            if len(visible) < 21:
                raise ValueError(
                    f"insufficient visible history for {symbol} at {decision_time.isoformat()}"
                )
            pack = self.feature_engine.build(visible, decision_time)
            news_provider = news.get(symbol)
            if self.settings.enable_context and news_provider is not None:
                news_provider.add_to_pack(pack)
            if self.settings.enable_context:
                for provider in evidence_providers:
                    provider.add_to_pack(pack)

            quant_opinion = self.quant.analyze(pack)
            has_context_evidence = any(
                item.kind in {"news", "macro", "fundamental"}
                for item in pack.evidence
            )
            context_opinion = (
                self.context.analyze(pack)
                if self.settings.enable_context and has_context_evidence
                else None
            )
            combined = self.fusion.combine(quant_opinion, context_opinion)
            reviewed = (
                self.critic.review(combined, pack)
                if self.settings.enable_critic
                else combined
            )
            intent = self.fusion.to_intent(symbol, reviewed)

            regime = RegimeGuardAgent()
            regime.restore_state(regime_state.get(symbol))
            assessment = regime.assess(pack)
            next_regime_state[symbol] = regime.export_state()

            current_weight = current_weights[symbol]
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

            overlay = overlays.get(symbol)
            overlay_note = "not supplied"
            if overlay is not None:
                research_action = str(overlay.get("action", "HOLD")).upper()
                research_confidence = float(overlay.get("confidence", 0.0))
                research_target = float(overlay.get("target_weight", current_weight))
                if research_action not in {"BUY", "HOLD", "SELL"}:
                    raise ValueError(
                        f"invalid research action for {symbol}: {research_action}"
                    )
                if not 0.0 <= research_confidence <= 1.0:
                    raise ValueError(
                        f"invalid research confidence for {symbol}: {research_confidence}"
                    )
                if not 0.0 <= research_target <= self.settings.max_position_weight:
                    raise ValueError(
                        f"invalid research target for {symbol}: {research_target}"
                    )
                original_target = target
                if research_action == "SELL":
                    target = 0.0
                    overlay_note = "research SELL forced protective zero target"
                elif research_action == "HOLD" or research_confidence < self.settings.min_confidence:
                    target = min(target, current_weight)
                    overlay_note = "research did not confirm an exposure increase"
                elif intent.action == Action.BUY:
                    target = min(
                        self.settings.max_position_weight,
                        0.5 * target + 0.5 * research_target,
                    )
                    overlay_note = "research BUY blended with deterministic target"
                else:
                    target = min(target, current_weight)
                    overlay_note = "research cannot create a position without quant confirmation"
                overlay_note += f" ({original_target:.2%}->{target:.2%})"

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
                "research_overlay": dict(overlay) if overlay is not None else None,
                "research_overlay_policy": overlay_note,
            }

        risk = PortfolioRiskGovernor(
            max_position_weight=self.settings.max_position_weight,
            max_gross_exposure=self.settings.max_gross_exposure,
            max_positions=self.settings.max_positions,
            max_drawdown=self.settings.max_drawdown,
            min_confidence=self.settings.min_confidence,
        )
        risk.restore_state(state.get("risk_state", "ACTIVE"))
        if self.settings.enable_risk:
            risk_decision = risk.review(
                proposed_targets,
                portfolio,
                close_snapshot,
                confidences=confidences,
            )
        else:
            gross = sum(proposed_targets.values())
            if gross > 1.0:
                scale = 1.0 / gross
                targets = {
                    symbol: weight * scale
                    for symbol, weight in proposed_targets.items()
                }
                reason = (
                    "portfolio risk governor disabled; "
                    f"broker cash invariant scaled gross {gross:.2%}->100.00%"
                )
            else:
                targets = proposed_targets
                reason = "portfolio risk governor disabled"
            risk_decision = PortfolioRiskDecision(
                approved=True,
                target_weights=targets,
                reason=reason,
                state=str(state.get("risk_state", "ACTIVE")),
            )

        next_state = {
            "regimes": next_regime_state,
            "risk_state": risk_decision.state,
        }
        return PortfolioPlan(
            decision_time=decision_time.isoformat(),
            market_timestamp=timestamp.isoformat(),
            proposed_targets=proposed_targets,
            target_weights=dict(risk_decision.target_weights),
            confidences=confidences,
            symbol_decisions=symbol_rows,
            risk_decision=risk_decision,
            strategy_state=next_state,
        )
