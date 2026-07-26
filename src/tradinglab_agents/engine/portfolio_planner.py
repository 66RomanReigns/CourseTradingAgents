from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from tradinglab_agents.agents.context import ContextAnalystAgent
from tradinglab_agents.agents.critic import CriticAgent
from tradinglab_agents.agents.quant import QuantSignalAgent
from tradinglab_agents.agents.regime import RegimeAssessment, RegimeGuardAgent
from tradinglab_agents.config import BacktestSettings
from tradinglab_agents.data.evidence_provider import LocalPointInTimeEvidenceProvider
from tradinglab_agents.data.news_provider import LocalNewsProvider
from tradinglab_agents.engine.features import FeatureEngine
from tradinglab_agents.engine.fusion import DecisionFusion
from tradinglab_agents.engine.market import AlignedMarketData
from tradinglab_agents.models import (
    Action,
    AgentOpinion,
    EvidencePack,
    MarketSnapshot,
    Portfolio,
    PortfolioRiskDecision,
    TradeIntent,
)
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
    graph_runtime: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PortfolioPlanningContext:
    market: AlignedMarketData
    timestamp: datetime
    portfolio: Portfolio
    news_providers: dict[str, LocalNewsProvider]
    evidence_providers: tuple[LocalPointInTimeEvidenceProvider, ...]
    overlays: dict[str, dict[str, Any]]
    strategy_state: dict[str, Any]
    regime_state: dict[str, Any]
    close_snapshot: MarketSnapshot
    decision_time: datetime
    current_weights: dict[str, float]


@dataclass(frozen=True)
class SymbolPlanningResult:
    symbol: str
    proposed_target: float
    confidence: float
    decision: dict[str, Any]
    regime_state: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "proposed_target": self.proposed_target,
            "confidence": self.confidence,
            "decision": dict(self.decision),
            "regime_state": dict(self.regime_state),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> SymbolPlanningResult:
        return cls(
            symbol=str(payload["symbol"]).upper(),
            proposed_target=float(payload["proposed_target"]),
            confidence=float(payload["confidence"]),
            decision=dict(payload["decision"]),
            regime_state=dict(payload["regime_state"]),
        )


class PortfolioPlanner:
    """Reusable synchronized close-time planner for backtests and paper runs.

    The public stage methods are deliberately pure with respect to account and
    order persistence. v0.16 uses them inside LangGraph, while `plan()` keeps a
    sequential compatibility path for backtests, ablations and rollback.
    """

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

    def prepare_context(
        self,
        market: AlignedMarketData,
        timestamp: datetime,
        portfolio: Portfolio,
        *,
        news_providers: Mapping[str, LocalNewsProvider] | None = None,
        evidence_providers: Sequence[LocalPointInTimeEvidenceProvider] | None = None,
        strategy_state: Mapping[str, Any] | None = None,
        research_overlays: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> PortfolioPlanningContext:
        news = {
            symbol.upper(): provider for symbol, provider in (news_providers or {}).items()
        }
        unknown_news = set(news).difference(market.symbols)
        if unknown_news:
            raise ValueError(f"news providers contain unknown symbols: {sorted(unknown_news)}")
        evidence = tuple(evidence_providers or ())
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
        close_snapshot = market.close_snapshot(timestamp)
        decision_time = market.decision_time(timestamp)
        current_weights = portfolio.weights(close_snapshot, set(market.symbols))
        return PortfolioPlanningContext(
            market=market,
            timestamp=timestamp,
            portfolio=portfolio,
            news_providers=news,
            evidence_providers=evidence,
            overlays=overlays,
            strategy_state=state,
            regime_state=dict(state.get("regimes", {})),
            close_snapshot=close_snapshot,
            decision_time=decision_time,
            current_weights=current_weights,
        )

    def build_evidence_pack(
        self,
        context: PortfolioPlanningContext,
        symbol: str,
    ) -> EvidencePack:
        normalized = symbol.upper()
        visible = context.market.history(normalized, context.decision_time)
        if len(visible) < 21:
            raise ValueError(
                f"insufficient visible history for {normalized} at "
                f"{context.decision_time.isoformat()}"
            )
        pack = self.feature_engine.build(visible, context.decision_time)
        news_provider = context.news_providers.get(normalized)
        if self.settings.enable_context and news_provider is not None:
            news_provider.add_to_pack(pack)
        if self.settings.enable_context:
            for provider in context.evidence_providers:
                provider.add_to_pack(pack)
        return pack

    def quant_signal(self, pack: EvidencePack) -> AgentOpinion:
        return self.quant.analyze(pack)

    def fuse_and_criticize(
        self,
        pack: EvidencePack,
        quant_opinion: AgentOpinion,
    ) -> tuple[AgentOpinion | None, AgentOpinion, AgentOpinion, TradeIntent]:
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
        intent = self.fusion.to_intent(pack.symbol, reviewed)
        return context_opinion, combined, reviewed, intent

    def assess_regime(
        self,
        context: PortfolioPlanningContext,
        symbol: str,
        pack: EvidencePack,
    ) -> tuple[RegimeAssessment, dict[str, Any]]:
        regime = RegimeGuardAgent()
        regime.restore_state(context.regime_state.get(symbol.upper()))
        assessment = regime.assess(pack)
        return assessment, dict(regime.export_state())

    def apply_symbol_target(
        self,
        context: PortfolioPlanningContext,
        symbol: str,
        *,
        pack: EvidencePack,
        quant_opinion: AgentOpinion,
        context_opinion: AgentOpinion | None,
        combined: AgentOpinion,
        reviewed: AgentOpinion,
        intent: TradeIntent,
        regime: RegimeAssessment,
        next_regime_state: Mapping[str, Any],
    ) -> SymbolPlanningResult:
        normalized = symbol.upper()
        current_weight = context.current_weights[normalized]
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
            target = min(target, base_cap * regime.exposure_multiplier)

        overlay = context.overlays.get(normalized)
        overlay_note = "not supplied"
        if overlay is not None:
            research_action = str(overlay.get("action", "HOLD")).upper()
            research_confidence = float(overlay.get("confidence", 0.0))
            research_target = float(overlay.get("target_weight", current_weight))
            if research_action not in {"BUY", "HOLD", "SELL"}:
                raise ValueError(
                    f"invalid research action for {normalized}: {research_action}"
                )
            if not 0.0 <= research_confidence <= 1.0:
                raise ValueError(
                    f"invalid research confidence for {normalized}: {research_confidence}"
                )
            if not 0.0 <= research_target <= self.settings.max_position_weight:
                raise ValueError(
                    f"invalid research target for {normalized}: {research_target}"
                )
            original_target = target
            if research_action == "SELL":
                target = 0.0
                overlay_note = "research SELL forced protective zero target"
            elif (
                research_action == "HOLD"
                or research_confidence < self.settings.min_confidence
            ):
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

        proposed_target = max(0.0, target)
        decision = {
            "symbol": normalized,
            "decision_time": context.decision_time.isoformat(),
            "market_timestamp": context.timestamp.isoformat(),
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
            "proposed_target_weight": proposed_target,
            "regime": regime.label,
            "regime_exposure_multiplier": regime.exposure_multiplier,
            "regime_rationale": regime.rationale,
            "critic_rationale": (
                reviewed.rationale if self.settings.enable_critic else "DISABLED"
            ),
            "evidence_ids": sorted(intent.evidence_ids),
            "available_evidence_ids": sorted(pack.ids),
            "evidence_count": len(pack.evidence),
            "research_overlay": dict(overlay) if overlay is not None else None,
            "research_overlay_policy": overlay_note,
        }
        return SymbolPlanningResult(
            symbol=normalized,
            proposed_target=proposed_target,
            confidence=intent.confidence,
            decision=decision,
            regime_state=dict(next_regime_state),
        )

    def analyze_symbol(
        self,
        context: PortfolioPlanningContext,
        symbol: str,
    ) -> SymbolPlanningResult:
        pack = self.build_evidence_pack(context, symbol)
        quant_opinion = self.quant_signal(pack)
        context_opinion, combined, reviewed, intent = self.fuse_and_criticize(
            pack,
            quant_opinion,
        )
        regime, next_regime_state = self.assess_regime(context, symbol, pack)
        return self.apply_symbol_target(
            context,
            symbol,
            pack=pack,
            quant_opinion=quant_opinion,
            context_opinion=context_opinion,
            combined=combined,
            reviewed=reviewed,
            intent=intent,
            regime=regime,
            next_regime_state=next_regime_state,
        )

    def finalize_plan(
        self,
        context: PortfolioPlanningContext,
        symbol_results: Sequence[SymbolPlanningResult | Mapping[str, Any]],
        *,
        graph_runtime: Mapping[str, Any] | None = None,
    ) -> PortfolioPlan:
        normalized = [
            result
            if isinstance(result, SymbolPlanningResult)
            else SymbolPlanningResult.from_dict(result)
            for result in symbol_results
        ]
        expected = sorted(context.market.symbols)
        actual = sorted(result.symbol for result in normalized)
        if expected != actual:
            raise ValueError(
                "portfolio planner symbol result mismatch: "
                f"expected={expected}, actual={actual}"
            )
        if len(set(actual)) != len(actual):
            raise ValueError("portfolio planner received duplicate symbol results")

        proposed_targets = {
            result.symbol: result.proposed_target for result in normalized
        }
        confidences = {result.symbol: result.confidence for result in normalized}
        symbol_rows = {result.symbol: dict(result.decision) for result in normalized}
        next_regime_state = {
            result.symbol: dict(result.regime_state) for result in normalized
        }

        risk = PortfolioRiskGovernor(
            max_position_weight=self.settings.max_position_weight,
            max_gross_exposure=self.settings.max_gross_exposure,
            max_positions=self.settings.max_positions,
            max_drawdown=self.settings.max_drawdown,
            min_confidence=self.settings.min_confidence,
        )
        risk.restore_state(context.strategy_state.get("risk_state", "ACTIVE"))
        if self.settings.enable_risk:
            risk_portfolio = Portfolio(
                cash=context.portfolio.cash,
                positions=dict(context.portfolio.positions),
                peak_equity=context.portfolio.peak_equity,
            )
            risk_decision = risk.review(
                proposed_targets,
                risk_portfolio,
                context.close_snapshot,
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
                state=str(context.strategy_state.get("risk_state", "ACTIVE")),
            )

        next_state = {
            "regimes": next_regime_state,
            "risk_state": risk_decision.state,
        }
        return PortfolioPlan(
            decision_time=context.decision_time.isoformat(),
            market_timestamp=context.timestamp.isoformat(),
            proposed_targets=proposed_targets,
            target_weights=dict(risk_decision.target_weights),
            confidences=confidences,
            symbol_decisions=symbol_rows,
            risk_decision=risk_decision,
            strategy_state=next_state,
            graph_runtime=dict(graph_runtime or {}),
        )

    def plan(
        self,
        market: AlignedMarketData,
        timestamp: datetime,
        portfolio: Portfolio,
        *,
        news_providers: Mapping[str, LocalNewsProvider] | None = None,
        evidence_providers: Sequence[LocalPointInTimeEvidenceProvider] | None = None,
        strategy_state: Mapping[str, Any] | None = None,
        research_overlays: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> PortfolioPlan:
        context = self.prepare_context(
            market,
            timestamp,
            portfolio,
            news_providers=news_providers,
            evidence_providers=evidence_providers,
            strategy_state=strategy_state,
            research_overlays=research_overlays,
        )
        results = [
            self.analyze_symbol(context, symbol) for symbol in context.market.symbols
        ]
        return self.finalize_plan(context, results)


__all__ = [
    "PortfolioPlan",
    "PortfolioPlanner",
    "PortfolioPlanningContext",
    "SymbolPlanningResult",
]
