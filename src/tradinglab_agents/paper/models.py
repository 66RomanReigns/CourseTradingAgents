from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class AccountStatus(str, Enum):
    ACTIVE = "ACTIVE"
    HALTED = "HALTED"
    CLOSED = "CLOSED"


class ApprovalPolicy(str, Enum):
    ALL = "ALL"
    RISK_AUTO = "RISK_AUTO"
    NONE = "NONE"


class OrderStatus(str, Enum):
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"

    @property
    def terminal(self) -> bool:
        return self in {
            OrderStatus.REJECTED,
            OrderStatus.CANCELLED,
            OrderStatus.EXPIRED,
            OrderStatus.EXECUTED,
            OrderStatus.FAILED,
        }


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class DailyRunStatus(str, Enum):
    STARTED = "STARTED"
    OPEN_EXECUTED = "OPEN_EXECUTED"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


_ALLOWED_ORDER_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.PENDING_APPROVAL: frozenset(
        {
            OrderStatus.APPROVED,
            OrderStatus.REJECTED,
            OrderStatus.CANCELLED,
            OrderStatus.EXPIRED,
        }
    ),
    OrderStatus.APPROVED: frozenset(
        {
            OrderStatus.CANCELLED,
            OrderStatus.EXPIRED,
            OrderStatus.EXECUTED,
            OrderStatus.FAILED,
        }
    ),
    OrderStatus.REJECTED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
    OrderStatus.EXPIRED: frozenset(),
    OrderStatus.EXECUTED: frozenset(),
    OrderStatus.FAILED: frozenset(),
}


def validate_order_transition(current: OrderStatus, target: OrderStatus) -> None:
    if target not in _ALLOWED_ORDER_TRANSITIONS[current]:
        raise ValueError(f"invalid order transition: {current.value} -> {target.value}")


@dataclass(frozen=True)
class PaperAccount:
    account_id: str
    name: str
    base_currency: str
    initial_cash: float
    cash: float
    peak_equity: float
    status: AccountStatus
    risk_state: str
    approval_policy: ApprovalPolicy
    symbols: tuple[str, ...]
    positions: dict[str, int] = field(default_factory=dict)
    strategy_state: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_session: str | None = None


@dataclass(frozen=True)
class PaperOrder:
    order_id: str
    account_id: str
    run_id: str
    symbol: str
    side: OrderSide
    target_weight: float
    decision_time: datetime
    scheduled_for: datetime
    status: OrderStatus
    approval_required: bool
    force_execution: bool
    reason: str
    evidence_ids: tuple[str, ...] = ()
    created_at: datetime | None = None
    reviewed_at: datetime | None = None
    reviewer: str | None = None
    review_note: str | None = None
    executed_at: datetime | None = None


@dataclass(frozen=True)
class PaperFill:
    fill_id: str
    order_id: str
    account_id: str
    symbol: str
    quantity: int
    price: float
    fee: float
    timestamp: datetime


@dataclass(frozen=True)
class DailyRun:
    run_id: str
    account_id: str
    session_date: str
    open_at: datetime
    close_at: datetime
    status: DailyRunStatus
    orders_created: int = 0
    orders_executed: int = 0
    payload: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
