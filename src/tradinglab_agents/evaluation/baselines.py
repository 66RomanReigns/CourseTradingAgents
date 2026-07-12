from __future__ import annotations

from dataclasses import asdict
from statistics import fmean

from tradinglab_agents.broker.paper import PaperBroker
from tradinglab_agents.data.csv_provider import LocalCsvProvider
from tradinglab_agents.evaluation.metrics import compute_metrics
from tradinglab_agents.models import Portfolio


def _finish(name: str, provider: LocalCsvProvider, portfolio: Portfolio, fills: list[dict], equity_curve: list[dict], initial_cash: float) -> dict:
    metrics = compute_metrics(equity_curve, initial_cash, fills)
    return {
        "name": name,
        "symbol": provider.symbol,
        "initial_cash": initial_cash,
        "final_equity": metrics["final_equity"],
        "metrics": metrics,
        "fills": fills,
        "equity_curve": equity_curve,
    }


def run_buy_and_hold(
    provider: LocalCsvProvider,
    initial_cash: float = 100_000.0,
    warmup_bars: int = 60,
    commission_bps: float = 5.0,
    slippage_bps: float = 5.0,
) -> dict:
    bars = list(provider.bars)
    if len(bars) <= warmup_bars + 1:
        raise ValueError("insufficient bars for buy-and-hold baseline")
    portfolio = Portfolio(cash=initial_cash, peak_equity=initial_cash)
    broker = PaperBroker(commission_bps=commission_bps, slippage_bps=slippage_bps)
    entry = bars[warmup_bars + 1]
    fill = broker.rebalance(portfolio, provider.symbol, 0.999, entry.open, entry.open_at)
    fills = [asdict(fill)] if fill else []
    curve = [
        {"timestamp": bar.timestamp.isoformat(), "equity": portfolio.equity({provider.symbol: bar.close})}
        for bar in bars[warmup_bars + 1 :]
    ]
    return _finish("buy_and_hold", provider, portfolio, fills, curve, initial_cash)


def run_sma_cross(
    provider: LocalCsvProvider,
    initial_cash: float = 100_000.0,
    warmup_bars: int = 60,
    short_window: int = 10,
    long_window: int = 30,
    commission_bps: float = 5.0,
    slippage_bps: float = 5.0,
) -> dict:
    if short_window >= long_window:
        raise ValueError("short_window must be smaller than long_window")
    bars = list(provider.bars)
    start = max(warmup_bars, long_window)
    portfolio = Portfolio(cash=initial_cash, peak_equity=initial_cash)
    broker = PaperBroker(commission_bps=commission_bps, slippage_bps=slippage_bps)
    fills: list[dict] = []
    curve: list[dict] = []
    invested = False
    for index in range(start, len(bars) - 1):
        closes = [bar.close for bar in bars[: index + 1]]
        short = fmean(closes[-short_window:])
        long = fmean(closes[-long_window:])
        target = 0.95 if short > long else 0.0
        if (target > 0) != invested:
            next_bar = bars[index + 1]
            fill = broker.rebalance(portfolio, provider.symbol, target, next_bar.open, next_bar.open_at)
            if fill:
                fills.append(asdict(fill))
            invested = target > 0
        next_bar = bars[index + 1]
        curve.append(
            {"timestamp": next_bar.timestamp.isoformat(), "equity": portfolio.equity({provider.symbol: next_bar.close})}
        )
    return _finish("sma_cross", provider, portfolio, fills, curve, initial_cash)
