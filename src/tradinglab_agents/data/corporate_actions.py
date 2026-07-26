from __future__ import annotations

import json
import math
from dataclasses import dataclass, replace
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping

from tradinglab_agents.engine.trading_calendar import ExchangeTradingCalendar
from tradinglab_agents.models import Bar, Portfolio


class CorporateActionType(str, Enum):
    SPLIT = "SPLIT"
    CASH_DIVIDEND = "CASH_DIVIDEND"


@dataclass(frozen=True)
class CorporateAction:
    action_id: str
    symbol: str
    action_type: CorporateActionType
    effective_at: datetime
    available_at: datetime
    split_ratio: float = 1.0
    cash_amount_per_share: float = 0.0
    currency: str = "USD"
    source: str = "local_corporate_action_file"

    def __post_init__(self) -> None:
        action_id = self.action_id.strip()
        symbol = self.symbol.strip().upper()
        currency = self.currency.strip().upper()
        if not action_id:
            raise ValueError("corporate action_id cannot be empty")
        if not symbol:
            raise ValueError("corporate action symbol cannot be empty")
        if not currency:
            raise ValueError("corporate action currency cannot be empty")
        if self.available_at > self.effective_at:
            raise ValueError(
                f"corporate action {action_id} was unavailable at its effective time"
            )
        if self.action_type == CorporateActionType.SPLIT:
            if not math.isfinite(self.split_ratio) or self.split_ratio <= 0:
                raise ValueError("split_ratio must be positive and finite")
            if abs(self.split_ratio - 1.0) <= 1e-12:
                raise ValueError("split_ratio cannot equal 1")
            if abs(self.cash_amount_per_share) > 1e-12:
                raise ValueError("split cannot define cash_amount_per_share")
        elif self.action_type == CorporateActionType.CASH_DIVIDEND:
            if not math.isfinite(self.cash_amount_per_share) or self.cash_amount_per_share <= 0:
                raise ValueError("cash dividend amount must be positive and finite")
            if abs(self.split_ratio - 1.0) > 1e-12:
                raise ValueError("cash dividend cannot define split_ratio")
        object.__setattr__(self, "action_id", action_id)
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "currency", currency)

    @property
    def effective_date(self) -> date:
        return self.effective_at.date()

    def as_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "symbol": self.symbol,
            "action_type": self.action_type.value,
            "effective_at": self.effective_at.isoformat(),
            "available_at": self.available_at.isoformat(),
            "split_ratio": self.split_ratio,
            "cash_amount_per_share": self.cash_amount_per_share,
            "currency": self.currency,
            "source": self.source,
        }


@dataclass(frozen=True)
class CorporateActionEffect:
    action_id: str
    symbol: str
    action_type: CorporateActionType
    quantity_before: int
    quantity_after: int
    cash_delta: float
    fractional_shares: float
    reference_price: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "symbol": self.symbol,
            "action_type": self.action_type.value,
            "quantity_before": self.quantity_before,
            "quantity_after": self.quantity_after,
            "cash_delta": self.cash_delta,
            "fractional_shares": self.fractional_shares,
            "reference_price": self.reference_price,
        }


def apply_corporate_actions(
    portfolio: Portfolio,
    actions: Iterable[CorporateAction],
    reference_prices: Mapping[str, float],
) -> tuple[CorporateActionEffect, ...]:
    """Apply known actions in memory before the session open.

    Fractional shares from reverse or non-integral splits are converted to cash
    at the supplied session-open reference price. The function is intentionally
    free of storage concerns; PaperTradingStore provides idempotency.
    """

    effects: list[CorporateActionEffect] = []
    for action in sorted(actions, key=lambda item: (item.symbol, item.action_id)):
        symbol = action.symbol
        quantity_before = int(portfolio.positions.get(symbol, 0))
        if quantity_before < 0:
            raise ValueError("corporate actions do not support short positions")
        reference_price = float(reference_prices.get(symbol, 0.0))
        if not math.isfinite(reference_price) or reference_price <= 0:
            raise ValueError(f"missing positive corporate-action price for {symbol}")
        quantity_after = quantity_before
        fractional = 0.0
        cash_delta = 0.0
        if action.action_type == CorporateActionType.SPLIT:
            exact_quantity = quantity_before * action.split_ratio
            quantity_after = math.floor(exact_quantity + 1e-12)
            fractional = max(0.0, exact_quantity - quantity_after)
            cash_delta = fractional * reference_price
            if quantity_after:
                portfolio.positions[symbol] = quantity_after
            else:
                portfolio.positions.pop(symbol, None)
        elif action.action_type == CorporateActionType.CASH_DIVIDEND:
            cash_delta = quantity_before * action.cash_amount_per_share
        portfolio.cash += cash_delta
        effects.append(
            CorporateActionEffect(
                action_id=action.action_id,
                symbol=symbol,
                action_type=action.action_type,
                quantity_before=quantity_before,
                quantity_after=quantity_after,
                cash_delta=cash_delta,
                fractional_shares=fractional,
                reference_price=reference_price,
            )
        )
    portfolio.clean_positions()
    return tuple(effects)


class LocalCorporateActionProvider:
    """Point-in-time corporate actions with deterministic split adjustment."""

    def __init__(
        self,
        path: str | Path,
        symbol: str,
        *,
        calendar: ExchangeTradingCalendar | None = None,
    ) -> None:
        self.path = Path(path)
        self.symbol = symbol.strip().upper()
        self.calendar = calendar or ExchangeTradingCalendar("XNYS")
        if not self.symbol:
            raise ValueError("corporate action provider symbol cannot be empty")
        self._actions = self._load()

    @classmethod
    def empty(
        cls,
        symbol: str,
        *,
        calendar: ExchangeTradingCalendar | None = None,
    ) -> "LocalCorporateActionProvider":
        instance = cls.__new__(cls)
        instance.path = Path("")
        instance.symbol = symbol.strip().upper()
        instance.calendar = calendar or ExchangeTradingCalendar("XNYS")
        instance._actions = tuple()
        return instance

    def _load(self) -> tuple[CorporateAction, ...]:
        if not self.path.is_file():
            raise FileNotFoundError(self.path)
        actions: list[CorporateAction] = []
        seen: set[str] = set()
        for line_number, line in enumerate(
            self.path.read_text(encoding="utf-8").splitlines(),
            1,
        ):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("row must be an object")
                raw_type = CorporateActionType(str(row["action_type"]).upper())
                effective_raw = row.get("effective_at")
                if effective_raw is None:
                    effective_date = date.fromisoformat(str(row["effective_date"]))
                    effective_at = self.calendar.session(effective_date).open_at
                else:
                    effective_at = datetime.fromisoformat(str(effective_raw))
                session = self.calendar.session(effective_at.date())
                if effective_at != session.open_at:
                    raise ValueError(
                        "corporate action effective_at must equal exchange session open"
                    )
                action = CorporateAction(
                    action_id=str(row["action_id"]),
                    symbol=str(row.get("symbol") or self.symbol),
                    action_type=raw_type,
                    effective_at=effective_at,
                    available_at=datetime.fromisoformat(str(row["available_at"])),
                    split_ratio=float(row.get("split_ratio", 1.0)),
                    cash_amount_per_share=float(
                        row.get("cash_amount_per_share", 0.0)
                    ),
                    currency=str(row.get("currency", "USD")),
                    source=str(row.get("source", "local_corporate_action_file")),
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError(
                    f"invalid corporate action {self.path}:{line_number}: {exc}"
                ) from exc
            if action.symbol != self.symbol:
                raise ValueError(
                    f"corporate action symbol mismatch: {action.symbol} != {self.symbol}"
                )
            if action.action_id in seen:
                raise ValueError(f"duplicate corporate action id: {action.action_id}")
            seen.add(action.action_id)
            actions.append(action)
        actions.sort(key=lambda item: (item.effective_at, item.action_id))
        return tuple(actions)

    @property
    def actions(self) -> tuple[CorporateAction, ...]:
        return self._actions

    def visible(self, decision_time: datetime) -> tuple[CorporateAction, ...]:
        return tuple(
            action for action in self._actions if action.available_at <= decision_time
        )

    def actions_for_session(
        self,
        session_date: date | str,
        *,
        as_of: datetime | None = None,
    ) -> tuple[CorporateAction, ...]:
        target = date.fromisoformat(session_date) if isinstance(session_date, str) else session_date
        session = self.calendar.session(target)
        cutoff = as_of or session.open_at
        return tuple(
            action
            for action in self._actions
            if action.effective_date == target and action.available_at <= cutoff
        )

    def split_factor(
        self,
        bar_date: date,
        decision_time: datetime,
    ) -> float:
        factor = 1.0
        for action in self._actions:
            if (
                action.action_type == CorporateActionType.SPLIT
                and bar_date < action.effective_date
                and action.effective_at <= decision_time
                and action.available_at <= decision_time
            ):
                factor *= action.split_ratio
        return factor

    def adjust_history(
        self,
        bars: Iterable[Bar],
        decision_time: datetime,
        *,
        include_splits: bool = True,
        include_cash_dividends: bool = True,
    ) -> list[Bar]:
        source = list(bars)
        if not source:
            return []
        price_factors = [1.0 for _ in source]
        volume_factors = [1.0 for _ in source]
        visible_actions = [
            action
            for action in self._actions
            if action.effective_at <= decision_time
            and action.available_at <= decision_time
        ]
        for action in visible_actions:
            prior_indexes = [
                index
                for index, bar in enumerate(source)
                if bar.timestamp.date() < action.effective_date
            ]
            if not prior_indexes:
                continue
            if action.action_type == CorporateActionType.SPLIT and include_splits:
                for index in prior_indexes:
                    price_factors[index] /= action.split_ratio
                    volume_factors[index] *= action.split_ratio
            elif (
                action.action_type == CorporateActionType.CASH_DIVIDEND
                and include_cash_dividends
            ):
                reference_close = source[prior_indexes[-1]].close
                factor = (
                    reference_close - action.cash_amount_per_share
                ) / reference_close
                if not 0.0 < factor <= 1.0:
                    raise ValueError(
                        f"invalid dividend adjustment factor for {action.action_id}: "
                        f"close={reference_close}, dividend={action.cash_amount_per_share}"
                    )
                for index in prior_indexes:
                    price_factors[index] *= factor

        adjusted: list[Bar] = []
        for index, bar in enumerate(source):
            price_factor = price_factors[index]
            volume_factor = volume_factors[index]
            if (
                abs(price_factor - 1.0) <= 1e-12
                and abs(volume_factor - 1.0) <= 1e-12
            ):
                adjusted.append(bar)
                continue
            adjusted.append(
                replace(
                    bar,
                    open=bar.open * price_factor,
                    high=bar.high * price_factor,
                    low=bar.low * price_factor,
                    close=bar.close * price_factor,
                    volume=bar.volume * volume_factor,
                )
            )
        return adjusted

    def adjust_history_for_splits(
        self,
        bars: Iterable[Bar],
        decision_time: datetime,
    ) -> list[Bar]:
        return self.adjust_history(
            bars,
            decision_time,
            include_splits=True,
            include_cash_dividends=False,
        )

    def summary(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "path": str(self.path) if str(self.path) else None,
            "action_count": len(self._actions),
            "split_count": sum(
                action.action_type == CorporateActionType.SPLIT
                for action in self._actions
            ),
            "cash_dividend_count": sum(
                action.action_type == CorporateActionType.CASH_DIVIDEND
                for action in self._actions
            ),
            "first_effective_at": (
                self._actions[0].effective_at.isoformat() if self._actions else None
            ),
            "last_effective_at": (
                self._actions[-1].effective_at.isoformat() if self._actions else None
            ),
        }


__all__ = [
    "CorporateAction",
    "CorporateActionEffect",
    "CorporateActionType",
    "LocalCorporateActionProvider",
    "apply_corporate_actions",
]
