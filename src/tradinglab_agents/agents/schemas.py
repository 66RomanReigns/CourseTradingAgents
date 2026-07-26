from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EvidenceAnalysis(StrictModel):
    score: float = Field(ge=-1.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(min_length=1, max_length=4000)
    evidence_ids: tuple[str, ...] = ()
    catalysts: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()


class NewsAnalysis(EvidenceAnalysis):
    analysis_type: Literal["news"] = "news"


class MacroAnalysis(EvidenceAnalysis):
    analysis_type: Literal["macro"] = "macro"


class FundamentalAnalysis(EvidenceAnalysis):
    analysis_type: Literal["fundamental"] = "fundamental"


class DebateView(StrictModel):
    stance: Literal["bull", "bear"]
    score: float = Field(ge=-1.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    thesis: str = Field(min_length=1, max_length=4000)
    evidence_ids: tuple[str, ...] = ()
    supporting_points: tuple[str, ...] = ()
    counterpoints: tuple[str, ...] = ()


class DebateRound(StrictModel):
    round_index: int = Field(ge=1, le=5)
    bull: DebateView
    bear: DebateView


class ResearchDecision(StrictModel):
    action: Literal["BUY", "HOLD", "SELL"]
    confidence: float = Field(ge=0.0, le=1.0)
    target_weight: float = Field(ge=0.0, le=1.0)
    thesis: str = Field(min_length=1, max_length=4000)
    evidence_ids: tuple[str, ...] = ()
    dissent: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_action_weight(self) -> "ResearchDecision":
        if self.action == "SELL" and self.target_weight != 0.0:
            raise ValueError("SELL research decisions must target zero weight")
        if self.action == "HOLD" and self.target_weight > 0.45:
            raise ValueError("HOLD target_weight must not exceed 0.45")
        return self


class TradePlan(StrictModel):
    action: Literal["BUY", "HOLD", "SELL"]
    confidence: float = Field(ge=0.0, le=1.0)
    target_weight: float = Field(ge=0.0, le=1.0)
    order_type: Literal["MARKET_NEXT_OPEN", "NO_ORDER"]
    rationale: str = Field(min_length=1, max_length=4000)
    evidence_ids: tuple[str, ...] = ()
    requires_human_approval: bool = True

    @model_validator(mode="after")
    def validate_order_contract(self) -> "TradePlan":
        if self.action == "HOLD" and self.order_type != "NO_ORDER":
            raise ValueError("HOLD plans must use NO_ORDER")
        if self.action != "HOLD" and self.order_type != "MARKET_NEXT_OPEN":
            raise ValueError("directional plans must use MARKET_NEXT_OPEN")
        if self.action == "SELL" and self.target_weight != 0.0:
            raise ValueError("SELL plans must target zero weight")
        return self


class RiskReview(StrictModel):
    persona: Literal["aggressive", "balanced", "conservative"]
    verdict: Literal["APPROVE", "REDUCE", "VETO"]
    confidence: float = Field(ge=0.0, le=1.0)
    max_target_weight: float = Field(ge=0.0, le=0.20)
    rationale: str = Field(min_length=1, max_length=4000)
    evidence_ids: tuple[str, ...] = ()
    conditions: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_verdict(self) -> "RiskReview":
        if self.verdict == "VETO" and self.max_target_weight != 0.0:
            raise ValueError("VETO risk reviews must cap target weight at zero")
        return self


class PortfolioSymbolCap(StrictModel):
    symbol: str = Field(min_length=1, max_length=32)
    max_target_weight: float = Field(ge=0.0, le=0.20)
    rationale: str = Field(min_length=1, max_length=2000)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return value.strip().upper()


class PortfolioCommitteeReview(StrictModel):
    reviewer: Literal["correlation", "concentration"]
    confidence: float = Field(ge=0.0, le=1.0)
    max_gross_target: float = Field(ge=0.0, le=1.0)
    symbol_caps: tuple[PortfolioSymbolCap, ...]
    rationale: str = Field(min_length=1, max_length=4000)
    warnings: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_unique_caps(self) -> "PortfolioCommitteeReview":
        symbols = [item.symbol for item in self.symbol_caps]
        if len(symbols) != len(set(symbols)):
            raise ValueError("portfolio review symbol caps must be unique")
        return self


class PortfolioAllocationItem(StrictModel):
    symbol: str = Field(min_length=1, max_length=32)
    action: Literal["BUY", "HOLD", "SELL"]
    target_weight: float = Field(ge=0.0, le=0.20)
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(min_length=1, max_length=2000)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return value.strip().upper()

    @model_validator(mode="after")
    def validate_action_weight(self) -> "PortfolioAllocationItem":
        if self.action == "SELL" and self.target_weight != 0.0:
            raise ValueError("portfolio SELL allocations must target zero")
        if self.action == "HOLD" and self.target_weight > 0.20:
            raise ValueError("portfolio HOLD allocation exceeds the research cap")
        return self


class PortfolioSupervisorDecision(StrictModel):
    allocations: tuple[PortfolioAllocationItem, ...]
    gross_target: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(min_length=1, max_length=4000)
    dissent: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_allocation_set(self) -> "PortfolioSupervisorDecision":
        symbols = [item.symbol for item in self.allocations]
        if len(symbols) != len(set(symbols)):
            raise ValueError("portfolio supervisor allocations must be unique")
        total = sum(item.target_weight for item in self.allocations)
        if abs(total - self.gross_target) > 1e-8:
            raise ValueError("portfolio supervisor gross_target must equal allocation sum")
        return self


class GuardedPortfolioAllocation(StrictModel):
    allocations: tuple[PortfolioAllocationItem, ...]
    gross_target: float = Field(ge=0.0, le=1.0)
    constraint_notes: tuple[str, ...] = ()
    high_correlation_clusters: tuple[tuple[str, ...], ...] = ()

    @model_validator(mode="after")
    def validate_guarded_allocation(self) -> "GuardedPortfolioAllocation":
        symbols = [item.symbol for item in self.allocations]
        if len(symbols) != len(set(symbols)):
            raise ValueError("guarded portfolio allocations must be unique")
        total = sum(item.target_weight for item in self.allocations)
        if abs(total - self.gross_target) > 1e-8:
            raise ValueError("guarded portfolio gross_target must equal allocation sum")
        return self


class ResearchPipelineResult(StrictModel):
    symbol: str
    decision_time: str
    news: NewsAnalysis
    macro: MacroAnalysis
    fundamental: FundamentalAnalysis
    debate_rounds: tuple[DebateRound, ...]
    bull: DebateView
    bear: DebateView
    manager: ResearchDecision
    preliminary_trader: TradePlan
    risk_reviews: tuple[RiskReview, ...]
    trader: TradePlan
