from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


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


class ResearchPipelineResult(StrictModel):
    symbol: str
    decision_time: str
    news: NewsAnalysis
    macro: MacroAnalysis
    fundamental: FundamentalAnalysis
    bull: DebateView
    bear: DebateView
    manager: ResearchDecision
    trader: TradePlan
