from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from tradinglab_agents.agents.llm import build_llm_client
from tradinglab_agents.agents.portfolio_supervisor import (
    PORTFOLIO_SUPERVISOR_CALLS,
    LangGraphPortfolioSupervisorRuntime,
    build_portfolio_market_context,
)
from tradinglab_agents.agents.quant import QuantSignalAgent
from tradinglab_agents.agents.research import (
    MultiAgentResearchPipeline,
    research_calls_per_symbol,
    research_graph_spec,
)
from tradinglab_agents.config import BacktestSettings
from tradinglab_agents.data.alpha_vantage import AlphaVantageNewsClient
from tradinglab_agents.data.connectivity import (
    NetworkAccessError,
    probe_external_access,
)
from tradinglab_agents.data.corporate_actions import LocalCorporateActionProvider
from tradinglab_agents.data.csv_provider import LocalCsvProvider
from tradinglab_agents.data.evidence_provider import LocalPointInTimeEvidenceProvider
from tradinglab_agents.data.fallback_router import (
    ProviderFallbackRouter,
    ProviderRequest,
)
from tradinglab_agents.data.fred import FredClient
from tradinglab_agents.data.news_provider import LocalNewsProvider
from tradinglab_agents.data.provider_adapters import ProviderAdapterFactory
from tradinglab_agents.data.provider_quality import QualityGateStatus
from tradinglab_agents.data.provider_registry import (
    DataKind,
    default_provider_registry,
)
from tradinglab_agents.data.sec_edgar import DEFAULT_US_GAAP_CONCEPTS, SecEdgarClient
from tradinglab_agents.data.twelve_data import TwelveDataClient
from tradinglab_agents.data.writers import (
    merge_bars_csv,
    merge_evidence_jsonl,
    merge_news_jsonl,
)
from tradinglab_agents.engine.features import FeatureEngine
from tradinglab_agents.engine.market import AlignedMarketData
from tradinglab_agents.engine.trading_calendar import ExchangeTradingCalendar
from tradinglab_agents.paper.scheduler import run_next_with_lock
from tradinglab_agents.paper.service import PaperTradingService
from tradinglab_agents.storage.paper_store import PaperTradingStore
from tradinglab_agents.storage.provider_health import ProviderHealthStore
from tradinglab_agents.storage.provider_usage import (
    ProviderCallTracker,
    ProviderState,
    ProviderUsageStore,
    classify_provider_exception,
)
from tradinglab_agents.workflows.data_provider_graph import (
    DataProviderGraphExecution,
    LangGraphDataProviderRuntime,
)
from tradinglab_agents.workflows.research_parent import (
    LangGraphResearchParentRuntime,
)
from tradinglab_agents.workflows.state import WorkflowStateStore
from tradinglab_agents.workflows.workflow_core import (
    LangGraphWorkflowCoreRuntime,
    WorkflowCoreExecution,
)


class _ProviderTrackedLLMClient:
    def __init__(self, delegate, tracker: ProviderCallTracker, provider: str):
        self.delegate = delegate
        self.tracker = tracker
        self.provider = provider

    @property
    def identity(self) -> str:
        return self.delegate.identity

    def complete(self, *, task, system_prompt, payload, response_model):
        started = time.perf_counter()
        try:
            result = self.delegate.complete(
                task=task,
                system_prompt=system_prompt,
                payload=payload,
                response_model=response_model,
            )
        except Exception as exc:
            self.tracker.record_state(
                provider=self.provider,
                operation=task,
                resource=getattr(response_model, "__name__", "structured_output"),
                status=classify_provider_exception(exc),
                units=1.0,
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
                error=f"{type(exc).__name__}: {exc}"[:2000],
            )
            raise
        metadata = self.delegate.last_call_metadata
        runtime_state = str(metadata.get("runtime_state", "LIVE_OK"))
        state = (
            ProviderState.CACHE_HIT
            if runtime_state == "CACHE_HIT"
            else ProviderState.LIVE_OK
        )
        self.tracker.record_state(
            provider=self.provider,
            operation=task,
            resource=getattr(response_model, "__name__", "structured_output"),
            status=state,
            units=0.0 if state == ProviderState.CACHE_HIT else 1.0,
            duration_ms=float(metadata.get("duration_ms", 0.0)),
            detail={
                "runtime_state": runtime_state,
                "usage": metadata.get("usage", {}),
            },
        )
        return result


class DailyWorkflow:
    """Data -> research -> internal paper-trading orchestration.

    Modes:
    - dry_run: no external requests and no paper-account mutation;
    - offline: local files, deterministic LLM execution and optional paper run;
    - live: explicit external refresh and live GLM execution, requiring confirmation.

    Every provider refresh and every research role is persisted as an atomic
    node artifact. A failed run can be resumed only when its mode and complete
    settings fingerprint are unchanged.
    """

    ETF_SYMBOLS = frozenset({"SPY", "QQQ"})

    def __init__(
        self,
        settings: BacktestSettings,
        project_root: str | Path,
    ) -> None:
        self.settings = settings
        self.root = Path(project_root).resolve()

    def _research_calls_per_symbol(self) -> int:
        return research_calls_per_symbol(
            debate_rounds=self.settings.workflow_debate_rounds,
            risk_personas=self.settings.workflow_risk_personas,
        )

    def _portfolio_supervisor_calls(self, candidate_count: int) -> int:
        return (
            PORTFOLIO_SUPERVISOR_CALLS
            if (
                self.settings.workflow_portfolio_supervisor_enabled
                and candidate_count >= 2
                and self.settings.workflow_run_research
            )
            else 0
        )

    def _resolve(self, value: str | Path) -> Path:
        path = Path(value)
        return path if path.is_absolute() else self.root / path

    def _mode(self, mode: str | None) -> str:
        selected = (mode or self.settings.workflow_mode).lower()
        if selected not in {"dry_run", "offline", "live"}:
            raise ValueError("workflow mode must be dry_run, offline or live")
        return selected

    def _settings_hash(self) -> str:
        payload = json.dumps(
            asdict(self.settings),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def plan(
        self,
        *,
        mode: str | None = None,
        account_id: str | None = None,
    ) -> dict[str, Any]:
        selected = self._mode(mode)
        symbols = list(self.settings.workflow_symbols)
        equities = [symbol for symbol in symbols if symbol not in self.ETF_SYMBOLS]
        candidate_count = min(
            self.settings.workflow_llm_candidate_limit,
            len(symbols),
        )
        symbol_research_calls = (
            self._research_calls_per_symbol() * candidate_count
            if self.settings.workflow_run_research
            else 0
        )
        portfolio_supervisor_calls = self._portfolio_supervisor_calls(candidate_count)
        llm_calls = symbol_research_calls + portfolio_supervisor_calls
        if llm_calls > self.settings.llm_max_calls_per_run:
            raise ValueError(
                f"planned LLM calls {llm_calls} exceed configured limit "
                f"{self.settings.llm_max_calls_per_run}"
            )
        credential_names = {
            "market_primary": "TWELVE_DATA_API_KEY",
            "market_fallback": "ALPHA_VANTAGE_API_KEY",
            "news": "ALPHA_VANTAGE_API_KEY",
            "macro": "FRED_API_KEY",
            "fundamentals": "SEC_USER_AGENT",
            "llm": self.settings.llm_api_key_env,
        }
        disabled = set(self.settings.workflow_disabled_providers)
        if "twelve_data" in disabled:
            credential_names.pop("market_primary", None)
        if "fred" in disabled:
            credential_names.pop("macro", None)
        steps = [
            {
                "name": "preflight",
                "external_calls": 0,
                "enabled": True,
                "description": "validate configuration, budgets, paths and local fixtures",
            },
            {
                "name": "network_preflight",
                "external_calls": 0,
                "enabled": selected == "live" and self.settings.workflow_network_preflight,
                "credential_free": True,
                "description": "detect captive portals and TLS interception before credentials",
            },
            {
                "name": "refresh_market",
                "external_calls": (
                    len(symbols) * 2
                    if self.settings.workflow_provider_graph_enabled
                    else len(symbols)
                ),
                "enabled": self.settings.workflow_refresh_market,
                "provider": "provider_graph",
                "provider_chain": [
                    provider
                    for provider in (
                        "twelve_data",
                        "alpha_vantage_market",
                        "local_market_cache",
                    )
                    if provider not in self.settings.workflow_disabled_providers
                ],
                "note": (
                    "maximum external attempts; local cache consumes zero quota"
                ),
            },
            {
                "name": "refresh_news",
                "external_calls": len(symbols),
                "enabled": self.settings.workflow_refresh_news,
                "provider": "provider_graph",
                "provider_chain": ["alpha_vantage", "local_news_cache"],
            },
            {
                "name": "refresh_macro",
                "external_calls": len(self.settings.workflow_macro_series),
                "enabled": self.settings.workflow_refresh_macro,
                "provider": "provider_graph",
                "provider_chain": [
                    provider
                    for provider in ("fred", "local_macro_cache")
                    if provider not in self.settings.workflow_disabled_providers
                ],
            },
            {
                "name": "refresh_fundamentals",
                "external_calls": len(equities) * 2,
                "enabled": self.settings.workflow_refresh_fundamentals,
                "provider": "provider_graph",
                "provider_chain": [
                    "sec_edgar",
                    "local_fundamentals_cache",
                ],
                "note": "approximate: ticker map plus company facts; ticker map is cacheable",
            },
            {
                "name": "candidate_screen",
                "external_calls": 0,
                "enabled": self.settings.workflow_run_research,
                "candidate_limit": candidate_count,
            },
            {
                "name": "structured_research",
                "external_calls": llm_calls,
                "enabled": self.settings.workflow_run_research,
                "provider": self.settings.llm_provider,
                "model": self.settings.llm_model,
                "quick_model": self.settings.llm_quick_model,
                "deep_model": self.settings.llm_deep_model,
                "debate_rounds": self.settings.workflow_debate_rounds,
                "risk_personas": list(self.settings.workflow_risk_personas),
                "memory_feedback": {
                    "enabled": self.settings.workflow_memory_feedback_enabled,
                    "max_matured_memories_per_symbol": (
                        self.settings.workflow_memory_feedback_limit
                    ),
                    "point_in_time_filter": True,
                },
                "calls_per_symbol": self._research_calls_per_symbol(),
                "symbol_research_calls": symbol_research_calls,
                "portfolio_supervisor": {
                    "enabled": self.settings.workflow_portfolio_supervisor_enabled,
                    "calls": portfolio_supervisor_calls,
                    "parallel_reviewers": ["correlation", "concentration"],
                    "final_supervisor_tier": "deep",
                    "deterministic_non_expansion_guard": True,
                    "correlation_window": (
                        self.settings.workflow_portfolio_correlation_window
                    ),
                    "high_correlation_threshold": (
                        self.settings.workflow_portfolio_high_correlation_threshold
                    ),
                    "cluster_gross_cap": (
                        self.settings.workflow_portfolio_cluster_gross_cap
                    ),
                },
                "graph": research_graph_spec(
                    debate_rounds=self.settings.workflow_debate_rounds,
                    risk_personas=self.settings.workflow_risk_personas,
                ),
                "runtime": self.settings.workflow_research_runtime,
                "langgraph": {
                    "workflow_core_graph": {
                        "enabled": self.settings.workflow_core_graph_enabled,
                        "thread_suffix": "WORKFLOW_CORE",
                        "checkpoint_database": (
                            self.settings.workflow_core_checkpoint_database
                        ),
                        "nodes": [
                            "data_validation",
                            "research_parent",
                            "overlay_assembly",
                            "decision_preparation",
                        ],
                        "provider_refresh_in_graph": False,
                        "paper_execution_in_graph": False,
                    },
                    "deterministic_decision_graph": {
                        "enabled": self.settings.workflow_decision_graph_enabled,
                        "thread_suffix": "DECISION",
                        "checkpoint_database": (
                            self.settings.workflow_decision_checkpoint_database
                        ),
                        "symbol_subgraph_nodes": [
                            "evidence_pack",
                            "quant_signal",
                            "fusion_critic",
                            "regime_guard",
                            "target_overlay",
                        ],
                        "portfolio_nodes": [
                            "portfolio_risk",
                            "finalize",
                        ],
                        "input_fingerprint": "sha256",
                        "plan_fingerprint": "sha256",
                        "account_mutation_in_graph": False,
                        "order_persistence_in_graph": False,
                    },
                    "research_parent_graph": {
                        "enabled": (
                            self.settings.workflow_research_parent_graph_enabled
                        ),
                        "dynamic_send_fan_out": True,
                        "thread_suffix": "RESEARCH_PARENT",
                        "checkpoint_database": (
                            self.settings.workflow_research_parent_checkpoint_database
                        ),
                        "staged_subgraphs": [
                            "symbol_research",
                            "portfolio_supervisor",
                        ],
                    },
                    "native_parallel_branches": True,
                    "cyclic_debate": True,
                    "conditional_routing": True,
                    "retry_attempts": self.settings.workflow_langgraph_retry_attempts,
                    "provider_timeout_seconds": (
                        self.settings.workflow_langgraph_node_timeout_seconds
                    ),
                    "checkpoint_database": (
                        self.settings.workflow_langgraph_checkpoint_database
                    ),
                    "cost_aware_routing": (
                        self.settings.workflow_langgraph_cost_aware_routing
                    ),
                    "human_review_mode": (
                        self.settings.workflow_langgraph_human_review_mode
                    ),
                },
                "langchain": {
                    "prompt_runnables": True,
                    "secret_free_trace_path": (
                        self.settings.workflow_langchain_trace_path
                    ),
                },
                "streaming": {
                    "mode": "updates",
                    "secret_free_event_path": (
                        self.settings.workflow_langgraph_event_path
                    ),
                    "state_payloads_logged": False,
                },
                "checkpoint_granularity": (
                    "LangGraph super-step plus one immutable JSON artifact per role"
                ),
            },
            {
                "name": "paper_session",
                "external_calls": 0,
                "enabled": self.settings.workflow_run_paper,
                "account_id": account_id or self.settings.paper_default_account_id,
                "external_broker": False,
            },
        ]
        planned_external = sum(
            int(step.get("external_calls", 0))
            for step in steps
            if bool(step.get("enabled"))
        )
        return {
            "mode": selected,
            "symbols": symbols,
            "remote_llm": {
                "provider": self.settings.llm_provider,
                "model": self.settings.llm_model,
                "quick_model": self.settings.llm_quick_model,
                "deep_model": self.settings.llm_deep_model,
                "execution_mode": self.settings.llm_execution_mode,
                "planned_calls": llm_calls,
                "max_calls_per_run": self.settings.llm_max_calls_per_run,
            },
            "external_requests_enabled": selected == "live",
            "planned_external_requests": planned_external,
            "provider_runtime": {
                "graph_enabled": self.settings.workflow_provider_graph_enabled,
                "thread_suffix": "DATA_PROVIDER",
                "checkpoint_database": (
                    self.settings.workflow_provider_checkpoint_database
                ),
                "health_database": self.settings.workflow_provider_health_database,
                "usage_database": self.settings.workflow_provider_usage_database,
                "network_preflight": self.settings.workflow_network_preflight,
                "failure_policy": self.settings.workflow_provider_failure_policy,
                "stale_data_max_hours": self.settings.workflow_stale_data_max_hours,
                "minimum_quality_score": (
                    self.settings.workflow_provider_min_quality_score
                ),
                "block_quality_score": (
                    self.settings.workflow_provider_block_quality_score
                ),
                "conflict_warn_relative_difference": (
                    self.settings.workflow_provider_conflict_warn_relative_difference
                ),
                "conflict_block_relative_difference": (
                    self.settings.workflow_provider_conflict_block_relative_difference
                ),
                "shadow_validation_enabled": (
                    self.settings.workflow_provider_shadow_validation_enabled
                ),
                "fallback_only_reduces_risk": True,
                "blocked_data_persisted": False,
                "credentials_logged": False,
                "capabilities": default_provider_registry()
                .excluding(self.settings.workflow_disabled_providers)
                .as_dict()["providers"],
            },
            "market_runtime": {
                "calendar": self.settings.market_calendar_name,
                "timezone": self.settings.paper_timezone,
                "strict_sessions": self.settings.market_strict_sessions,
                "strict_session_times": (
                    self.settings.market_strict_session_times
                ),
                "require_complete_alignment": (
                    self.settings.market_require_complete_alignment
                ),
                "corporate_actions_enabled": (
                    self.settings.market_corporate_actions_enabled
                ),
                "corporate_action_suffix": (
                    self.settings.market_corporate_action_suffix
                ),
                "adjust_history_for_splits": (
                    self.settings.market_adjust_history_for_splits
                ),
                "adjust_history_for_dividends": (
                    self.settings.market_adjust_history_for_dividends
                ),
                "raw_prices_used_for_execution": True,
                "corporate_actions_applied_before_open": True,
            },
            "credentials": {
                name: {
                    "env": env_name,
                    "present": bool(os.environ.get(env_name)),
                }
                for name, env_name in credential_names.items()
            },
            "steps": steps,
            "safety": {
                "real_broker_connected": False,
                "dry_run_mutates_account": False,
                "live_requires_explicit_confirmation": True,
                "checkpoint_resume_requires_identical_settings": True,
                "tls_verification_can_be_disabled": False,
                "credentials_sent_after_network_gate_only": True,
            },
        }

    def _market_paths(self, data_dir: Path) -> dict[str, Path]:
        return {
            symbol: data_dir / f"{symbol}.csv"
            for symbol in self.settings.workflow_symbols
        }

    def _validate_local_data(self, data_dir: Path) -> dict[str, Any]:
        paths = self._market_paths(data_dir)
        missing = [str(path) for path in paths.values() if not path.is_file()]
        if missing:
            raise ValueError("missing workflow market files: " + ", ".join(missing))
        calendar = ExchangeTradingCalendar(self.settings.market_calendar_name)
        providers = {
            symbol: LocalCsvProvider(path, symbol)
            for symbol, path in paths.items()
        }
        actions: dict[str, LocalCorporateActionProvider] = {}
        if self.settings.market_corporate_actions_enabled:
            for symbol in self.settings.workflow_symbols:
                action_path = (
                    data_dir
                    / f"{symbol}{self.settings.market_corporate_action_suffix}"
                )
                if action_path.is_file():
                    actions[symbol] = LocalCorporateActionProvider(
                        action_path,
                        symbol,
                        calendar=calendar,
                    )
        market = AlignedMarketData(
            providers,
            calendar=(calendar if self.settings.market_strict_sessions else None),
            strict_session_times=self.settings.market_strict_session_times,
            require_complete_alignment=(
                self.settings.market_require_complete_alignment
            ),
            corporate_actions=actions,
            adjust_history_for_splits=(
                self.settings.market_adjust_history_for_splits
            ),
            adjust_history_for_dividends=(
                self.settings.market_adjust_history_for_dividends
            ),
        )
        semantics = market.market_semantics()
        return {
            "status": "completed",
            "data_dir": str(data_dir),
            "market_files": len(providers),
            "synchronized_sessions": len(market.timestamps),
            "dropped_timestamp_count": market.dropped_timestamp_count,
            "first_session": market.timestamps[0].date().isoformat(),
            "last_session": market.timestamps[-1].date().isoformat(),
            "calendar": semantics["calendar"],
            "strict_session_times": semantics["strict_session_times"],
            "adjust_history_for_splits": semantics[
                "adjust_history_for_splits"
            ],
            "adjust_history_for_dividends": semantics[
                "adjust_history_for_dividends"
            ],
            "corporate_actions": semantics["corporate_actions"],
            "corporate_action_count": sum(
                int(item["action_count"])
                for item in semantics["corporate_actions"].values()
            ),
        }

    def _network_probe(self, tracker: ProviderCallTracker) -> dict[str, Any]:
        started = time.perf_counter()
        result = probe_external_access(
            self.settings.workflow_connectivity_probe_url,
            timeout_seconds=min(10.0, self.settings.llm_timeout_seconds),
        )
        tracker.record_state(
            provider="network",
            operation="connectivity_preflight",
            resource="external_https",
            status=(
                ProviderState.LIVE_OK if result.ok else ProviderState.NETWORK_BLOCKED
            ),
            detail={
                "state": result.state.value,
                "final_host": urlsplit(result.final_url or "").hostname,
                "http_status": result.http_status,
            },
            duration_ms=round((time.perf_counter() - started) * 1000, 3),
            error=None if result.ok else result.detail,
        )
        return result.to_dict()

    @staticmethod
    def _network_gate(payload: dict[str, Any]) -> dict[str, Any]:
        if bool(payload.get("ok")):
            return {"status": "completed", "state": payload.get("state")}
        raise NetworkAccessError(
            f"{payload.get('state', 'NETWORK_BLOCKED')}: {payload.get('detail', 'external access unavailable')}. "
            "Authenticate the server network or configure a trusted outbound proxy, then retry. "
            "TLS verification will not be disabled and provider credentials were not sent."
        )

    @staticmethod
    def _tracked_call(
        tracker: ProviderCallTracker | None,
        *,
        provider: str,
        operation: str,
        resource: str,
        units: float,
        function,
    ):
        if tracker is None:
            return function()
        return tracker.call(
            provider=provider,
            operation=operation,
            resource=resource,
            units=units,
            function=function,
        )

    @staticmethod
    def _file_row_count(path: Path) -> int:
        if not path.is_file():
            return 0
        with path.open("r", encoding="utf-8") as handle:
            count = sum(1 for line in handle if line.strip())
        return max(0, count - 1) if path.suffix.lower() == ".csv" else count

    def _last_known_good(
        self,
        path: Path,
        *,
        tracker: ProviderCallTracker | None,
        provider: str,
        operation: str,
        resource: str,
        exc: Exception,
    ) -> dict[str, Any] | None:
        if self.settings.workflow_provider_failure_policy != "last_known_good":
            return None
        if not path.is_file():
            return None
        age_hours = (
            datetime.now(timezone.utc).timestamp() - path.stat().st_mtime
        ) / 3600.0
        if age_hours > self.settings.workflow_stale_data_max_hours:
            return None
        rows = self._file_row_count(path)
        if rows <= 0:
            return None
        if tracker is not None:
            tracker.record_state(
                provider=provider,
                operation=operation,
                resource=resource,
                status=ProviderState.DEGRADED_STALE,
                detail={
                    "path": str(path),
                    "rows": rows,
                    "age_hours": round(age_hours, 3),
                    "failure_policy": "last_known_good",
                    "upstream_error_type": type(exc).__name__,
                },
            )
        return {
            "status": ProviderState.DEGRADED_STALE.value,
            "rows": rows,
            "path": str(path),
            "age_hours": round(age_hours, 3),
            "upstream_error_type": type(exc).__name__,
        }

    def _provider_requests(self, data_dir: Path) -> tuple[ProviderRequest, ...]:
        requests: list[ProviderRequest] = []
        today = datetime.now(timezone.utc).date()
        alpha_market_only = "twelve_data" in self.settings.workflow_disabled_providers
        if self.settings.workflow_refresh_market:
            for symbol in self.settings.workflow_symbols:
                path = data_dir / f"{symbol}.csv"
                start_date = None
                outputsize = 5000
                refresh_mode = "initial_full"
                alpha_vantage_outputsize = None
                if alpha_market_only:
                    outputsize = 100
                    refresh_mode = "initial_compact"
                    alpha_vantage_outputsize = "compact"
                if path.is_file():
                    existing = LocalCsvProvider(path, symbol)
                    start_date = existing.bars[-1].timestamp.date() - timedelta(days=7)
                    outputsize = 100
                    refresh_mode = "incremental_overlap"
                    alpha_vantage_outputsize = "compact"
                requests.append(
                    ProviderRequest(
                        request_id=f"market.{symbol}",
                        data_kind=DataKind.MARKET_DAILY,
                        resource=symbol,
                        shadow_validate=(
                            self.settings.workflow_provider_shadow_validation_enabled
                        ),
                        metadata={
                            "start_date": (
                                start_date.isoformat() if start_date else None
                            ),
                            "end_date": today.isoformat(),
                            "outputsize": outputsize,
                            "alpha_vantage_outputsize": alpha_vantage_outputsize,
                            "expected_min_records": 1,
                            "refresh_mode": refresh_mode,
                            "force_refresh": True,
                        },
                    )
                )
        if self.settings.workflow_refresh_news:
            for symbol in self.settings.workflow_symbols:
                path = data_dir / f"{symbol}_news.jsonl"
                time_from = None
                sort_order = "LATEST"
                refresh_mode = "initial_latest"
                if path.is_file():
                    events = LocalNewsProvider(path).events
                    if events:
                        time_from = max(
                            item.available_at for item in events
                        ) - timedelta(minutes=1)
                        sort_order = "EARLIEST"
                        refresh_mode = "incremental_overlap"
                requests.append(
                    ProviderRequest(
                        request_id=f"news.{symbol}",
                        data_kind=DataKind.NEWS,
                        resource=symbol,
                        metadata={
                            "time_from": (
                                time_from.isoformat() if time_from else None
                            ),
                            "sort": sort_order,
                            "limit": 200,
                            "refresh_mode": refresh_mode,
                            "force_refresh": True,
                        },
                    )
                )
        if self.settings.workflow_refresh_macro:
            for series_id in self.settings.workflow_macro_series:
                requests.append(
                    ProviderRequest(
                        request_id=f"macro.{series_id}",
                        data_kind=DataKind.MACRO,
                        resource=series_id,
                        metadata={"force_refresh": True},
                    )
                )
        if self.settings.workflow_refresh_fundamentals:
            for symbol in self.settings.workflow_symbols:
                if symbol in self.ETF_SYMBOLS:
                    continue
                requests.append(
                    ProviderRequest(
                        request_id=f"fundamentals.{symbol}",
                        data_kind=DataKind.FUNDAMENTALS,
                        resource=symbol,
                        metadata={
                            "concepts": list(DEFAULT_US_GAAP_CONCEPTS),
                            "force_refresh": True,
                        },
                    )
                )
        return tuple(requests)

    def _run_data_provider_graph(
        self,
        data_dir: Path,
        *,
        state: WorkflowStateStore,
        tracker: ProviderCallTracker,
    ) -> DataProviderGraphExecution:
        requests = self._provider_requests(data_dir)
        if not requests:
            raise ValueError("data provider graph has no enabled refresh requests")
        adapter = ProviderAdapterFactory(
            data_dir,
            calendar_name=self.settings.market_calendar_name,
        )
        router = ProviderFallbackRouter(
            registry=default_provider_registry().excluding(
                self.settings.workflow_disabled_providers
            ),
            health_store=ProviderHealthStore(
                self._resolve(self.settings.workflow_provider_health_database)
            ),
            run_id=state.run_id,
            fetchers=adapter.fetchers(),
            tracker=tracker,
            minimum_quality_score=(
                self.settings.workflow_provider_min_quality_score
            ),
            block_quality_score=(
                self.settings.workflow_provider_block_quality_score
            ),
            conflict_warn_relative_difference=(
                self.settings.workflow_provider_conflict_warn_relative_difference
            ),
            conflict_block_relative_difference=(
                self.settings.workflow_provider_conflict_block_relative_difference
            ),
        )
        runtime = LangGraphDataProviderRuntime(
            event_path=self._resolve(self.settings.workflow_langgraph_event_path)
        )
        return runtime.run(
            thread_id=f"{state.run_id}:DATA_PROVIDER",
            requests=requests,
            router=router,
            persist_result=adapter.persist_route_result,
            checkpointer_path=self._resolve(
                self.settings.workflow_provider_checkpoint_database
            ),
            resume=state.resume,
        )

    @staticmethod
    def _provider_execution_payload(
        execution: DataProviderGraphExecution,
    ) -> dict[str, Any]:
        routes = []
        for item in execution.route_results:
            selected = dict(item["selected"])
            routes.append(
                {
                    "request": dict(item["request"]),
                    "selected_provider": selected["provider"],
                    "record_count": selected["record_count"],
                    "payload_sha256": selected["payload_sha256"],
                    "fallback_depth": item["fallback_depth"],
                    "quality": dict(item["quality"]),
                    "attempts": list(item["attempts"]),
                    "secondary_provider": (
                        item["secondary"]["provider"]
                        if item.get("secondary")
                        else None
                    ),
                }
            )
        return {
            "status": "completed",
            "thread_id": execution.thread_id,
            "input_sha256": execution.input_sha256,
            "checkpoint_count": execution.checkpoint_count,
            "stream_event_count": execution.event_count,
            "execution_path": list(execution.execution_path),
            "checkpoint_database": (
                "artifacts/langgraph/data_provider_checkpoints.db"
            ),
            "quality": dict(execution.quality_summary),
            "routes": routes,
            "persisted_results": [
                dict(item) for item in execution.persisted_results
            ],
            "credentials_logged": False,
            "external_broker": False,
        }

    def _refresh_market(
        self,
        data_dir: Path,
        tracker: ProviderCallTracker | None = None,
    ) -> dict[str, Any]:
        data_dir.mkdir(parents=True, exist_ok=True)
        client = TwelveDataClient()
        result: dict[str, Any] = {}
        for symbol in self.settings.workflow_symbols:
            path = data_dir / f"{symbol}.csv"
            start_date = None
            end_date = None
            outputsize = 5000
            refresh_mode = "initial_full"
            if path.is_file():
                existing = LocalCsvProvider(path, symbol)
                latest_date = existing.bars[-1].timestamp.date()
                start_date = latest_date - timedelta(days=7)
                end_date = datetime.now(timezone.utc).date()
                outputsize = 100
                refresh_mode = "incremental_overlap"
            try:
                bars = self._tracked_call(
                    tracker,
                    provider="twelve_data",
                    operation="time_series_1day",
                    resource=symbol,
                    units=1.0,
                    function=lambda symbol=symbol, start_date=start_date, end_date=end_date, outputsize=outputsize: client.fetch_daily_bars(
                        symbol,
                        start_date=start_date,
                        end_date=end_date,
                        outputsize=outputsize,
                    ),
                )
            except Exception as exc:
                fallback = self._last_known_good(
                    path,
                    tracker=tracker,
                    provider="twelve_data",
                    operation="time_series_1day",
                    resource=symbol,
                    exc=exc,
                )
                if fallback is None:
                    raise
                result[symbol] = fallback
                continue
            result[symbol] = {
                "status": ProviderState.LIVE_OK.value,
                "rows": merge_bars_csv(bars, path),
                "path": str(path),
                "refresh_mode": refresh_mode,
                "fetched_rows": len(bars),
            }
        return result

    def _refresh_news(
        self,
        data_dir: Path,
        tracker: ProviderCallTracker | None = None,
    ) -> dict[str, Any]:
        data_dir.mkdir(parents=True, exist_ok=True)
        client = AlphaVantageNewsClient()
        result: dict[str, Any] = {}
        for index, symbol in enumerate(self.settings.workflow_symbols):
            if index:
                time.sleep(15.0)
            path = data_dir / f"{symbol}_news.jsonl"
            time_from = None
            sort_order = "LATEST"
            refresh_mode = "initial_latest"
            if path.is_file():
                existing_events = LocalNewsProvider(path).events
                if existing_events:
                    time_from = max(
                        event.available_at for event in existing_events
                    ) - timedelta(minutes=1)
                    sort_order = "EARLIEST"
                    refresh_mode = "incremental_overlap"
            try:
                events = self._tracked_call(
                    tracker,
                    provider="alpha_vantage",
                    operation="news_sentiment",
                    resource=symbol,
                    units=1.0,
                    function=lambda symbol=symbol, time_from=time_from, sort_order=sort_order: client.fetch_news(
                        symbol,
                        time_from=time_from,
                        limit=200,
                        sort=sort_order,
                    ),
                )
            except Exception as exc:
                fallback = self._last_known_good(
                    path,
                    tracker=tracker,
                    provider="alpha_vantage",
                    operation="news_sentiment",
                    resource=symbol,
                    exc=exc,
                )
                if fallback is None:
                    raise
                result[symbol] = fallback
                continue
            result[symbol] = {
                "status": ProviderState.LIVE_OK.value,
                "rows": merge_news_jsonl(events, path),
                "path": str(path),
                "refresh_mode": refresh_mode,
                "fetched_rows": len(events),
            }
        return result

    def _refresh_macro(
        self,
        data_dir: Path,
        tracker: ProviderCallTracker | None = None,
    ) -> dict[str, Any]:
        data_dir.mkdir(parents=True, exist_ok=True)
        records = []
        client = FredClient()
        path = data_dir / "macro.jsonl"
        try:
            for index, series_id in enumerate(self.settings.workflow_macro_series):
                if index:
                    time.sleep(1.0)
                records.extend(
                    self._tracked_call(
                        tracker,
                        provider="fred",
                        operation="series_observations_initial_release",
                        resource=series_id,
                        units=1.0,
                        function=lambda series_id=series_id: client.fetch_initial_release_records(
                            series_id, symbol="MACRO"
                        ),
                    )
                )
        except Exception as exc:
            fallback = self._last_known_good(
                path,
                tracker=tracker,
                provider="fred",
                operation="macro_bundle",
                resource=",".join(self.settings.workflow_macro_series),
                exc=exc,
            )
            if fallback is None:
                raise
            return fallback
        records.sort(key=lambda item: (item.available_at, item.evidence_id))
        return {
            "status": ProviderState.LIVE_OK.value,
            "rows": merge_evidence_jsonl(records, path),
            "path": str(path),
        }

    def _refresh_fundamentals(
        self,
        data_dir: Path,
        tracker: ProviderCallTracker | None = None,
    ) -> dict[str, Any]:
        data_dir.mkdir(parents=True, exist_ok=True)
        client = SecEdgarClient()
        result: dict[str, Any] = {}
        for symbol in self.settings.workflow_symbols:
            if symbol in self.ETF_SYMBOLS:
                continue
            path = data_dir / f"{symbol}_fundamentals.jsonl"
            try:
                records = self._tracked_call(
                    tracker,
                    provider="sec_edgar",
                    operation="company_facts",
                    resource=symbol,
                    units=2.0,
                    function=lambda symbol=symbol: client.fetch_fundamental_records(
                        symbol, concepts=DEFAULT_US_GAAP_CONCEPTS
                    ),
                )
            except Exception as exc:
                fallback = self._last_known_good(
                    path,
                    tracker=tracker,
                    provider="sec_edgar",
                    operation="company_facts",
                    resource=symbol,
                    exc=exc,
                )
                if fallback is None:
                    raise
                result[symbol] = fallback
                continue
            result[symbol] = {
                "status": ProviderState.LIVE_OK.value,
                "rows": merge_evidence_jsonl(records, path),
                "path": str(path),
            }
        return result

    def _refresh_live_data(self, data_dir: Path) -> dict[str, Any]:
        """Compatibility wrapper; production execution checkpoints providers separately."""

        return {
            "market": (
                self._refresh_market(data_dir)
                if self.settings.workflow_refresh_market
                else {}
            ),
            "news": (
                self._refresh_news(data_dir)
                if self.settings.workflow_refresh_news
                else {}
            ),
            "macro": (
                self._refresh_macro(data_dir)
                if self.settings.workflow_refresh_macro
                else None
            ),
            "fundamentals": (
                self._refresh_fundamentals(data_dir)
                if self.settings.workflow_refresh_fundamentals
                else {}
            ),
        }

    def _evidence_paths(self, data_dir: Path) -> list[Path]:
        paths: list[Path] = []
        macro = data_dir / "macro.jsonl"
        if macro.is_file():
            paths.append(macro)
        for symbol in self.settings.workflow_symbols:
            path = data_dir / f"{symbol}_fundamentals.jsonl"
            if path.is_file():
                paths.append(path)
        return paths

    def _select_candidates(self, data_dir: Path) -> list[dict[str, Any]]:
        engine = FeatureEngine()
        quant = QuantSignalAgent()
        scored: list[tuple[str, float]] = []
        for symbol in self.settings.workflow_symbols:
            provider = LocalCsvProvider(data_dir / f"{symbol}.csv", symbol)
            decision_bar = provider.bars[-1]
            pack = engine.build(
                provider.history(decision_bar.available_at),
                decision_bar.available_at,
            )
            opinion = quant.analyze(pack)
            priority = abs(opinion.score) * max(0.1, opinion.confidence)
            scored.append((symbol, priority))
        scored.sort(key=lambda item: (-item[1], item[0]))
        return [
            {"symbol": symbol, "priority": priority}
            for symbol, priority in scored[: self.settings.workflow_llm_candidate_limit]
        ]

    def _research_pack(
        self,
        data_dir: Path,
        symbol: str,
        evidence: list[LocalPointInTimeEvidenceProvider],
    ):
        provider = LocalCsvProvider(data_dir / f"{symbol}.csv", symbol)
        decision_bar = provider.bars[-1]
        pack = FeatureEngine().build(
            provider.history(decision_bar.available_at),
            decision_bar.available_at,
        )
        news_path = data_dir / f"{symbol}_news.jsonl"
        if news_path.is_file():
            LocalNewsProvider(news_path).add_to_pack(pack)
        for external in evidence:
            external.add_to_pack(pack)
        return pack

    def _decision_memory_context(
        self,
        *,
        account_id: str,
        symbol: str,
        decision_time: datetime,
    ) -> list[dict[str, Any]]:
        if not self.settings.workflow_memory_feedback_enabled:
            return []
        database = self._resolve(self.settings.paper_database_path)
        if not database.is_file():
            return []
        store = PaperTradingStore(database)
        if store.get_account(account_id) is None:
            return []
        rows = store.list_decision_memories(
            account_id,
            symbol=symbol,
            outcome_status="MATURED",
            limit=20,
        )
        visible: list[dict[str, Any]] = []
        for row in rows:
            raw_outcome = row.get("outcome_timestamp")
            if not raw_outcome:
                continue
            try:
                outcome_time = datetime.fromisoformat(str(raw_outcome))
            except ValueError:
                continue
            if outcome_time > decision_time:
                continue
            reflection = dict(row.get("reflection") or {})
            visible.append(
                {
                    "memory_id": row.get("memory_id"),
                    "decision_time": row.get("decision_time"),
                    "outcome_timestamp": raw_outcome,
                    "action": row.get("action"),
                    "raw_return": row.get("raw_return"),
                    "benchmark_return": row.get("benchmark_return"),
                    "alpha_return": row.get("alpha_return"),
                    "failure_type": row.get("failure_type"),
                    "lesson": reflection.get("lesson"),
                    "research_attribution": reflection.get("research_attribution"),
                }
            )
            if len(visible) >= self.settings.workflow_memory_feedback_limit:
                break
        return visible

    def _build_research_clients(
        self,
        *,
        mode: str,
        tracker: ProviderCallTracker | None,
    ):
        execution_mode = (
            "live"
            if mode == "live"
            else ("mock" if mode == "offline" else "dry_run")
        )
        client_settings = replace(
            self.settings,
            llm_execution_mode=execution_mode,
        )
        quick_client = build_llm_client(
            client_settings,
            self.root,
            model=self.settings.llm_quick_model,
            cache_namespace="quick",
        )
        deep_client = build_llm_client(
            client_settings,
            self.root,
            model=self.settings.llm_deep_model,
            cache_namespace="deep",
        )
        if mode == "live" and tracker is not None:
            quick_client = _ProviderTrackedLLMClient(
                quick_client,
                tracker,
                self.settings.llm_provider,
            )
            deep_client = _ProviderTrackedLLMClient(
                deep_client,
                tracker,
                self.settings.llm_provider,
            )
        return quick_client, deep_client

    def _run_research_parent(
        self,
        data_dir: Path,
        *,
        mode: str,
        state: WorkflowStateStore,
        account_id: str,
        tracker: ProviderCallTracker | None = None,
    ) -> dict[str, Any]:
        evidence = [
            LocalPointInTimeEvidenceProvider(path)
            for path in self._evidence_paths(data_dir)
        ]

        def run_symbol(candidate: dict[str, Any]) -> dict[str, Any]:
            symbol = str(candidate["symbol"]).upper()
            priority = float(candidate["priority"])
            quick_client, deep_client = self._build_research_clients(
                mode=mode,
                tracker=tracker,
            )
            pack = self._research_pack(data_dir, symbol, evidence)
            memory_context = self._decision_memory_context(
                account_id=account_id,
                symbol=symbol,
                decision_time=pack.decision_time,
            )
            pipeline = MultiAgentResearchPipeline(
                quick_client,
                deep_client,
                debate_rounds=self.settings.workflow_debate_rounds,
                risk_personas=self.settings.workflow_risk_personas,
                use_langgraph=(
                    self.settings.workflow_research_runtime == "langgraph"
                ),
                langchain_trace_path=self._resolve(
                    self.settings.workflow_langchain_trace_path
                ),
                langgraph_event_path=self._resolve(
                    self.settings.workflow_langgraph_event_path
                ),
                langgraph_retry_attempts=(
                    self.settings.workflow_langgraph_retry_attempts
                ),
                langgraph_node_timeout_seconds=(
                    self.settings.workflow_langgraph_node_timeout_seconds
                ),
                langgraph_cost_aware_routing=(
                    self.settings.workflow_langgraph_cost_aware_routing
                ),
                langgraph_hold_skip_confidence=(
                    self.settings.workflow_langgraph_hold_skip_confidence
                ),
                langgraph_human_review_mode=(
                    self.settings.workflow_langgraph_human_review_mode
                ),
            )
            result = pipeline.run_resumable(
                pack,
                memory_context=memory_context,
                node_runner=lambda node, model, function: state.run_model_node(
                    f"research.{symbol}.{node}",
                    model,
                    function,
                ),
                thread_id=f"{state.run_id}:{symbol}",
                checkpointer_path=self._resolve(
                    self.settings.workflow_langgraph_checkpoint_database
                ),
                resume=state.resume,
            )
            graph_execution = pipeline._last_graph_execution
            return {
                "symbol": symbol,
                "screen_priority": priority,
                "memory_context_count": len(memory_context),
                "memory_context_ids": [
                    str(item.get("memory_id")) for item in memory_context
                ],
                "clients": {
                    "quick": quick_client.identity,
                    "deep": deep_client.identity,
                },
                "runtime": self.settings.workflow_research_runtime,
                "langgraph": (
                    {
                        "thread_id": graph_execution.thread_id,
                        "checkpoint_count": graph_execution.checkpoint_count,
                        "execution_path": list(graph_execution.execution_path),
                        "human_review": graph_execution.latest_state.get(
                            "human_review", {}
                        ),
                        "interrupted": graph_execution.interrupted,
                        "stream_event_count": graph_execution.event_count,
                    }
                    if graph_execution is not None
                    else None
                ),
                "result": result.model_dump(mode="json"),
            }

        def run_portfolio(rows: list[dict[str, Any]]) -> dict[str, Any]:
            calls = self._portfolio_supervisor_calls(len(rows))
            if not calls:
                return {
                    "status": "disabled",
                    "enabled": self.settings.workflow_portfolio_supervisor_enabled,
                    "calls": 0,
                    "adjustments": {},
                }
            quick_client, deep_client = self._build_research_clients(
                mode=mode,
                tracker=tracker,
            )
            candidate_plans = [
                {
                    "symbol": str(row["symbol"]).upper(),
                    "screen_priority": float(row["screen_priority"]),
                    **dict(row["result"]["trader"]),
                }
                for row in sorted(rows, key=lambda item: str(item["symbol"]))
            ]
            providers = {
                str(row["symbol"]).upper(): LocalCsvProvider(
                    data_dir / f"{str(row['symbol']).upper()}.csv",
                    str(row["symbol"]).upper(),
                )
                for row in rows
            }
            market_context = state.run_json_node(
                "research.portfolio.market_context",
                lambda: build_portfolio_market_context(
                    providers,
                    window_sessions=(
                        self.settings.workflow_portfolio_correlation_window
                    ),
                    high_correlation_threshold=(
                        self.settings.workflow_portfolio_high_correlation_threshold
                    ),
                ),
            )
            runtime = LangGraphPortfolioSupervisorRuntime(
                quick_client,
                deep_client,
                trace_path=self._resolve(
                    self.settings.workflow_langchain_trace_path
                ),
                event_path=self._resolve(
                    self.settings.workflow_langgraph_event_path
                ),
                retry_attempts=self.settings.workflow_langgraph_retry_attempts,
                max_gross_target=self.settings.max_gross_exposure,
                max_positions=self.settings.max_positions,
                high_correlation_threshold=(
                    self.settings.workflow_portfolio_high_correlation_threshold
                ),
                cluster_gross_cap=(
                    self.settings.workflow_portfolio_cluster_gross_cap
                ),
            )
            execution = runtime.run(
                candidate_plans,
                market_context,
                thread_id=f"{state.run_id}:PORTFOLIO",
                node_runner=lambda node, model, function: state.run_model_node(
                    f"research.{node}",
                    model,
                    function,
                ),
                checkpointer_path=self._resolve(
                    self.settings.workflow_langgraph_checkpoint_database
                ),
                resume=state.resume,
            )
            adjustments = {
                item.symbol: item.model_dump(mode="json")
                for item in execution.result.allocations
            }
            return {
                "status": "completed",
                "enabled": True,
                "calls": calls,
                "thread_id": execution.thread_id,
                "checkpoint_count": execution.checkpoint_count,
                "stream_event_count": execution.event_count,
                "execution_path": list(execution.execution_path),
                "market_context": market_context,
                "reviews": [
                    item.model_dump(mode="json") for item in execution.reviews
                ],
                "proposal": execution.proposal.model_dump(mode="json"),
                "guarded_allocation": execution.result.model_dump(mode="json"),
                "adjustments": adjustments,
                "cannot_increase_symbol_targets": True,
                "hard_risk_still_required": True,
                "external_broker": False,
            }

        runtime = LangGraphResearchParentRuntime(
            event_path=self._resolve(self.settings.workflow_langgraph_event_path)
        )
        parent = runtime.run(
            thread_id=f"{state.run_id}:RESEARCH_PARENT",
            candidate_selector=lambda: self._select_candidates(data_dir),
            symbol_runner=run_symbol,
            portfolio_runner=run_portfolio,
            audit_runner=lambda name, function: state.run_json_node(name, function),
            checkpointer_path=self._resolve(
                self.settings.workflow_research_parent_checkpoint_database
            ),
            resume=state.resume,
        )
        candidates = list(parent.candidates)
        symbol_research_calls = len(candidates) * self._research_calls_per_symbol()
        portfolio_supervisor_calls = self._portfolio_supervisor_calls(len(candidates))
        expected_calls = symbol_research_calls + portfolio_supervisor_calls
        if expected_calls > self.settings.llm_max_calls_per_run:
            raise ValueError("candidate selection exceeds the LLM call budget")

        outputs = {
            str(row["symbol"]).upper(): {
                key: value for key, value in row.items() if key != "symbol"
            }
            for row in parent.candidate_results
        }
        adjustments = dict(parent.portfolio_summary.get("adjustments", {}))
        for symbol, adjustment in adjustments.items():
            if symbol in outputs:
                outputs[symbol]["portfolio_adjustment"] = dict(adjustment)
        portfolio_summary = {
            key: value
            for key, value in parent.portfolio_summary.items()
            if key != "adjustments"
        }
        return state.run_json_node(
            "research_summary",
            lambda: {
                "candidate_count": len(candidates),
                "planned_calls": expected_calls,
                "symbol_research_calls": symbol_research_calls,
                "portfolio_supervisor_calls": portfolio_supervisor_calls,
                "memory_feedback_account": account_id,
                "memory_feedback_enabled": self.settings.workflow_memory_feedback_enabled,
                "research_runtime": self.settings.workflow_research_runtime,
                "parent_graph": {
                    "enabled": True,
                    "thread_id": parent.thread_id,
                    "checkpoint_count": parent.checkpoint_count,
                    "stream_event_count": parent.event_count,
                    "execution_path": list(parent.execution_path),
                    "dynamic_send_fan_out": True,
                    "checkpoint_database": str(
                        self._resolve(
                            self.settings.workflow_research_parent_checkpoint_database
                        )
                    ),
                },
                "langgraph_checkpoint_database": str(
                    self._resolve(
                        self.settings.workflow_langgraph_checkpoint_database
                    )
                ),
                "langchain_trace_path": str(
                    self._resolve(self.settings.workflow_langchain_trace_path)
                ),
                "langgraph_event_path": str(
                    self._resolve(self.settings.workflow_langgraph_event_path)
                ),
                "portfolio_supervisor": portfolio_summary,
                "candidates": outputs,
            },
        )

    def _run_research(
        self,
        data_dir: Path,
        *,
        mode: str,
        state: WorkflowStateStore,
        account_id: str,
        tracker: ProviderCallTracker | None = None,
    ) -> dict[str, Any]:
        if self.settings.workflow_research_parent_graph_enabled:
            return self._run_research_parent(
                data_dir,
                mode=mode,
                state=state,
                account_id=account_id,
                tracker=tracker,
            )
        return self._run_research_sequential(
            data_dir,
            mode=mode,
            state=state,
            account_id=account_id,
            tracker=tracker,
        )

    def _run_research_sequential(
        self,
        data_dir: Path,
        *,
        mode: str,
        state: WorkflowStateStore,
        account_id: str,
        tracker: ProviderCallTracker | None = None,
    ) -> dict[str, Any]:
        candidates = state.run_json_node(
            "candidate_screen",
            lambda: self._select_candidates(data_dir),
        )
        symbol_research_calls = len(candidates) * self._research_calls_per_symbol()
        portfolio_supervisor_calls = self._portfolio_supervisor_calls(len(candidates))
        expected_calls = symbol_research_calls + portfolio_supervisor_calls
        if expected_calls > self.settings.llm_max_calls_per_run:
            raise ValueError("candidate selection exceeds the LLM call budget")
        execution_mode = (
            "live"
            if mode == "live"
            else ("mock" if mode == "offline" else "dry_run")
        )
        client_settings = replace(
            self.settings,
            llm_execution_mode=execution_mode,
        )
        quick_client = build_llm_client(
            client_settings,
            self.root,
            model=self.settings.llm_quick_model,
            cache_namespace="quick",
        )
        deep_client = build_llm_client(
            client_settings,
            self.root,
            model=self.settings.llm_deep_model,
            cache_namespace="deep",
        )
        if mode == "live" and tracker is not None:
            quick_client = _ProviderTrackedLLMClient(
                quick_client,
                tracker,
                self.settings.llm_provider,
            )
            deep_client = _ProviderTrackedLLMClient(
                deep_client,
                tracker,
                self.settings.llm_provider,
            )
        evidence = [
            LocalPointInTimeEvidenceProvider(path)
            for path in self._evidence_paths(data_dir)
        ]
        outputs: dict[str, Any] = {}
        for candidate in candidates:
            symbol = str(candidate["symbol"]).upper()
            priority = float(candidate["priority"])
            pack = self._research_pack(data_dir, symbol, evidence)
            memory_context = self._decision_memory_context(
                account_id=account_id,
                symbol=symbol,
                decision_time=pack.decision_time,
            )
            pipeline = MultiAgentResearchPipeline(
                quick_client,
                deep_client,
                debate_rounds=self.settings.workflow_debate_rounds,
                risk_personas=self.settings.workflow_risk_personas,
                use_langgraph=(
                    self.settings.workflow_research_runtime == "langgraph"
                ),
                langchain_trace_path=self._resolve(
                    self.settings.workflow_langchain_trace_path
                ),
                langgraph_event_path=self._resolve(
                    self.settings.workflow_langgraph_event_path
                ),
                langgraph_retry_attempts=(
                    self.settings.workflow_langgraph_retry_attempts
                ),
                langgraph_node_timeout_seconds=(
                    self.settings.workflow_langgraph_node_timeout_seconds
                ),
                langgraph_cost_aware_routing=(
                    self.settings.workflow_langgraph_cost_aware_routing
                ),
                langgraph_hold_skip_confidence=(
                    self.settings.workflow_langgraph_hold_skip_confidence
                ),
                langgraph_human_review_mode=(
                    self.settings.workflow_langgraph_human_review_mode
                ),
            )
            thread_id = f"{state.run_id}:{symbol}"
            result = pipeline.run_resumable(
                pack,
                memory_context=memory_context,
                node_runner=lambda node, model, function, symbol=symbol: state.run_model_node(
                    f"research.{symbol}.{node}",
                    model,
                    function,
                ),
                thread_id=thread_id,
                checkpointer_path=self._resolve(
                    self.settings.workflow_langgraph_checkpoint_database
                ),
                resume=state.resume,
            )
            graph_execution = pipeline._last_graph_execution
            outputs[symbol] = {
                "screen_priority": priority,
                "memory_context_count": len(memory_context),
                "memory_context_ids": [
                    str(item.get("memory_id")) for item in memory_context
                ],
                "clients": {
                    "quick": quick_client.identity,
                    "deep": deep_client.identity,
                },
                "runtime": self.settings.workflow_research_runtime,
                "langgraph": (
                    {
                        "thread_id": graph_execution.thread_id,
                        "checkpoint_count": graph_execution.checkpoint_count,
                        "execution_path": list(graph_execution.execution_path),
                        "human_review": graph_execution.latest_state.get(
                            "human_review", {}
                        ),
                        "interrupted": graph_execution.interrupted,
                        "stream_event_count": graph_execution.event_count,
                    }
                    if graph_execution is not None
                    else None
                ),
                "result": result.model_dump(mode="json"),
            }

        portfolio_summary: dict[str, Any] = {
            "status": "disabled",
            "enabled": self.settings.workflow_portfolio_supervisor_enabled,
            "calls": portfolio_supervisor_calls,
        }
        if portfolio_supervisor_calls:
            candidate_plans = [
                {
                    "symbol": symbol,
                    "screen_priority": float(row["screen_priority"]),
                    **dict(row["result"]["trader"]),
                }
                for symbol, row in sorted(outputs.items())
            ]
            providers = {
                str(item["symbol"]).upper(): LocalCsvProvider(
                    data_dir / f"{str(item['symbol']).upper()}.csv",
                    str(item["symbol"]).upper(),
                )
                for item in candidates
            }
            market_context = state.run_json_node(
                "research.portfolio.market_context",
                lambda: build_portfolio_market_context(
                    providers,
                    window_sessions=(
                        self.settings.workflow_portfolio_correlation_window
                    ),
                    high_correlation_threshold=(
                        self.settings.workflow_portfolio_high_correlation_threshold
                    ),
                ),
            )
            portfolio_runtime = LangGraphPortfolioSupervisorRuntime(
                quick_client,
                deep_client,
                trace_path=self._resolve(
                    self.settings.workflow_langchain_trace_path
                ),
                event_path=self._resolve(
                    self.settings.workflow_langgraph_event_path
                ),
                retry_attempts=self.settings.workflow_langgraph_retry_attempts,
                max_gross_target=self.settings.max_gross_exposure,
                max_positions=self.settings.max_positions,
                high_correlation_threshold=(
                    self.settings.workflow_portfolio_high_correlation_threshold
                ),
                cluster_gross_cap=(
                    self.settings.workflow_portfolio_cluster_gross_cap
                ),
            )
            portfolio_execution = portfolio_runtime.run(
                candidate_plans,
                market_context,
                thread_id=f"{state.run_id}:PORTFOLIO",
                node_runner=lambda node, model, function: state.run_model_node(
                    f"research.{node}",
                    model,
                    function,
                ),
                checkpointer_path=self._resolve(
                    self.settings.workflow_langgraph_checkpoint_database
                ),
                resume=state.resume,
            )
            adjusted = {
                item.symbol: item.model_dump(mode="json")
                for item in portfolio_execution.result.allocations
            }
            for symbol, row in outputs.items():
                row["portfolio_adjustment"] = adjusted[symbol]
            portfolio_summary = {
                "status": "completed",
                "enabled": True,
                "calls": portfolio_supervisor_calls,
                "thread_id": portfolio_execution.thread_id,
                "checkpoint_count": portfolio_execution.checkpoint_count,
                "stream_event_count": portfolio_execution.event_count,
                "execution_path": list(portfolio_execution.execution_path),
                "market_context": market_context,
                "reviews": [
                    item.model_dump(mode="json")
                    for item in portfolio_execution.reviews
                ],
                "proposal": portfolio_execution.proposal.model_dump(mode="json"),
                "guarded_allocation": (
                    portfolio_execution.result.model_dump(mode="json")
                ),
                "cannot_increase_symbol_targets": True,
                "hard_risk_still_required": True,
                "external_broker": False,
            }

        return state.run_json_node(
            "research_summary",
            lambda: {
                "candidate_count": len(candidates),
                "planned_calls": expected_calls,
                "symbol_research_calls": symbol_research_calls,
                "portfolio_supervisor_calls": portfolio_supervisor_calls,
                "memory_feedback_account": account_id,
                "memory_feedback_enabled": self.settings.workflow_memory_feedback_enabled,
                "research_runtime": self.settings.workflow_research_runtime,
                "langgraph_checkpoint_database": str(
                    self._resolve(
                        self.settings.workflow_langgraph_checkpoint_database
                    )
                ),
                "langchain_trace_path": str(
                    self._resolve(self.settings.workflow_langchain_trace_path)
                ),
                "langgraph_event_path": str(
                    self._resolve(self.settings.workflow_langgraph_event_path)
                ),
                "portfolio_supervisor": portfolio_summary,
                "candidates": outputs,
            },
        )

    @staticmethod
    def _research_overlay(row: dict[str, Any]) -> dict[str, Any]:
        result = dict(row["result"])
        original = dict(result["trader"])
        final = dict(original)
        portfolio_adjustment = row.get("portfolio_adjustment")
        if portfolio_adjustment is not None:
            adjustment = dict(portfolio_adjustment)
            action = str(adjustment.get("action", "HOLD")).upper()
            target = float(adjustment.get("target_weight", 0.0))
            if action not in {"BUY", "HOLD", "SELL"}:
                raise ValueError(f"invalid portfolio adjustment action: {action}")
            if target > float(original.get("target_weight", 0.0)) + 1e-12:
                raise ValueError("portfolio adjustment attempted to increase target")
            if str(original.get("action", "HOLD")).upper() == "SELL" and action != "SELL":
                raise ValueError("portfolio adjustment weakened a protective SELL")
            final.update(
                {
                    "action": action,
                    "confidence": min(
                        float(original.get("confidence", 0.0)),
                        float(adjustment.get("confidence", 0.0)),
                    ),
                    "target_weight": target,
                    "order_type": (
                        "NO_ORDER" if action == "HOLD" else "MARKET_NEXT_OPEN"
                    ),
                    "rationale": str(adjustment.get("rationale", "portfolio adjusted")),
                    "requires_human_approval": True,
                }
            )
        final["trace"] = {
            "graph_version": "portfolio_supervised_research_graph_v2",
            "clients": dict(row.get("clients", {})),
            "analysts": {
                "news": result.get("news"),
                "macro": result.get("macro"),
                "fundamental": result.get("fundamental"),
            },
            "debate_rounds": list(result.get("debate_rounds", [])),
            "manager": result.get("manager"),
            "preliminary_trader": result.get("preliminary_trader"),
            "risk_reviews": list(result.get("risk_reviews", [])),
            "portfolio_manager": result.get("trader"),
            "portfolio_adjustment": (
                dict(portfolio_adjustment)
                if portfolio_adjustment is not None
                else None
            ),
            "non_expansion_verified": (
                float(final.get("target_weight", 0.0))
                <= float(original.get("target_weight", 0.0)) + 1e-12
            ),
        }
        return final

    def _assemble_research_overlays(
        self,
        research_result: dict[str, Any],
        provider_quality: dict[str, Any] | None = None,
    ) -> dict[str, dict[str, Any]]:
        candidates = research_result.get("candidates", {})
        if not isinstance(candidates, dict) or not candidates:
            return {}
        quality = dict(provider_quality or {})
        quality_status = str(quality.get("status", "NORMAL")).upper()
        if quality_status == QualityGateStatus.BLOCKED.value:
            raise ValueError("blocked provider quality cannot enter research overlays")
        allow_increase = bool(quality.get("allow_position_increase", True))
        overlays = {
            str(symbol).upper(): self._research_overlay(dict(row))
            for symbol, row in candidates.items()
        }
        for symbol, overlay in overlays.items():
            original_action = str(overlay.get("action", "HOLD")).upper()
            original_target = float(overlay.get("target_weight", 0.0))
            enforced = False
            if not allow_increase and original_action == "BUY":
                overlay.update(
                    {
                        "action": "HOLD",
                        "target_weight": 0.0,
                        "order_type": "NO_ORDER",
                        "confidence": min(
                            float(overlay.get("confidence", 0.0)),
                            float(quality.get("mean_score", 0.0)),
                        ),
                        "rationale": (
                            "Data Quality Gate blocked position expansion; "
                            + str(overlay.get("rationale", ""))
                        ).strip(),
                        "requires_human_approval": True,
                    }
                )
                enforced = True
            trace = dict(overlay.get("trace", {}))
            trace["data_quality_gate"] = {
                "status": quality_status,
                "allow_position_increase": allow_increase,
                "requires_human_review": bool(
                    quality.get("requires_human_review", False)
                ),
                "minimum_score": quality.get("minimum_score"),
                "mean_score": quality.get("mean_score"),
                "degraded_resources": list(
                    quality.get("degraded_resources", [])
                ),
                "blocked_resources": list(quality.get("blocked_resources", [])),
                "original_action": original_action,
                "original_target_weight": original_target,
                "enforced_hold": enforced,
                "cannot_increase_risk": True,
            }
            trace["non_expansion_verified"] = (
                float(overlay.get("target_weight", 0.0))
                <= original_target + 1e-12
            )
            overlay["trace"] = trace
            overlays[symbol] = overlay
        expected = sorted(str(symbol).upper() for symbol in candidates)
        actual = sorted(overlays)
        if expected != actual:
            raise ValueError(
                "research overlay assembly mismatch: "
                f"expected={expected}, actual={actual}"
            )
        return overlays

    def _prepare_decision_input(
        self,
        validation: dict[str, Any],
        research_result: dict[str, Any],
        overlays: dict[str, dict[str, Any]],
        provider_quality: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        action_counts = {"BUY": 0, "HOLD": 0, "SELL": 0}
        max_target = 0.0
        approval_required_count = 0
        all_non_expansion = True
        all_orders_valid = True
        all_targets_within_limit = True
        normalized: dict[str, dict[str, Any]] = {}
        for symbol, raw in sorted(overlays.items()):
            overlay = dict(raw)
            action = str(overlay.get("action", "HOLD")).upper()
            target = float(overlay.get("target_weight", 0.0))
            order_type = str(overlay.get("order_type", "NO_ORDER")).upper()
            if action not in action_counts:
                raise ValueError(f"invalid research overlay action: {action}")
            if not 0.0 <= target <= 1.0:
                raise ValueError(f"invalid research overlay target for {symbol}")
            if action == "HOLD" and order_type != "NO_ORDER":
                all_orders_valid = False
            if action in {"BUY", "SELL"} and order_type != "MARKET_NEXT_OPEN":
                all_orders_valid = False
            if action == "SELL" and target > 1e-12:
                all_orders_valid = False
            requires_approval = bool(overlay.get("requires_human_approval", False))
            if requires_approval:
                approval_required_count += 1
            action_counts[action] += 1
            max_target = max(max_target, target)
            all_targets_within_limit = (
                all_targets_within_limit
                and target <= self.settings.max_position_weight + 1e-12
            )
            trace = overlay.get("trace", {})
            all_non_expansion = (
                all_non_expansion
                and isinstance(trace, dict)
                and bool(trace.get("non_expansion_verified", False))
            )
            normalized[symbol] = overlay

        canonical = json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        validation_complete = str(validation.get("status", "")).lower() in {
            "completed",
            "not_required",
        }
        research_enabled = bool(research_result.get("candidates"))
        approval_complete = approval_required_count == len(overlays)
        quality = dict(provider_quality or {})
        quality_status = str(quality.get("status", "NORMAL")).upper()
        provider_not_blocked = quality_status != QualityGateStatus.BLOCKED.value
        safe = (
            validation_complete
            and provider_not_blocked
            and all_orders_valid
            and all_targets_within_limit
            and (all_non_expansion if overlays else True)
            and (approval_complete if overlays else True)
        )
        return {
            "schema_version": 3,
            "status": "ready" if overlays else "no_research",
            "data_validation_status": validation.get("status"),
            "research_enabled": research_enabled,
            "candidate_symbols": sorted(overlays),
            "overlay_count": len(overlays),
            "action_counts": action_counts,
            "directional_count": action_counts["BUY"] + action_counts["SELL"],
            "approval_required_count": approval_required_count,
            "all_require_human_approval": approval_complete,
            "all_non_expansion_verified": all_non_expansion if overlays else True,
            "all_orders_valid": all_orders_valid,
            "all_targets_within_position_limit": all_targets_within_limit,
            "maximum_target_weight": max_target,
            "overlay_payload_sha256": hashlib.sha256(canonical).hexdigest(),
            "provider_quality": {
                "status": quality_status,
                "minimum_score": quality.get("minimum_score"),
                "mean_score": quality.get("mean_score"),
                "allow_position_increase": bool(
                    quality.get("allow_position_increase", True)
                ),
                "requires_human_review": bool(
                    quality.get("requires_human_review", False)
                ),
                "degraded_resources": list(
                    quality.get("degraded_resources", [])
                ),
                "blocked_resources": list(quality.get("blocked_resources", [])),
                "non_expansion_gate_applied": not bool(
                    quality.get("allow_position_increase", True)
                ),
            },
            "deterministic_decision_graph": {
                "enabled": self.settings.workflow_decision_graph_enabled,
                "thread_suffix": "DECISION",
                "checkpoint_database": (
                    self.settings.workflow_decision_checkpoint_database
                ),
                "input_fingerprint": "sha256",
                "plan_fingerprint": "sha256",
                "account_mutation_in_graph": False,
                "order_persistence_in_graph": False,
            },
            "provider_allows_position_increase": bool(
                quality.get("allow_position_increase", True)
            ),
            "safe_for_paper_input": safe,
            "external_broker": False,
        }

    def _run_workflow_core(
        self,
        data_dir: Path,
        *,
        mode: str,
        state: WorkflowStateStore,
        account_id: str,
        tracker: ProviderCallTracker | None,
        provider_quality: dict[str, Any],
    ) -> WorkflowCoreExecution:
        runtime = LangGraphWorkflowCoreRuntime(
            event_path=self._resolve(self.settings.workflow_langgraph_event_path)
        )
        data_required = bool(
            self.settings.workflow_run_research
            or self.settings.workflow_run_paper
            or (mode == "live" and self.settings.workflow_refresh_market)
        )
        return runtime.run(
            thread_id=f"{state.run_id}:WORKFLOW_CORE",
            research_enabled=self.settings.workflow_run_research,
            data_validator=lambda: (
                {
                    **self._validate_local_data(data_dir),
                    "provider_quality": dict(provider_quality),
                }
                if data_required
                else {
                    "status": "not_required",
                    "data_dir": str(data_dir),
                    "market_files": 0,
                    "reason": "no research, paper session or market refresh consumes data",
                    "provider_quality": dict(provider_quality),
                }
            ),
            research_runner=lambda: self._run_research(
                data_dir,
                mode=mode,
                state=state,
                account_id=account_id,
                tracker=tracker,
            ),
            overlay_assembler=lambda research: self._assemble_research_overlays(
                dict(research),
                provider_quality,
            ),
            decision_preparer=lambda validation, research, overlays: (
                self._prepare_decision_input(
                    dict(validation),
                    dict(research),
                    {
                        str(symbol): dict(overlay)
                        for symbol, overlay in overlays.items()
                    },
                    provider_quality,
                )
            ),
            audit_runner=lambda node, function: state.run_json_node(node, function),
            checkpointer_path=self._resolve(
                self.settings.workflow_core_checkpoint_database
            ),
            resume=state.resume,
        )

    @staticmethod
    def _write_result(path: Path, payload: dict[str, Any]) -> None:
        temporary = path.with_name(f"{path.name}.tmp.{os.getpid()}")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        temporary.replace(path)

    def execute(
        self,
        *,
        mode: str | None = None,
        account_id: str | None = None,
        confirm_live: bool = False,
        run_id: str | None = None,
        resume: bool = False,
    ) -> dict[str, Any]:
        selected = self._mode(mode)
        if selected == "live" and not confirm_live:
            raise ValueError("live workflow requires confirm_live=True")
        if selected == "live" and self.settings.llm_provider == "zhipu":
            if not os.environ.get(self.settings.llm_api_key_env):
                raise ValueError(
                    f"live workflow requires {self.settings.llm_api_key_env}"
                )
        if resume and not run_id:
            raise ValueError("workflow resume requires an explicit run_id")

        resolved_run_id = run_id or datetime.now(timezone.utc).strftime(
            "workflow-%Y%m%dT%H%M%S%fZ"
        )
        run_dir = self._resolve(self.settings.workflow_artifact_dir) / resolved_run_id
        state = WorkflowStateStore(
            run_dir,
            run_id=resolved_run_id,
            mode=selected,
            settings_hash=self._settings_hash(),
            resume=resume,
        )
        usage_store = (
            ProviderUsageStore(
                self._resolve(self.settings.workflow_provider_usage_database)
            )
            if selected == "live"
            else None
        )
        tracker = (
            ProviderCallTracker(usage_store, resolved_run_id)
            if usage_store is not None
            else None
        )

        existing_state = state.state
        if resume and existing_state.get("status") == "COMPLETE":
            result_path = Path(str(existing_state.get("result_path", run_dir / "result.json")))
            if result_path.is_file():
                payload = json.loads(result_path.read_text(encoding="utf-8"))
                payload["artifact"] = str(result_path)
                payload["workflow_state"] = str(state.state_path)
                return payload

        plan = state.run_json_node(
            "preflight",
            lambda: self.plan(mode=selected, account_id=account_id),
        )
        self._write_result(run_dir / "plan.json", plan)

        data_dir = (
            self._resolve(self.settings.workflow_live_data_dir)
            if selected == "live"
            else self._resolve(self.settings.paper_data_dir)
        )
        payload: dict[str, Any] = {
            "run_id": resolved_run_id,
            "mode": selected,
            "resumed": resume,
            "plan": plan,
            "network": {"status": "not_required"},
            "data_provider": {
                "status": "not_required",
                "enabled": self.settings.workflow_provider_graph_enabled,
            },
            "data_refresh": {},
            "data_validation": {"status": "pending"},
            "workflow_core": {
                "status": "disabled",
                "enabled": self.settings.workflow_core_graph_enabled,
            },
            "research": {"status": "disabled"},
            "decision_preparation": {"status": "pending"},
            "paper": {"status": "disabled"},
        }

        provider_quality: dict[str, Any] = {
            "status": QualityGateStatus.NORMAL.value,
            "mode": "local_validated",
            "request_count": 0,
            "minimum_score": 1.0,
            "mean_score": 1.0,
            "allow_position_increase": True,
            "requires_human_review": False,
            "blocked_resources": [],
            "degraded_resources": [],
            "assessments": [],
        }

        if selected == "live":
            assert tracker is not None
            if self.settings.workflow_network_preflight:
                network = state.run_json_node(
                    "network_probe",
                    lambda: self._network_probe(tracker),
                    reuse_completed=False,
                )
                payload["network"] = {"status": "checked", "result": network}
                state.run_json_node(
                    "network_gate",
                    lambda: self._network_gate(network),
                    reuse_completed=False,
                )
            else:
                payload["network"] = {
                    "status": "disabled",
                    "warning": "network preflight disabled; TLS verification remains mandatory",
                }
            refresh_enabled = any(
                (
                    self.settings.workflow_refresh_market,
                    self.settings.workflow_refresh_news,
                    self.settings.workflow_refresh_macro,
                    self.settings.workflow_refresh_fundamentals,
                )
            )
            if self.settings.workflow_provider_graph_enabled and refresh_enabled:
                provider_execution = self._run_data_provider_graph(
                    data_dir,
                    state=state,
                    tracker=tracker,
                )
                provider_payload = self._provider_execution_payload(
                    provider_execution
                )
                provider_payload["checkpoint_database"] = str(
                    self._resolve(
                        self.settings.workflow_provider_checkpoint_database
                    )
                )
                payload["data_provider"] = state.run_json_node(
                    "data_provider_summary",
                    lambda: provider_payload,
                )
                payload["data_refresh"] = {
                    "status": "completed",
                    "provider_graph": True,
                    "results": provider_payload["persisted_results"],
                }
                provider_quality = dict(provider_execution.quality_summary)
            elif self.settings.workflow_provider_graph_enabled:
                payload["data_provider"] = {
                    "status": "not_required",
                    "enabled": True,
                    "request_count": 0,
                    "reason": "all provider refresh operations are disabled",
                }
            else:
                payload["data_provider"] = {
                    "status": "legacy_disabled",
                    "enabled": False,
                    "warning": (
                        "Provider Graph disabled; position expansion is blocked"
                    ),
                }
                provider_quality = {
                    "status": QualityGateStatus.DEGRADED.value,
                    "mode": "legacy_unassessed",
                    "request_count": 0,
                    "minimum_score": 0.0,
                    "mean_score": 0.0,
                    "allow_position_increase": False,
                    "requires_human_review": True,
                    "blocked_resources": [],
                    "degraded_resources": ["legacy_provider_refresh"],
                    "assessments": [],
                }
                if self.settings.workflow_refresh_market:
                    payload["data_refresh"]["market"] = {
                        "status": "completed",
                        "result": state.run_json_node(
                            "refresh_market",
                            lambda: self._refresh_market(data_dir, tracker),
                        ),
                    }
                if self.settings.workflow_refresh_news:
                    payload["data_refresh"]["news"] = {
                        "status": "completed",
                        "result": state.run_json_node(
                            "refresh_news",
                            lambda: self._refresh_news(data_dir, tracker),
                        ),
                    }
                if self.settings.workflow_refresh_macro:
                    payload["data_refresh"]["macro"] = {
                        "status": "completed",
                        "result": state.run_json_node(
                            "refresh_macro",
                            lambda: self._refresh_macro(data_dir, tracker),
                        ),
                    }
                if self.settings.workflow_refresh_fundamentals:
                    payload["data_refresh"]["fundamentals"] = {
                        "status": "completed",
                        "result": state.run_json_node(
                            "refresh_fundamentals",
                            lambda: self._refresh_fundamentals(data_dir, tracker),
                        ),
                    }

        research_overlays: dict[str, dict[str, Any]] = {}
        account = account_id or self.settings.paper_default_account_id
        if self.settings.workflow_core_graph_enabled:
            core = self._run_workflow_core(
                data_dir,
                mode=selected,
                state=state,
                account_id=account,
                tracker=tracker,
                provider_quality=provider_quality,
            )
            payload["workflow_core"] = {
                "status": "completed",
                "enabled": True,
                "thread_id": core.thread_id,
                "checkpoint_count": core.checkpoint_count,
                "stream_event_count": core.event_count,
                "execution_path": list(core.execution_path),
                "checkpoint_database": str(
                    self._resolve(self.settings.workflow_core_checkpoint_database)
                ),
                "provider_refresh_in_graph": False,
                "paper_execution_in_graph": False,
                "external_broker": False,
            }
            payload["data_validation"] = core.data_validation
            if selected != "live":
                payload["data_refresh"] = core.data_validation
            if self.settings.workflow_run_research:
                payload["research"] = {
                    "status": "completed",
                    "result": core.research,
                }
            payload["decision_preparation"] = core.decision_preparation
            research_overlays = core.research_overlays
        else:
            validation = state.run_json_node(
                "validate_local_data",
                lambda: {
                    **self._validate_local_data(data_dir),
                    "provider_quality": dict(provider_quality),
                },
            )
            payload["data_validation"] = validation
            if selected != "live":
                payload["data_refresh"] = validation
            research_result: dict[str, Any] = {
                "status": "disabled",
                "candidate_count": 0,
                "candidates": {},
            }
            if self.settings.workflow_run_research:
                research_result = self._run_research(
                    data_dir,
                    mode=selected,
                    state=state,
                    account_id=account,
                    tracker=tracker,
                )
                payload["research"] = {
                    "status": "completed",
                    "result": research_result,
                }
                research_overlays = self._assemble_research_overlays(
                    research_result,
                    provider_quality,
                )
            payload["decision_preparation"] = state.run_json_node(
                "workflow.core.decision_preparation.legacy",
                lambda: self._prepare_decision_input(
                    validation,
                    research_result,
                    research_overlays,
                    provider_quality,
                ),
            )

        if self.settings.workflow_run_paper:
            if selected == "dry_run":
                payload["paper"] = state.run_json_node(
                    "paper_session",
                    lambda: {
                        "status": "planned",
                        "mutated": False,
                        "external_broker": False,
                    },
                )
            else:
                account = account_id or self.settings.paper_default_account_id
                store = PaperTradingStore(
                    self._resolve(self.settings.paper_database_path)
                )
                if store.get_account(account) is None:
                    payload["paper"] = state.run_json_node(
                        "paper_session",
                        lambda: {
                            "status": "skipped",
                            "reason": f"paper account does not exist: {account}",
                            "external_broker": False,
                        },
                    )
                else:
                    service = PaperTradingService(store, self.settings)
                    result = state.run_json_node(
                        "paper_session",
                        lambda: run_next_with_lock(
                            service,
                            account,
                            data_dir=data_dir,
                            lock_path=self._resolve(self.settings.paper_lock_path),
                            evidence_paths=self._evidence_paths(data_dir),
                            research_overlays=research_overlays,
                        ),
                    )
                    payload["paper"] = {
                        "status": "completed",
                        "external_broker": False,
                        "result": result,
                    }

        payload["provider_usage"] = (
            usage_store.summary(run_id=resolved_run_id)
            if usage_store is not None
            else {"run_id": resolved_run_id, "providers": []}
        )
        payload["settings"] = asdict(self.settings)
        output = run_dir / "result.json"
        self._write_result(output, payload)
        state.complete(output)
        payload["artifact"] = str(output)
        payload["workflow_state"] = str(state.state_path)
        return payload
