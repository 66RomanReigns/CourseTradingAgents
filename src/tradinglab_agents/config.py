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
        "quick_model": None,
        "deep_model": None,
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
    "market": {
        "calendar": None,
        "strict_sessions": None,
        "strict_session_times": None,
        "require_complete_alignment": None,
        "corporate_actions_enabled": None,
        "corporate_action_suffix": None,
        "adjust_history_for_splits": None,
        "adjust_history_for_dividends": None,
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
        "network_preflight": None,
        "connectivity_probe_url": None,
        "provider_usage_database": None,
        "provider_graph_enabled": None,
        "provider_health_database": None,
        "provider_checkpoint_database": None,
        "provider_min_quality_score": None,
        "provider_block_quality_score": None,
        "provider_conflict_warn_relative_difference": None,
        "provider_conflict_block_relative_difference": None,
        "provider_shadow_validation_enabled": None,
        "provider_failure_policy": None,
        "stale_data_max_hours": None,
        "refresh_market": None,
        "refresh_news": None,
        "refresh_macro": None,
        "refresh_fundamentals": None,
        "run_research": None,
        "run_paper": None,
        "macro_series": None,
        "llm_candidate_limit": None,
        "debate_rounds": None,
        "risk_personas": None,
        "memory_feedback_enabled": None,
        "memory_feedback_limit": None,
        "research_runtime": None,
        "langgraph_checkpoint_database": None,
        "langchain_trace_path": None,
        "langgraph_event_path": None,
        "langgraph_retry_attempts": None,
        "langgraph_node_timeout_seconds": None,
        "langgraph_cost_aware_routing": None,
        "langgraph_hold_skip_confidence": None,
        "langgraph_human_review_mode": None,
        "portfolio_supervisor_enabled": None,
        "research_parent_graph_enabled": None,
        "research_parent_checkpoint_database": None,
        "workflow_core_graph_enabled": None,
        "workflow_core_checkpoint_database": None,
        "decision_graph_enabled": None,
        "decision_checkpoint_database": None,
        "portfolio_correlation_window": None,
        "portfolio_high_correlation_threshold": None,
        "portfolio_cluster_gross_cap": None,
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
    market_calendar_name: str = "XNYS"
    market_strict_sessions: bool = True
    market_strict_session_times: bool = True
    market_require_complete_alignment: bool = True
    market_corporate_actions_enabled: bool = True
    market_corporate_action_suffix: str = "_actions.jsonl"
    market_adjust_history_for_splits: bool = True
    market_adjust_history_for_dividends: bool = True
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
    llm_quick_model: str = "glm-4.7-flash"
    llm_deep_model: str = "glm-4.7-flash"
    llm_temperature: float = 0.0
    llm_thinking_mode: str = "disabled"
    llm_api_key_env: str = "ZHIPU_API_KEY"
    llm_base_url_env: str = "ZHIPU_BASE_URL"
    llm_timeout_seconds: float = 60.0
    llm_cache_dir: str = "artifacts/llm_cache"
    llm_log_path: str = "artifacts/llm_calls.jsonl"
    llm_max_calls_per_run: int = 29
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
    workflow_network_preflight: bool = True
    workflow_connectivity_probe_url: str = (
        "http://connectivitycheck.gstatic.com/generate_204"
    )
    workflow_provider_usage_database: str = "artifacts/provider_usage.db"
    workflow_provider_graph_enabled: bool = True
    workflow_provider_health_database: str = "artifacts/provider_health.db"
    workflow_provider_checkpoint_database: str = (
        "artifacts/langgraph/data_provider_checkpoints.db"
    )
    workflow_provider_min_quality_score: float = 0.75
    workflow_provider_block_quality_score: float = 0.45
    workflow_provider_conflict_warn_relative_difference: float = 0.005
    workflow_provider_conflict_block_relative_difference: float = 0.02
    workflow_provider_shadow_validation_enabled: bool = False
    workflow_provider_failure_policy: str = "fail_closed"
    workflow_stale_data_max_hours: float = 48.0
    workflow_refresh_market: bool = True
    workflow_refresh_news: bool = True
    workflow_refresh_macro: bool = True
    workflow_refresh_fundamentals: bool = True
    workflow_run_research: bool = True
    workflow_run_paper: bool = True
    workflow_macro_series: tuple[str, ...] = ("DGS10", "CPIAUCSL", "UNRATE")
    workflow_llm_candidate_limit: int = 2
    workflow_debate_rounds: int = 2
    workflow_risk_personas: tuple[str, ...] = (
        "aggressive",
        "balanced",
        "conservative",
    )
    workflow_memory_feedback_enabled: bool = True
    workflow_memory_feedback_limit: int = 5
    workflow_research_runtime: str = "langgraph"
    workflow_langgraph_checkpoint_database: str = (
        "artifacts/langgraph/research_checkpoints.db"
    )
    workflow_langchain_trace_path: str = "artifacts/langchain_events.jsonl"
    workflow_langgraph_event_path: str = "artifacts/langgraph_events.jsonl"
    workflow_langgraph_retry_attempts: int = 2
    workflow_langgraph_node_timeout_seconds: float = 120.0
    workflow_langgraph_cost_aware_routing: bool = False
    workflow_langgraph_hold_skip_confidence: float = 0.40
    workflow_langgraph_human_review_mode: str = "paper_queue"
    workflow_portfolio_supervisor_enabled: bool = True
    workflow_research_parent_graph_enabled: bool = True
    workflow_research_parent_checkpoint_database: str = (
        "artifacts/langgraph/research_parent_checkpoints.db"
    )
    workflow_core_graph_enabled: bool = True
    workflow_core_checkpoint_database: str = (
        "artifacts/langgraph/workflow_core_checkpoints.db"
    )
    workflow_decision_graph_enabled: bool = True
    workflow_decision_checkpoint_database: str = (
        "artifacts/langgraph/decision_checkpoints.db"
    )
    workflow_portfolio_correlation_window: int = 60
    workflow_portfolio_high_correlation_threshold: float = 0.80
    workflow_portfolio_cluster_gross_cap: float = 0.25

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
        if not self.market_calendar_name.strip():
            raise ValueError("market_calendar_name cannot be empty")
        if not self.market_corporate_action_suffix.strip():
            raise ValueError("market_corporate_action_suffix cannot be empty")
        if not self.market_corporate_action_suffix.endswith(".jsonl"):
            raise ValueError("market_corporate_action_suffix must end with .jsonl")
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
        if not self.llm_quick_model.strip() or not self.llm_deep_model.strip():
            raise ValueError("quick and deep LLM models must not be empty")
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
        if not self.workflow_connectivity_probe_url.startswith(("http://", "https://")):
            raise ValueError("workflow_connectivity_probe_url must be HTTP(S)")
        if not self.workflow_provider_usage_database.strip():
            raise ValueError("workflow_provider_usage_database cannot be empty")
        if not self.workflow_provider_health_database.strip():
            raise ValueError("workflow_provider_health_database cannot be empty")
        if not self.workflow_provider_checkpoint_database.strip():
            raise ValueError("workflow_provider_checkpoint_database cannot be empty")
        if not (
            0.0
            <= self.workflow_provider_block_quality_score
            <= self.workflow_provider_min_quality_score
            <= 1.0
        ):
            raise ValueError(
                "provider quality thresholds must satisfy 0 <= block <= minimum <= 1"
            )
        if not (
            0.0
            <= self.workflow_provider_conflict_warn_relative_difference
            <= self.workflow_provider_conflict_block_relative_difference
        ):
            raise ValueError(
                "provider conflict thresholds must satisfy 0 <= warn <= block"
            )
        if self.workflow_provider_failure_policy not in {
            "fail_closed",
            "last_known_good",
        }:
            raise ValueError(
                "workflow_provider_failure_policy must be fail_closed or last_known_good"
            )
        if self.workflow_stale_data_max_hours <= 0:
            raise ValueError("workflow_stale_data_max_hours must be positive")
        if self.workflow_llm_candidate_limit < 1:
            raise ValueError("workflow_llm_candidate_limit must be positive")
        if self.workflow_llm_candidate_limit > len(self.workflow_symbols):
            raise ValueError("workflow_llm_candidate_limit cannot exceed symbol count")
        if not 1 <= self.workflow_debate_rounds <= 3:
            raise ValueError("workflow_debate_rounds must be between 1 and 3")
        supported_personas = {"aggressive", "balanced", "conservative"}
        if not self.workflow_risk_personas:
            raise ValueError("workflow_risk_personas cannot be empty")
        if set(self.workflow_risk_personas).difference(supported_personas):
            raise ValueError("unsupported workflow risk persona")
        if len(set(self.workflow_risk_personas)) != len(self.workflow_risk_personas):
            raise ValueError("workflow_risk_personas cannot contain duplicates")
        if not 1 <= self.workflow_memory_feedback_limit <= 20:
            raise ValueError("workflow_memory_feedback_limit must be in [1, 20]")
        if self.workflow_research_runtime not in {"langgraph", "legacy_ablation"}:
            raise ValueError(
                "workflow_research_runtime must be langgraph or legacy_ablation"
            )
        if not self.workflow_langgraph_checkpoint_database.strip():
            raise ValueError("workflow_langgraph_checkpoint_database cannot be empty")
        if not self.workflow_langchain_trace_path.strip():
            raise ValueError("workflow_langchain_trace_path cannot be empty")
        if not self.workflow_langgraph_event_path.strip():
            raise ValueError("workflow_langgraph_event_path cannot be empty")
        if not 1 <= self.workflow_langgraph_retry_attempts <= 5:
            raise ValueError("workflow_langgraph_retry_attempts must be in [1, 5]")
        if self.workflow_langgraph_node_timeout_seconds <= 0:
            raise ValueError("workflow_langgraph_node_timeout_seconds must be positive")
        if not 0.0 <= self.workflow_langgraph_hold_skip_confidence <= 1.0:
            raise ValueError(
                "workflow_langgraph_hold_skip_confidence must be in [0, 1]"
            )
        if self.workflow_langgraph_human_review_mode not in {
            "paper_queue",
            "interrupt_directional",
        }:
            raise ValueError("unsupported workflow_langgraph_human_review_mode")
        if not self.workflow_research_parent_checkpoint_database.strip():
            raise ValueError(
                "workflow_research_parent_checkpoint_database cannot be empty"
            )
        if not self.workflow_core_checkpoint_database.strip():
            raise ValueError(
                "workflow_core_checkpoint_database cannot be empty"
            )
        if not self.workflow_decision_checkpoint_database.strip():
            raise ValueError(
                "workflow_decision_checkpoint_database cannot be empty"
            )
        if self.workflow_portfolio_correlation_window < 20:
            raise ValueError("workflow_portfolio_correlation_window must be at least 20")
        if not 0.0 <= self.workflow_portfolio_high_correlation_threshold <= 1.0:
            raise ValueError(
                "workflow_portfolio_high_correlation_threshold must be in [0, 1]"
            )
        if not 0.0 < self.workflow_portfolio_cluster_gross_cap <= self.max_gross_exposure:
            raise ValueError(
                "workflow_portfolio_cluster_gross_cap must be positive and no greater "
                "than max_gross_exposure"
            )

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "BacktestSettings":
        _validate_config_keys(data, _CONFIG_SCHEMA)
        project = data.get("project", {})
        llm = data.get("llm", {})
        broker = data.get("broker", {})
        risk = data.get("risk", {})
        backtest = data.get("backtest", {})
        market = data.get("market", {})
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
            market_calendar_name=str(
                market.get("calendar", cls.market_calendar_name)
            ).upper(),
            market_strict_sessions=bool(
                market.get("strict_sessions", cls.market_strict_sessions)
            ),
            market_strict_session_times=bool(
                market.get(
                    "strict_session_times",
                    cls.market_strict_session_times,
                )
            ),
            market_require_complete_alignment=bool(
                market.get(
                    "require_complete_alignment",
                    cls.market_require_complete_alignment,
                )
            ),
            market_corporate_actions_enabled=bool(
                market.get(
                    "corporate_actions_enabled",
                    cls.market_corporate_actions_enabled,
                )
            ),
            market_corporate_action_suffix=str(
                market.get(
                    "corporate_action_suffix",
                    cls.market_corporate_action_suffix,
                )
            ),
            market_adjust_history_for_splits=bool(
                market.get(
                    "adjust_history_for_splits",
                    cls.market_adjust_history_for_splits,
                )
            ),
            market_adjust_history_for_dividends=bool(
                market.get(
                    "adjust_history_for_dividends",
                    cls.market_adjust_history_for_dividends,
                )
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
            llm_quick_model=str(llm.get("quick_model", llm.get("model", cls.llm_quick_model))),
            llm_deep_model=str(llm.get("deep_model", llm.get("model", cls.llm_deep_model))),
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
            workflow_network_preflight=bool(
                workflow.get("network_preflight", cls.workflow_network_preflight)
            ),
            workflow_connectivity_probe_url=str(
                workflow.get(
                    "connectivity_probe_url",
                    cls.workflow_connectivity_probe_url,
                )
            ),
            workflow_provider_usage_database=str(
                workflow.get(
                    "provider_usage_database",
                    cls.workflow_provider_usage_database,
                )
            ),
            workflow_provider_graph_enabled=bool(
                workflow.get(
                    "provider_graph_enabled",
                    cls.workflow_provider_graph_enabled,
                )
            ),
            workflow_provider_health_database=str(
                workflow.get(
                    "provider_health_database",
                    cls.workflow_provider_health_database,
                )
            ),
            workflow_provider_checkpoint_database=str(
                workflow.get(
                    "provider_checkpoint_database",
                    cls.workflow_provider_checkpoint_database,
                )
            ),
            workflow_provider_min_quality_score=float(
                workflow.get(
                    "provider_min_quality_score",
                    cls.workflow_provider_min_quality_score,
                )
            ),
            workflow_provider_block_quality_score=float(
                workflow.get(
                    "provider_block_quality_score",
                    cls.workflow_provider_block_quality_score,
                )
            ),
            workflow_provider_conflict_warn_relative_difference=float(
                workflow.get(
                    "provider_conflict_warn_relative_difference",
                    cls.workflow_provider_conflict_warn_relative_difference,
                )
            ),
            workflow_provider_conflict_block_relative_difference=float(
                workflow.get(
                    "provider_conflict_block_relative_difference",
                    cls.workflow_provider_conflict_block_relative_difference,
                )
            ),
            workflow_provider_shadow_validation_enabled=bool(
                workflow.get(
                    "provider_shadow_validation_enabled",
                    cls.workflow_provider_shadow_validation_enabled,
                )
            ),
            workflow_provider_failure_policy=str(
                workflow.get(
                    "provider_failure_policy",
                    cls.workflow_provider_failure_policy,
                )
            ).lower(),
            workflow_stale_data_max_hours=float(
                workflow.get(
                    "stale_data_max_hours",
                    cls.workflow_stale_data_max_hours,
                )
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
            workflow_debate_rounds=int(
                workflow.get("debate_rounds", cls.workflow_debate_rounds)
            ),
            workflow_risk_personas=tuple(
                str(item).strip().lower()
                for item in workflow.get("risk_personas", cls.workflow_risk_personas)
            ),
            workflow_memory_feedback_enabled=bool(
                workflow.get(
                    "memory_feedback_enabled",
                    cls.workflow_memory_feedback_enabled,
                )
            ),
            workflow_memory_feedback_limit=int(
                workflow.get(
                    "memory_feedback_limit",
                    cls.workflow_memory_feedback_limit,
                )
            ),
            workflow_research_runtime=str(
                workflow.get("research_runtime", cls.workflow_research_runtime)
            ).lower(),
            workflow_langgraph_checkpoint_database=str(
                workflow.get(
                    "langgraph_checkpoint_database",
                    cls.workflow_langgraph_checkpoint_database,
                )
            ),
            workflow_langchain_trace_path=str(
                workflow.get(
                    "langchain_trace_path",
                    cls.workflow_langchain_trace_path,
                )
            ),
            workflow_langgraph_event_path=str(
                workflow.get(
                    "langgraph_event_path",
                    cls.workflow_langgraph_event_path,
                )
            ),
            workflow_langgraph_retry_attempts=int(
                workflow.get(
                    "langgraph_retry_attempts",
                    cls.workflow_langgraph_retry_attempts,
                )
            ),
            workflow_langgraph_node_timeout_seconds=float(
                workflow.get(
                    "langgraph_node_timeout_seconds",
                    cls.workflow_langgraph_node_timeout_seconds,
                )
            ),
            workflow_langgraph_cost_aware_routing=bool(
                workflow.get(
                    "langgraph_cost_aware_routing",
                    cls.workflow_langgraph_cost_aware_routing,
                )
            ),
            workflow_langgraph_hold_skip_confidence=float(
                workflow.get(
                    "langgraph_hold_skip_confidence",
                    cls.workflow_langgraph_hold_skip_confidence,
                )
            ),
            workflow_langgraph_human_review_mode=str(
                workflow.get(
                    "langgraph_human_review_mode",
                    cls.workflow_langgraph_human_review_mode,
                )
            ).lower(),
            workflow_portfolio_supervisor_enabled=bool(
                workflow.get(
                    "portfolio_supervisor_enabled",
                    cls.workflow_portfolio_supervisor_enabled,
                )
            ),
            workflow_research_parent_graph_enabled=bool(
                workflow.get(
                    "research_parent_graph_enabled",
                    cls.workflow_research_parent_graph_enabled,
                )
            ),
            workflow_research_parent_checkpoint_database=str(
                workflow.get(
                    "research_parent_checkpoint_database",
                    cls.workflow_research_parent_checkpoint_database,
                )
            ),
            workflow_core_graph_enabled=bool(
                workflow.get(
                    "workflow_core_graph_enabled",
                    cls.workflow_core_graph_enabled,
                )
            ),
            workflow_core_checkpoint_database=str(
                workflow.get(
                    "workflow_core_checkpoint_database",
                    cls.workflow_core_checkpoint_database,
                )
            ),
            workflow_decision_graph_enabled=bool(
                workflow.get(
                    "decision_graph_enabled",
                    cls.workflow_decision_graph_enabled,
                )
            ),
            workflow_decision_checkpoint_database=str(
                workflow.get(
                    "decision_checkpoint_database",
                    cls.workflow_decision_checkpoint_database,
                )
            ),
            workflow_portfolio_correlation_window=int(
                workflow.get(
                    "portfolio_correlation_window",
                    cls.workflow_portfolio_correlation_window,
                )
            ),
            workflow_portfolio_high_correlation_threshold=float(
                workflow.get(
                    "portfolio_high_correlation_threshold",
                    cls.workflow_portfolio_high_correlation_threshold,
                )
            ),
            workflow_portfolio_cluster_gross_cap=float(
                workflow.get(
                    "portfolio_cluster_gross_cap",
                    cls.workflow_portfolio_cluster_gross_cap,
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
