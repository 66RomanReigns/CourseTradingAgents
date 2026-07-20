from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


_CONFIG_SCHEMA: dict[str, Any] = {
    "project": {
        "seed": None,
    },
    "llm": {
        "execution_mode": None,
        "provider": None,
        "model": None,
        "temperature": None,
        "thinking_mode": None,
        "api_key_env": None,
        "base_url_env": None,
        "timeout_seconds": None,
        "cache_dir": None,
        "log_path": None,
        "max_calls_per_run": None,
    },
    "agents": {
        "context": {"enabled": None},
        "critic": {"enabled": None},
        "regime": {"enabled": None},
        "fusion": {
            "quant_weight": None,
            "context_weight": None,
            "min_confidence": None,
            "buy_threshold": None,
            "sell_threshold": None,
            "conflict_penalty": None,
        },
    },
    "risk": {
        "enabled": None,
        "max_position_weight": None,
        "max_gross_exposure": None,
        "max_positions": None,
        "max_portfolio_drawdown": None,
    },
    "broker": {
        "initial_cash": None,
        "commission_rate": None,
        "slippage_bps": None,
    },
    "backtest": {
        "warmup_days": None,
        "rebalance_threshold": None,
        "cooldown_bars": None,
    },
    "paper": {
        "database_path": None,
        "data_dir": None,
        "default_account_id": None,
        "approval_policy": None,
        "lock_path": None,
        "timezone": None,
    },
    "workflow": {
        "mode": None,
        "symbols": None,
        "live_data_dir": None,
        "artifact_dir": None,
        "refresh_market": None,
        "refresh_news": None,
        "refresh_macro": None,
        "refresh_fundamentals": None,
        "run_research": None,
        "run_paper": None,
        "macro_series": None,
        "llm_candidate_limit": None,
    },
}


def _validate_config_keys(
    value: dict[str, Any],
    schema: dict[str, Any],
    prefix: str = "",
) -> None:
    unknown = sorted(set(value).difference(schema))
    if unknown:
        dotted = [f"{prefix}{name}" for name in unknown]
        raise ValueError(
            "unsupported configuration key(s): " + ", ".join(dotted)
        )
    for key, child_schema in schema.items():
        if key not in value:
            continue
        child = value[key]
        dotted = f"{prefix}{key}"
        if child_schema is None:
            if isinstance(child, dict):
                raise ValueError(f"configuration value must not be a mapping: {dotted}")
            continue
        if not isinstance(child, dict):
            raise ValueError(f"configuration section must be a mapping: {dotted}")
        _validate_config_keys(child, child_schema, f"{dotted}.")


@dataclass(frozen=True)
class BacktestSettings:
    seed: int = 42
    initial_cash: float = 100_000.0
    warmup_bars: int = 60
    commission_bps: float = 5.0
    slippage_bps: float = 5.0
    max_position_weight: float = 0.30
    max_gross_exposure: float = 0.90
    max_positions: int = 5
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
    llm_execution_mode: str = "dry_run"
    llm_provider: str = "zhipu"
    llm_model: str = "glm-4.7-flash"
    llm_temperature: float = 0.0
    llm_thinking_mode: str = "disabled"
    llm_api_key_env: str = "ZHIPU_API_KEY"
    llm_base_url_env: str = "ZHIPU_BASE_URL"
    llm_timeout_seconds: float = 60.0
    llm_cache_dir: str = "artifacts/llm_cache"
    llm_log_path: str = "artifacts/llm_calls.jsonl"
    llm_max_calls_per_run: int = 14
    paper_database_path: str = "artifacts/paper_trading.db"
    paper_data_dir: str = "data/multi_sample"
    paper_default_account_id: str = "demo-paper"
    paper_approval_policy: str = "ALL"
    paper_lock_path: str = "artifacts/paper_scheduler.lock"
    paper_timezone: str = "America/New_York"
    workflow_mode: str = "dry_run"
    workflow_symbols: tuple[str, ...] = ("SPY", "QQQ", "AAPL", "MSFT", "NVDA")
    workflow_live_data_dir: str = "data/real"
    workflow_artifact_dir: str = "artifacts/workflows"
    workflow_refresh_market: bool = True
    workflow_refresh_news: bool = True
    workflow_refresh_macro: bool = True
    workflow_refresh_fundamentals: bool = True
    workflow_run_research: bool = True
    workflow_run_paper: bool = True
    workflow_macro_series: tuple[str, ...] = ("DGS10", "CPIAUCSL", "UNRATE")
    workflow_llm_candidate_limit: int = 2

    def __post_init__(self) -> None:
        if self.initial_cash <= 0:
            raise ValueError("initial_cash must be positive")
        if self.warmup_bars < 20:
            raise ValueError("warmup_bars must be at least 20")
        if self.commission_bps < 0 or self.slippage_bps < 0:
            raise ValueError("transaction costs must be non-negative")
        if not 0.0 < self.max_position_weight <= 1.0:
            raise ValueError("max_position_weight must be in (0, 1]")
        if not 0.0 < self.max_gross_exposure <= 1.0:
            raise ValueError("max_gross_exposure must be in (0, 1]")
        if self.max_position_weight > self.max_gross_exposure:
            raise ValueError("max_position_weight cannot exceed max_gross_exposure")
        if self.max_positions < 1:
            raise ValueError("max_positions must be positive")
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
            raise ValueError(
                "buy_threshold must be positive and sell_threshold negative"
            )
        if not 0.0 <= self.conflict_penalty <= 1.0:
            raise ValueError("conflict_penalty must be in [0, 1]")
        if self.llm_execution_mode not in {"mock", "dry_run", "live"}:
            raise ValueError("llm_execution_mode must be mock, dry_run or live")
        if self.llm_provider not in {"mock", "openai_compatible", "zhipu"}:
            raise ValueError(
                "llm_provider must be mock, openai_compatible or zhipu"
            )
        if not self.llm_model.strip():
            raise ValueError("llm_model must not be empty")
        if not 0.0 <= self.llm_temperature <= 2.0:
            raise ValueError("llm_temperature must be in [0, 2]")
        if self.llm_thinking_mode not in {"enabled", "disabled"}:
            raise ValueError("llm_thinking_mode must be enabled or disabled")
        if self.llm_timeout_seconds <= 0:
            raise ValueError("llm_timeout_seconds must be positive")
        if self.llm_max_calls_per_run < 1:
            raise ValueError("llm_max_calls_per_run must be positive")
        if self.paper_approval_policy not in {"ALL", "RISK_AUTO", "NONE"}:
            raise ValueError("paper_approval_policy must be ALL, RISK_AUTO or NONE")
        if not self.paper_database_path.strip():
            raise ValueError("paper_database_path cannot be empty")
        if not self.paper_data_dir.strip():
            raise ValueError("paper_data_dir cannot be empty")
        if not self.paper_default_account_id.strip():
            raise ValueError("paper_default_account_id cannot be empty")
        if not self.paper_lock_path.strip():
            raise ValueError("paper_lock_path cannot be empty")
        if not self.paper_timezone.strip():
            raise ValueError("paper_timezone cannot be empty")
        if self.workflow_mode not in {"dry_run", "offline", "live"}:
            raise ValueError("workflow_mode must be dry_run, offline or live")
        if len(self.workflow_symbols) < 2:
            raise ValueError("workflow_symbols must contain at least two symbols")
        if len(set(self.workflow_symbols)) != len(self.workflow_symbols):
            raise ValueError("workflow_symbols cannot contain duplicates")
        if not self.workflow_live_data_dir.strip() or not self.workflow_artifact_dir.strip():
            raise ValueError("workflow paths cannot be empty")
        if self.workflow_llm_candidate_limit < 1:
            raise ValueError("workflow_llm_candidate_limit must be positive")
        if self.workflow_llm_candidate_limit > len(self.workflow_symbols):
            raise ValueError("workflow_llm_candidate_limit cannot exceed symbol count")

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "BacktestSettings":
        _validate_config_keys(data, _CONFIG_SCHEMA)
        project = data.get("project", {})
        llm = data.get("llm", {})
        broker = data.get("broker", {})
        risk = data.get("risk", {})
        backtest = data.get("backtest", {})
        paper = data.get("paper", {})
        workflow = data.get("workflow", {})
        agents = data.get("agents", {})
        fusion = agents.get("fusion", {})
        return cls(
            seed=int(project.get("seed", cls.seed)),
            initial_cash=float(broker.get("initial_cash", cls.initial_cash)),
            warmup_bars=int(backtest.get("warmup_days", cls.warmup_bars)),
            commission_bps=(
                float(broker.get("commission_rate", cls.commission_bps / 10_000))
                * 10_000
            ),
            slippage_bps=float(
                broker.get("slippage_bps", cls.slippage_bps)
            ),
            max_position_weight=float(
                risk.get("max_position_weight", cls.max_position_weight)
            ),
            max_gross_exposure=float(
                risk.get("max_gross_exposure", cls.max_gross_exposure)
            ),
            max_positions=int(risk.get("max_positions", cls.max_positions)),
            max_drawdown=float(
                risk.get("max_portfolio_drawdown", cls.max_drawdown)
            ),
            min_confidence=float(
                fusion.get("min_confidence", cls.min_confidence)
            ),
            rebalance_threshold=float(
                backtest.get("rebalance_threshold", cls.rebalance_threshold)
            ),
            cooldown_bars=int(
                backtest.get("cooldown_bars", cls.cooldown_bars)
            ),
            enable_context=bool(
                agents.get("context", {}).get("enabled", cls.enable_context)
            ),
            enable_critic=bool(
                agents.get("critic", {}).get("enabled", cls.enable_critic)
            ),
            enable_risk=bool(risk.get("enabled", cls.enable_risk)),
            enable_regime_guard=bool(
                agents.get("regime", {}).get(
                    "enabled", cls.enable_regime_guard
                )
            ),
            context_weight=float(
                fusion.get("context_weight", cls.context_weight)
            ),
            quant_weight=float(
                fusion.get("quant_weight", cls.quant_weight)
            ),
            buy_threshold=float(
                fusion.get("buy_threshold", cls.buy_threshold)
            ),
            sell_threshold=float(
                fusion.get("sell_threshold", cls.sell_threshold)
            ),
            conflict_penalty=float(
                fusion.get("conflict_penalty", cls.conflict_penalty)
            ),
            llm_execution_mode=str(
                llm.get("execution_mode", cls.llm_execution_mode)
            ).lower(),
            llm_provider=str(llm.get("provider", cls.llm_provider)).lower(),
            llm_model=str(llm.get("model", cls.llm_model)),
            llm_temperature=float(
                llm.get("temperature", cls.llm_temperature)
            ),
            llm_thinking_mode=str(
                llm.get("thinking_mode", cls.llm_thinking_mode)
            ).lower(),
            llm_api_key_env=str(
                llm.get("api_key_env", cls.llm_api_key_env)
            ),
            llm_base_url_env=str(
                llm.get("base_url_env", cls.llm_base_url_env)
            ),
            llm_timeout_seconds=float(
                llm.get("timeout_seconds", cls.llm_timeout_seconds)
            ),
            llm_cache_dir=str(
                llm.get("cache_dir", cls.llm_cache_dir)
            ),
            llm_log_path=str(llm.get("log_path", cls.llm_log_path)),
            llm_max_calls_per_run=int(
                llm.get("max_calls_per_run", cls.llm_max_calls_per_run)
            ),
            paper_database_path=str(
                paper.get("database_path", cls.paper_database_path)
            ),
            paper_data_dir=str(paper.get("data_dir", cls.paper_data_dir)),
            paper_default_account_id=str(
                paper.get("default_account_id", cls.paper_default_account_id)
            ),
            paper_approval_policy=str(
                paper.get("approval_policy", cls.paper_approval_policy)
            ).upper(),
            paper_lock_path=str(paper.get("lock_path", cls.paper_lock_path)),
            paper_timezone=str(paper.get("timezone", cls.paper_timezone)),
            workflow_mode=str(
                workflow.get("mode", cls.workflow_mode)
            ).lower(),
            workflow_symbols=tuple(
                str(symbol).strip().upper()
                for symbol in workflow.get("symbols", cls.workflow_symbols)
            ),
            workflow_live_data_dir=str(
                workflow.get("live_data_dir", cls.workflow_live_data_dir)
            ),
            workflow_artifact_dir=str(
                workflow.get("artifact_dir", cls.workflow_artifact_dir)
            ),
            workflow_refresh_market=bool(
                workflow.get("refresh_market", cls.workflow_refresh_market)
            ),
            workflow_refresh_news=bool(
                workflow.get("refresh_news", cls.workflow_refresh_news)
            ),
            workflow_refresh_macro=bool(
                workflow.get("refresh_macro", cls.workflow_refresh_macro)
            ),
            workflow_refresh_fundamentals=bool(
                workflow.get(
                    "refresh_fundamentals",
                    cls.workflow_refresh_fundamentals,
                )
            ),
            workflow_run_research=bool(
                workflow.get("run_research", cls.workflow_run_research)
            ),
            workflow_run_paper=bool(
                workflow.get("run_paper", cls.workflow_run_paper)
            ),
            workflow_macro_series=tuple(
                str(series).strip().upper()
                for series in workflow.get(
                    "macro_series", cls.workflow_macro_series
                )
            ),
            workflow_llm_candidate_limit=int(
                workflow.get(
                    "llm_candidate_limit",
                    cls.workflow_llm_candidate_limit,
                )
            ),
        )


def load_settings(path: str | Path | None = None) -> BacktestSettings:
    if path is None:
        return BacktestSettings()
    config_path = Path(path)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("configuration root must be a mapping")
    return BacktestSettings.from_mapping(raw)
