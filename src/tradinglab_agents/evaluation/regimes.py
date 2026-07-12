from __future__ import annotations

import math
from collections import defaultdict
from statistics import fmean, pstdev

from tradinglab_agents.data.csv_provider import LocalCsvProvider
from tradinglab_agents.evaluation.metrics import max_drawdown


REGIMES = ("bull", "bear", "sideways", "volatile")


def classify_market_regimes(
    provider: LocalCsvProvider,
    lookback: int = 20,
    trend_threshold: float = 0.05,
    volatility_threshold: float = 0.35,
) -> dict[str, str]:
    """Classify each close using trailing-only information.

    Labels are intentionally simple and interpretable. Volatile takes precedence;
    otherwise the trailing lookback return determines bull/bear/sideways.
    """
    bars = list(provider.bars)
    labels: dict[str, str] = {}
    closes = [bar.close for bar in bars]
    for index, bar in enumerate(bars):
        if index < lookback:
            labels[bar.timestamp.isoformat()] = "sideways"
            continue
        window = closes[index - lookback : index + 1]
        returns = [window[i] / window[i - 1] - 1.0 for i in range(1, len(window))]
        annualized_vol = pstdev(returns) * math.sqrt(252) if len(returns) > 1 else 0.0
        trailing_return = window[-1] / window[0] - 1.0
        if annualized_vol >= volatility_threshold:
            label = "volatile"
        elif trailing_return >= trend_threshold:
            label = "bull"
        elif trailing_return <= -trend_threshold:
            label = "bear"
        else:
            label = "sideways"
        labels[bar.timestamp.isoformat()] = label
    return labels


def compute_regime_metrics(equity_curve: list[dict], regime_labels: dict[str, str]) -> dict[str, dict]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for point in equity_curve:
        label = regime_labels.get(point["timestamp"], "sideways")
        grouped[label].append(float(point["equity"]))

    output: dict[str, dict] = {}
    for label in REGIMES:
        values = grouped.get(label, [])
        if not values:
            output[label] = {
                "observations": 0,
                "return": 0.0,
                "max_drawdown": 0.0,
                "positive_step_rate": 0.0,
                "average_step_return": 0.0,
            }
            continue
        step_returns = [values[i] / values[i - 1] - 1.0 for i in range(1, len(values)) if values[i - 1]]
        output[label] = {
            "observations": len(values),
            "return": values[-1] / values[0] - 1.0 if values[0] else 0.0,
            "max_drawdown": max_drawdown(values),
            "positive_step_rate": (
                sum(value > 0 for value in step_returns) / len(step_returns) if step_returns else 0.0
            ),
            "average_step_return": fmean(step_returns) if step_returns else 0.0,
        }
    return output


def attach_regime_metrics(result: dict, provider: LocalCsvProvider) -> dict:
    labels = classify_market_regimes(provider)
    result["regime_metrics"] = compute_regime_metrics(result.get("equity_curve", []), labels)
    return result
