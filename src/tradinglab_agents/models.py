from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Action(str, Enum):
    BUY = "BUY"
    HOLD = "HOLD"
    SELL = "SELL"


@dataclass(frozen=True)
class Bar:
    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    available_at: datetime


@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    kind: str
    timestamp: datetime
    available_at: datetime
    value: float | str
    source: str
    detail: str = ""


@dataclass
class EvidencePack:
    symbol: str
    decision_time: datetime
    evidence: list[Evidence] = field(default_factory=list)

    def add(self, item: Evidence) -> None:
        if item.available_at > self.decision_time:
            raise ValueError(
                f"future evidence rejected: {item.evidence_id} available at {item.available_at}"
            )
        self.evidence.append(item)

    def get_float(self, evidence_id: str, default: float = 0.0) -> float:
        for item in self.evidence:
            if item.evidence_id == evidence_id:
                return float(item.value)
        return default


@dataclass(frozen=True)
class AgentOpinion:
    agent: str
    action: Action
    confidence: float
    score: float
    rationale: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class TradeIntent:
    symbol: str
    action: Action
    confidence: float
    target_weight: float
    rationale: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    target_weight: float
    reason: str


@dataclass(frozen=True)
class Fill:
    symbol: str
    quantity: int
    price: float
    fee: float
    timestamp: datetime


@dataclass
class Portfolio:
    cash: float
    positions: dict[str, int] = field(default_factory=dict)
    peak_equity: float = 0.0

    def market_value(self, prices: dict[str, float]) -> float:
        return sum(qty * prices.get(symbol, 0.0) for symbol, qty in self.positions.items())

    def equity(self, prices: dict[str, float]) -> float:
        return self.cash + self.market_value(prices)
