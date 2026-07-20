from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from types import MappingProxyType


class Action(str, Enum):
    BUY = "BUY"
    HOLD = "HOLD"
    SELL = "SELL"


@dataclass(frozen=True)
class Bar:
    symbol: str
    timestamp: datetime
    open_at: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    available_at: datetime


@dataclass(frozen=True)
class MarketSnapshot:
    """Immutable, validated prices for one synchronized market instant.

    Portfolio valuation is only allowed through a snapshot containing every
    non-zero holding. Missing prices are rejected instead of being interpreted
    as zero, which prevents silent equity and drawdown corruption.
    """

    timestamp: datetime
    prices: Mapping[str, float]
    field: str = "close"

    def __post_init__(self) -> None:
        if self.field not in {"open", "close", "mark"}:
            raise ValueError("snapshot field must be 'open', 'close' or 'mark'")
        normalized: dict[str, float] = {}
        for raw_symbol, raw_price in self.prices.items():
            symbol = str(raw_symbol).strip().upper()
            if not symbol:
                raise ValueError("snapshot symbol cannot be empty")
            price = float(raw_price)
            if not math.isfinite(price) or price <= 0:
                raise ValueError(f"invalid {self.field} price for {symbol}: {raw_price}")
            if symbol in normalized:
                raise ValueError(f"duplicate snapshot symbol: {symbol}")
            normalized[symbol] = price
        if not normalized:
            raise ValueError("market snapshot cannot be empty")
        object.__setattr__(self, "prices", MappingProxyType(normalized))

    @property
    def symbols(self) -> frozenset[str]:
        return frozenset(self.prices)

    def price(self, symbol: str) -> float:
        normalized = symbol.upper()
        try:
            return float(self.prices[normalized])
        except KeyError as exc:
            raise ValueError(
                f"missing {self.field} price for {normalized} at {self.timestamp.isoformat()}"
            ) from exc

    def require(self, symbols: set[str] | frozenset[str]) -> None:
        missing = sorted(symbol.upper() for symbol in symbols if symbol.upper() not in self.prices)
        if missing:
            raise ValueError(
                f"market snapshot missing prices for {missing} at {self.timestamp.isoformat()}"
            )


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
        if item.evidence_id in self.ids:
            raise ValueError(f"duplicate evidence id: {item.evidence_id}")
        self.evidence.append(item)

    @property
    def ids(self) -> set[str]:
        return {item.evidence_id for item in self.evidence}

    def get_float(self, evidence_id: str, default: float = 0.0) -> float:
        for item in self.evidence:
            if item.evidence_id == evidence_id:
                return float(item.value)
        return default

    def by_kind(self, kind: str) -> list[Evidence]:
        return [item for item in self.evidence if item.kind == kind]


@dataclass(frozen=True)
class AgentOpinion:
    agent: str
    action: Action
    confidence: float
    score: float
    rationale: str
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be in [0, 1]")


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
    force_execution: bool = False
    state: str = "ACTIVE"


@dataclass(frozen=True)
class PortfolioRiskDecision:
    approved: bool
    target_weights: dict[str, float]
    reason: str
    force_execution: bool = False
    state: str = "ACTIVE"


@dataclass(frozen=True)
class Fill:
    symbol: str
    quantity: int
    price: float
    fee: float
    timestamp: datetime

    @property
    def notional(self) -> float:
        return abs(self.quantity) * self.price


@dataclass
class Portfolio:
    cash: float
    positions: dict[str, int] = field(default_factory=dict)
    peak_equity: float = 0.0

    @property
    def held_symbols(self) -> frozenset[str]:
        return frozenset(
            symbol.upper() for symbol, quantity in self.positions.items() if quantity != 0
        )

    def _snapshot(
        self,
        prices: MarketSnapshot | Mapping[str, float],
        *,
        timestamp: datetime | None = None,
        field: str = "mark",
    ) -> MarketSnapshot:
        if isinstance(prices, MarketSnapshot):
            snapshot = prices
        else:
            snapshot = MarketSnapshot(
                timestamp=timestamp or datetime.min,
                prices=prices,
                field=field,
            )
        snapshot.require(self.held_symbols)
        return snapshot

    def market_value(self, prices: MarketSnapshot | Mapping[str, float]) -> float:
        snapshot = self._snapshot(prices)
        return sum(
            quantity * snapshot.price(symbol)
            for symbol, quantity in self.positions.items()
            if quantity != 0
        )

    def equity(self, prices: MarketSnapshot | Mapping[str, float]) -> float:
        return self.cash + self.market_value(prices)

    def weight(
        self,
        symbol: str,
        prices: MarketSnapshot | Mapping[str, float],
    ) -> float:
        snapshot = self._snapshot(prices)
        normalized = symbol.upper()
        equity = self.equity(snapshot)
        if equity <= 0:
            return 0.0
        quantity = self.positions.get(normalized, 0)
        if quantity == 0:
            return 0.0
        return quantity * snapshot.price(normalized) / equity

    def weights(
        self,
        prices: MarketSnapshot | Mapping[str, float],
        symbols: set[str] | frozenset[str] | None = None,
    ) -> dict[str, float]:
        snapshot = self._snapshot(prices)
        requested = set(symbol.upper() for symbol in (symbols or self.held_symbols))
        snapshot.require(self.held_symbols.union(requested))
        equity = self.equity(snapshot)
        if equity <= 0:
            return {symbol: 0.0 for symbol in sorted(requested)}
        return {
            symbol: self.positions.get(symbol, 0) * snapshot.price(symbol) / equity
            if self.positions.get(symbol, 0) != 0
            else 0.0
            for symbol in sorted(requested)
        }

    def gross_exposure(self, prices: MarketSnapshot | Mapping[str, float]) -> float:
        return sum(abs(weight) for weight in self.weights(prices).values())

    def clean_positions(self) -> None:
        self.positions = {
            symbol.upper(): quantity
            for symbol, quantity in self.positions.items()
            if quantity != 0
        }
