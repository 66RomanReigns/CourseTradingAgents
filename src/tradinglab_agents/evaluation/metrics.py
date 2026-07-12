from __future__ import annotations

import math
from statistics import fmean, pstdev
from typing import Iterable


def _returns(values: list[float]) -> list[float]:
    return [values[i] / values[i - 1] - 1.0 for i in range(1, len(values)) if values[i - 1] != 0]


def max_drawdown(values: list[float]) -> float:
    peak = 0.0
    worst = 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, 1.0 - value / peak)
    return worst


def compute_metrics(
    equity_curve: Iterable[dict],
    initial_cash: float,
    fills: Iterable[dict] = (),
    periods_per_year: int = 252,
) -> dict[str, float | int]:
    points = list(equity_curve)
    values = [initial_cash, *[float(point["equity"]) for point in points]]
    daily = _returns(values)
    final_equity = values[-1]
    total_return = final_equity / initial_cash - 1.0 if initial_cash else 0.0
    periods = max(1, len(daily))
    annualized_return = (1.0 + total_return) ** (periods_per_year / periods) - 1.0 if total_return > -1 else -1.0
    annualized_volatility = pstdev(daily) * math.sqrt(periods_per_year) if len(daily) > 1 else 0.0
    mean_return = fmean(daily) if daily else 0.0
    sharpe = mean_return * periods_per_year / annualized_volatility if annualized_volatility else 0.0
    downside = [min(0.0, value) for value in daily]
    downside_deviation = pstdev(downside) * math.sqrt(periods_per_year) if len(downside) > 1 else 0.0
    sortino = mean_return * periods_per_year / downside_deviation if downside_deviation else 0.0
    drawdown = max_drawdown(values)
    calmar = annualized_return / drawdown if drawdown else 0.0
    win_rate = sum(value > 0 for value in daily) / len(daily) if daily else 0.0
    fills_list = list(fills)
    turnover_notional = sum(abs(float(fill["quantity"])) * float(fill["price"]) for fill in fills_list)
    average_equity = fmean(values) if values else initial_cash
    turnover = turnover_notional / average_equity if average_equity else 0.0
    fees = sum(float(fill.get("fee", 0.0)) for fill in fills_list)
    return {
        "final_equity": final_equity,
        "total_return": total_return,
        "annualized_return": annualized_return,
        "annualized_volatility": annualized_volatility,
        "sharpe": sharpe,
        "sortino": sortino,
        "max_drawdown": drawdown,
        "calmar": calmar,
        "win_rate": win_rate,
        "turnover": turnover,
        "trade_count": len(fills_list),
        "fees": fees,
    }
