from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class BacktestSettings:
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
    context_weight: float = 0.25
    quant_weight: float = 0.75

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "BacktestSettings":
        broker = data.get("broker", {})
        risk = data.get("risk", {})
        backtest = data.get("backtest", {})
        agents = data.get("agents", {})
        fusion = agents.get("fusion", {})
        return cls(
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
            enable_risk=True,
            context_weight=float(fusion.get("context_weight", cls.context_weight)),
            quant_weight=float(fusion.get("quant_weight", cls.quant_weight)),
        )


def load_settings(path: str | Path | None = None) -> BacktestSettings:
    if path is None:
        return BacktestSettings()
    config_path = Path(path)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("configuration root must be a mapping")
    return BacktestSettings.from_mapping(raw)
