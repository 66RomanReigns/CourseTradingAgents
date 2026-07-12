from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class BacktestSettings:
    seed: int = 42
    initial_cash: float = 100_000.0
    warmup_bars: int = 60
    commission_bps: float = 5.0
    slippage_bps: float = 5.0
    max_position_weight: float = 0.30
    max_drawdown: float = 0.15
    min_confidence: float = 0.45
    rebalance_threshold: float = 0.03
    cooldown_bars: int = 3
    enable_context: bool = True
    enable_critic: bool = True
    enable_risk: bool = True
    enable_regime_guard: bool = True
    context_weight: float = 0.25
    quant_weight: float = 0.75
    buy_threshold: float = 0.035
    sell_threshold: float = -0.035
    conflict_penalty: float = 0.70

    def __post_init__(self) -> None:
        if self.initial_cash <= 0:
            raise ValueError("initial_cash must be positive")
        if self.warmup_bars < 20:
            raise ValueError("warmup_bars must be at least 20")
        if self.commission_bps < 0 or self.slippage_bps < 0:
            raise ValueError("transaction costs must be non-negative")
        if not 0.0 < self.max_position_weight <= 1.0:
            raise ValueError("max_position_weight must be in (0, 1]")
        if not 0.0 < self.max_drawdown < 1.0:
            raise ValueError("max_drawdown must be in (0, 1)")
        if not 0.0 <= self.min_confidence <= 1.0:
            raise ValueError("min_confidence must be in [0, 1]")
        if not 0.0 <= self.rebalance_threshold <= 1.0:
            raise ValueError("rebalance_threshold must be in [0, 1]")
        if self.cooldown_bars < 0:
            raise ValueError("cooldown_bars must be non-negative")
        if self.quant_weight < 0 or self.context_weight < 0:
            raise ValueError("fusion weights must be non-negative")
        if self.quant_weight + self.context_weight <= 0:
            raise ValueError("fusion weights must sum to a positive value")
        if self.buy_threshold <= 0 or self.sell_threshold >= 0:
            raise ValueError("buy_threshold must be positive and sell_threshold negative")
        if not 0.0 <= self.conflict_penalty <= 1.0:
            raise ValueError("conflict_penalty must be in [0, 1]")

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "BacktestSettings":
        project = data.get("project", {})
        broker = data.get("broker", {})
        risk = data.get("risk", {})
        backtest = data.get("backtest", {})
        agents = data.get("agents", {})
        fusion = agents.get("fusion", {})
        return cls(
            seed=int(project.get("seed", cls.seed)),
            initial_cash=float(broker.get("initial_cash", cls.initial_cash)),
            warmup_bars=int(backtest.get("warmup_days", cls.warmup_bars)),
            commission_bps=float(broker.get("commission_rate", 0.0005)) * 10_000,
            slippage_bps=float(broker.get("slippage_bps", cls.slippage_bps)),
            max_position_weight=float(risk.get("max_position_weight", cls.max_position_weight)),
            max_drawdown=float(risk.get("max_portfolio_drawdown", cls.max_drawdown)),
            min_confidence=float(fusion.get("min_confidence", cls.min_confidence)),
            rebalance_threshold=float(backtest.get("rebalance_threshold", cls.rebalance_threshold)),
            cooldown_bars=int(backtest.get("cooldown_bars", cls.cooldown_bars)),
            enable_context=bool(agents.get("context", {}).get("enabled", cls.enable_context)),
            enable_critic=bool(agents.get("critic", {}).get("enabled", cls.enable_critic)),
            enable_risk=bool(risk.get("enabled", cls.enable_risk)),
            enable_regime_guard=bool(
                agents.get("regime", {}).get("enabled", cls.enable_regime_guard)
            ),
            context_weight=float(fusion.get("context_weight", cls.context_weight)),
            quant_weight=float(fusion.get("quant_weight", cls.quant_weight)),
            buy_threshold=float(fusion.get("buy_threshold", cls.buy_threshold)),
            sell_threshold=float(fusion.get("sell_threshold", cls.sell_threshold)),
            conflict_penalty=float(fusion.get("conflict_penalty", cls.conflict_penalty)),
        )


def load_settings(path: str | Path | None = None) -> BacktestSettings:
    if path is None:
        return BacktestSettings()
    config_path = Path(path)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("configuration root must be a mapping")
    return BacktestSettings.from_mapping(raw)
